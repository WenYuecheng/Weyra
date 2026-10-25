"""单步模型请求编排、流式消费与溢出/容灾熔断机 (Model Step Orchestration & Failover Pipeline)。

【架构定位】
本模块负责在智能体单轮迭代中安全发起大模型推理。
涵盖请求预组装、Hook 动态视窗拦截与改写、流式分块消费与事件上报、
物理上下文溢出（overflow）自动压缩自愈，以及瞬态网络故障（transient）自动路由故障转移（Route Failover）。
确保模型请求在不稳定的分布式网络与苛刻的上下文窗口约束下具备高度自愈能力。

【设计思想与开源血统】
1. 深度借鉴 Pi Agent (packages/agent/ & dg-ai-notes 第 4 章 模型调用):
   - 请求前置拦截点 (`BeforeModelCallHook`): 在模型请求实际物理发送前 1ms 触发，支持动态改写上下文或审查敏感内容；
   - 改写后即时安全校验：Hook 改写消息后立即通过 `estimate_tokens` 重新核算 Token 预算，若超出模型物理窗口立即抛出
     `ContextOverflow` 防御性阻断，杜绝无意义网络调用；
   - 工具与响应成对校验 (`_validate_pairs`): 发送前静态校验 tool_calls 与 tool 角色配对关系，确保 API 400 免疫。
2. 深度借鉴 DeepSeek-Harness (packages/llm & packages/compaction):
   - 物理溢出自动压缩闭环：捕获供应商返回的 `overflow` 错误后，自动触发 `compact(trigger="overflow")`，
     比对压缩前后状态指纹（digest），若成功缩小上下文则原地重试（受限于 `max_overflow_retries`）；
   - 备选路由平滑接管：当主交互模型遭遇不可恢复的瞬态网络或限流故障时，自动调用 `next_fallback("main")`
     切换至备用路由模型并激活新路线。

【核心不变性与安全约束】
- 物理尝试上限约束：单轮步进物理尝试次数受限于 `attempts['limit'] = 4`，杜绝无限失败重试。
- 幂等状态比对：压缩前后通过 SHA-256 指纹核验，若压缩未能实际减少上下文则直接阻断，防止空转死循环。
"""

import asyncio
from .context import ContextOverflow, estimate_tokens
from weyra.contracts import digest, PreparedRequest
from .model_runtime import ModelError
from .hooks import BeforeModelCallHook


async def run_model_step(self, ctx):
    """编排并执行单次模型推理请求，集成流式传输与自愈重试。

    【执行流程与容灾时序】
    1. 检查协同取消信号，准备并获取上下文请求信封 (`prepare()`)；
    2. 触发 `BeforeModelCallHook`，允许外部中间件对输入消息序列进行审查或改写；
    3. 流式消费模型输出，将 `text` 增量实时通过 `ctx.emit` 广播给前端，提取最终完整的 `response`；
    4. 异常分级处置：
       - 若捕获 `overflow`（上下文物理超限）：调度上下文压缩自愈 (`compact(trigger='overflow')`)，
         校验前后指纹是否有实质缩减，随后原地重试；
       - 若捕获 `transient`（网络超时或供应商瞬态限流）：激活备用模型路由 (`next_fallback`) 并重试；
       - 若超出物理重试上限，抛出 ModelError 终止。

    Args:
        self: 包含 context_manager, runtime, session, tools, hooks 的智能体宿主对象。
        ctx: 运行时上下文 RunContext 实例。

    Returns:
        ModelResponse: 最终校验完成的完整大模型响应结果。

    Raises:
        PermissionError: 当 Hook 显式阻断请求时抛出。
        ContextOverflow: 当请求超出上下文容量且无法压缩精简时抛出。
        ModelError: 当达到物理尝试上限或遇到无法恢复的模型错误时抛出。
    """
    attempts = {"attempts": 0, "limit": 4}
    overflow_retries = 0
    while attempts["attempts"] < attempts["limit"]:
        ctx.check_cancelled()
        request = await self.context_manager.prepare(
            self.runtime, ctx, self.tools.declarations()
        )
        start_system = ctx.services.get("run_system")
        transformed = await self.hooks.emit(BeforeModelCallHook(list(request.messages), attempts["attempts"]))
        if transformed and transformed.block:
            raise PermissionError(transformed.reason or "Model request blocked")
        if start_system is not None or (transformed and transformed.updated_messages is not None):
            request = copy_request(request, system=start_system if start_system is not None else request.system,
                              messages=tuple(transformed.updated_messages) if transformed and transformed.updated_messages is not None else request.messages)
            parameters = dict(request.parameters)
            parameters.pop("cache_system_prefix", None)
            parameters["estimated_input_tokens"] = estimate_tokens(request.to_dict())
            request = copy_request(request, parameters=parameters)
            from .model_runtime import _validate_pairs
            _validate_pairs(request)
            if parameters["estimated_input_tokens"] + self.context_manager._reserved_output > request.model.context_window:
                raise ContextOverflow("Hook-transformed request exceeds model capacity")
        response = None
        try:
            async for event in self.runtime.stream(request, ctx, attempt_budget=attempts):
                if event.kind == "text":
                    await ctx.emit("text", event.data)
                elif event.kind == "response":
                    response = event.data
            if response is None:
                raise ModelError("invalid", "No complete model response")
            return response
        except ModelError as error:
            await ctx.emit("attempt_reset", {"kind": error.kind})
            ctx.check_cancelled()
            if (
                error.kind == "overflow"
                and overflow_retries < self.context_manager.policy.max_overflow_retries
            ):
                before = digest([m.to_dict() for m in self.session.project()])
                try:
                    await self.context_manager.compact(self.runtime, ctx, trigger="overflow")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if before == digest([m.to_dict() for m in self.session.project()]):
                        raise
                after = digest([m.to_dict() for m in self.session.project()])
                if before == after:
                    raise ContextOverflow("Provider overflow could not be reduced") from error
                overflow_retries += 1
                continue
            model = self.runtime.next_fallback("main", self.context_manager.model)
            if (
                error.kind != "transient"
                or model is None
                or attempts["attempts"] >= attempts["limit"]
            ):
                raise
            self.runtime.activate_route("main", model, ctx)
            self.context_manager.model = model
            self.context_manager._sync_policy(self.runtime)
            self.context_manager.change("explicit_model_failover")
    raise ModelError("attempt_limit", "Model physical attempt limit reached")


def copy_request(request: PreparedRequest, **changes) -> PreparedRequest:
    """不可变 PreparedRequest 信封的浅拷贝与选择性属性覆写助手函数。

    【设计原理】
    保持请求信封的不可变性（Immutable Value Object），当 Hook 改写系统提示词或消息列表时，
    通过函数式拷贝产生新的不可变对象，绝不就地篡改旧请求实例。

    Args:
        request: 待复制的原始 PreparedRequest 实例。
        **changes: 需要选择性覆写的字段键值对。

    Returns:
        PreparedRequest: 携带更新后属性的全新不可变请求信封实例。
    """
    values = dict(model=request.model, system=request.system, messages=request.messages,
                  tools=request.tools, purpose=request.purpose, context_version=request.context_version,
                  parameters=request.parameters, id=request.id)
    values.update(changes)
    return PreparedRequest(**values)

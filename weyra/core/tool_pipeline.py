"""统一原生工具调用执行管道与调度拦截引擎 (Tool Execution Pipeline)。

【架构定位】(Architectural Role)
    位于 `weyra.core` 工具体系的执行控制层，作为所有调度模式（独占互斥、共享并发、派发）
    底层统一共享的原生调用执行管道。
    负责协调前置依赖等待、动态 Hook 拦截改写、JSON Schema 入参强校验、
    读写并发槽位分配（`_slot`）、权限策略二次核验、意图事件审计记录、
    真实执行调度以及跨阶段取消与异常规范化映射。

【开源设计思想与架构血统】(Design Pedigree)
    1. 遵循 DeepSeek-Harness 的工具执行管道规约（Tool Invocation Pipeline）：
       分离调度策略（Scheduler）与具体执行管道（Pipeline），使单工具执行具备标准的流转生命周期；
    2. 继承 Pi Agent 确定性意图记录哲学（Intent-before-Execution）：
       在真实执行任何产生副作用的工具前，强制先向 `SessionStore` 追加 `tool_intent` 事件，
       确保崩溃后可追溯智能体的真实意图；
    3. 集成 Model Context Protocol (MCP) 上下文隔离与私有工具调度防护机制。

【核心不变性与安全约束】(Invariants & Safety)
    - 鉴权防竞态：在获取互斥槽位后，必须二次核验工具准入有效性与权限判定，防御鉴权期间外部撤销权限；
    - 异常与取消规范化：执行异常绝不向上泄漏非受控崩溃，统一捕获并封装为带有明确状态码与效应指示的 `ToolResult`；
    - 上下文追踪清理：执行完毕后在 `finally` 块中严格重置 `ACTIVE_CALL` 上下文变量并清理注册任务引用。
"""

import asyncio
import inspect
import json

from jsonschema import Draft202012Validator
from weyra.contracts import ToolCall, ToolResult

from .hooks import ToolCallHook
from .tool_admission import ACTIVE_CALL


class ToolExecutionPipeline:
    """工具原子调用的统一执行与拦截管道 (Tool Execution Pipeline Driver)。

    【架构定位】
    统一封装单次工具调用的参数校验、权限审批、意图记录、异常隔离与结果归一化。
    """

    def __init__(self, runtime):
        """初始化工具执行管道。

        Args:
            runtime: 底层关联的 ToolRuntime 运行时实例。
        """
        self.runtime = runtime

    def __getattr__(self, name: str):
        """代理访问关联运行时的底层内部方法与属性。"""
        return getattr(self.runtime, name)

    async def run(
        self,
        call: ToolCall,
        context,
        previous: asyncio.Event | None = None,
        prepared: asyncio.Event | None = None,
        tool_set=None,
    ) -> ToolResult:
        """执行单个工具调用的核心流转与异常捕获逻辑 (Execute Single Tool Invocation Core)。

        【执行管道阶段分解】
        1. 依赖排队：若传入前置就绪事件 `previous`，异步等待前序工具准备就绪；
        2. 步级准入校验：核验该工具在当前步骤准入集合中是否合法；
        3. Hook 拦截改写：通过 `ToolCallHook` 触发扩展点，支持外部动态修改参数或熔断阻断；
        4. Schema 强校验：使用 Draft 2020-12 标准校验参数结构是否符合工具 Schema；
        5. 槽位获取与鉴权：申请读写并发槽位，调用 `_allowed` 验证权限并复核防竞态；
        6. 意图持久化：向底层会话日志追加 `tool_intent` 事件并广播 `tool_start`；
        7. 真实物理调用：设置 `ACTIVE_CALL`，调度工具处理函数并规范化为 `ToolResult`。

        Args:
            call: 待执行的工具调用请求对象。
            context: 轮次运行上下文。
            previous: 可选的前置准备事件。
            prepared: 可选的当前准备就绪通知事件。
            tool_set: 可选的当前步骤准入工具集。

        Returns:
            ToolResult: 包含执行状态、输出内容与副作用标记的标准结果对象。
        """
        inherited_set, parent_call = ACTIVE_CALL.get()
        tool_set = tool_set if tool_set is not None else inherited_set
        registration = self._registrations.get(call.name)
        spec = self.specs.get(call.name)
        token = None
        result = ToolResult(call.id, call.name, "", status="invalid")
        try:
            if previous is not None:
                await previous.wait()
            if tool_set is not None and (
                tool_set.runtime is not self.runtime
                or not tool_set.validate(call.name)
                or (
                    call.name.startswith("mcp__")
                    and (parent_call != "mcp_call" or not tool_set.validate("mcp_call"))
                )
            ):
                result.content = "Tool registration changed or not admitted for this step"
                return result
            if (
                tool_set is None
                and registration is not None
                and registration.when is not None
            ):
                result.content = "Conditional tool requires step admission"
                return result
            if spec is None:
                result.content = "Unknown tool"
                return result
            hooks = context.services.get("hooks")
            if hooks:
                intervention = await hooks.emit(ToolCallHook(call.id, call.name, call.arguments))
                if intervention and intervention.block:
                    result.status, result.content = "denied", intervention.reason or "Tool blocked"
                    return result
                if intervention and intervention.updated_args is not None:
                    call = ToolCall(call.id, call.name, intervention.updated_args)
            errors = list(Draft202012Validator(spec.schema).iter_errors(call.arguments))
            if errors:
                result.content = "Arguments do not satisfy the tool schema"
                return result
            read = (
                None
                if spec.execution_mode == "dispatch"
                else spec.execution_mode == "read" and spec.permission == "read"
            )
            async with self._slot(read):
                context.check_cancelled()
                if not await self._allowed(spec, call.arguments, context):
                    result.status, result.content = "denied", "Permission denied"
                    return result
                context.check_cancelled()
                if self.specs.get(call.name) is not spec or (
                    tool_set is not None and not tool_set.validate(call.name)
                ):
                    result.content = "Tool registration changed during authorization"
                    return result
                context.services["tools"] = self
                if registration is not None:
                    registration.tasks.add(asyncio.current_task())
                self.session.append(
                    "tool_intent",
                    {"call_id": call.id, "name": call.name, "arguments": call.arguments},
                    checkpoint=True,
                )
                result.started = True
                emit = getattr(context, "emit", None)
                if emit:
                    await emit(
                        "tool_start",
                        {
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    )
                if prepared is not None:
                    prepared.set()
                token = ACTIVE_CALL.set((tool_set, call.name))
                value = spec.handler(call.arguments, context)
                value = await value if inspect.isawaitable(value) else value
                if isinstance(value, ToolResult):
                    result = value
                    result.call_id, result.name, result.started = call.id, call.name, True
                else:
                    result.content = (
                        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    )
                    result.structured = value if not isinstance(value, str) else None
                    result.status = "success"
                    result.effect = "none" if spec.permission == "read" else "completed"
        except asyncio.CancelledError:
            result.status, result.content = "cancelled", "Cancelled"
            result.effect = (
                "unknown"
                if result.started and spec is not None and spec.permission != "read"
                else "none"
            )
        except Exception as exc:
            result.status = "error"
            result.content = f"Tool failed ({type(exc).__name__})"
            result.effect = (
                "unknown"
                if result.started and spec is not None and spec.permission != "read"
                else "none"
            )
        finally:
            if token is not None:
                ACTIVE_CALL.reset(token)
            if registration is not None:
                registration.tasks.discard(asyncio.current_task())
            if prepared is not None:
                prepared.set()
        return result

"""有序工具批处理执行流与原子模型观察面提交 (Ordered Tool Batch Flow & Atomic Acceptance)。

【架构定位】
本模块负责在模型生成工具调用意图后，执行安全防爆批处理与结果原子提交。
在工业级 Agent 运行时中，模型可能在上下文截断状态下输出残缺不全的工具参数，
直接调用将导致灾难性执行错误；或者在并发执行多工具时因异常中断导致会话日志撕裂。
本模块通过截断熔断保护、Hook 结果动态重构、以及单个数据库事务原子提交，
为智能体与物理世界的交互构建坚不可摧的原子屏障。

【设计思想与开源血统】
1. 深度借鉴 DeepSeek-Harness (packages/tool_runtime/ & packages/session/):
   - 截断响应防御性阻断机制：若模型停止原因为 `length` 或 `max_tokens`，判定模型输出受到最大 Token 上限截断而残缺不全，
     直接将该批次所有 ToolCall 标记为 `status="invalid"` 并放弃物理执行，防止盲目调用参数截断的危险命令（如残缺的 rm 命令）；
   - 单事务原子提交（Atomic Transaction）：使用 `with agent.session.transaction():` 
     一次性将该批次所有 ToolResult 消息与审计记录持久化，保证事务的严格 ACID 特性。
2. 深度借鉴 Pi Agent (packages/agent/ & dg-ai-notes 第 5 章 工具系统):
   - 结合 `ToolResultHook` 提供工具出参改写与脱敏接口，支持在进入模型上下文前对高危数据进行过滤；
   - 观察面视图分离（Tool Result View）：支持为前端展示与模型上下文提供不同粒度的结果视图，避免长文本破坏 KV 缓存。

【核心不变性与安全约束】
- 原子提交约束：同批次内的所有工具执行结果必须在同一个数据库事务内整体提交，杜绝局部提交导致状态机撕裂。
"""

from dataclasses import asdict
from weyra.contracts import Message, ToolResult
from .hooks import ToolResultHook


async def run_tool_step(agent, response, ctx):
    """协调单批次工具调用执行、Hook 结果改写与原子化会话落地。

    【执行流程】
    1. 截断安全检查：若模型的停止原因为 `length` 或 `max_tokens`，判定参数残缺，
       将该批次所有调用直接置为 invalid 并记录审计事件，跳过物理执行；
    2. 批处理执行：调用 `agent.tools.execute_batch(response.tool_calls, ctx)` 并发调度执行；
    3. Hook 介入：通过 `ToolResultHook` 允许外部中间件对工具输出内容进行脱敏或改写；
    4. 原子提交：开启数据库事务，批量持久化 `tool_result_view` 与 `Message('tool')` 实体，
       并触发 `agent.accept_result(result)`；
    5. 延迟压缩检查：若执行期间标记了 `compact_requested`，自动调度上下文压缩。

    Args:
        agent: 实现 LoopHost 契约的智能体宿主实例。
        response: 模型返回的包含 tool_calls 的 ModelResponse。
        ctx: 运行时上下文 RunContext 实例。

    Returns:
        None
    """
    if response.stop_reason in {"length", "max_tokens"}:
        results = [
            ToolResult(
                call.id,
                call.name,
                "Truncated model response; tool not executed",
                status="invalid",
            )
            for call in response.tool_calls
        ]
        for result in results:
            agent.session.append("tool_result", asdict(result), True)
    else:
        results = await agent.tools.execute_batch(response.tool_calls, ctx)
    views = {}
    for result in results:
        intervention = await agent.hooks.emit(ToolResultHook(result.call_id, result.name, result.content, result.status != "success"))
        if intervention and intervention.updated_result is not None:
            views[result.call_id] = intervention.updated_result
    with agent.session.transaction():
        for result in results:
            if result.call_id in views:
                agent.session.append("tool_result_view", {"call_id": result.call_id, "content": views[result.call_id]}, True)
            agent.session.add_message(
                Message(
                    "tool",
                    views.get(result.call_id, result.content),
                    source="tool",
                    tool_call_id=result.call_id,
                    name=result.name,
                    provider_data={"tool_status": result.status},
                )
            )
            agent.accept_result(result)
    ctx.check_cancelled()
    if ctx.services.pop("compact_requested", False):
        await agent.context_manager.compact(agent.runtime, ctx)

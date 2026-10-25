"""纯函数式智能体主循环与外层追问队列调度器 (Functional Agent Loop & Outer Follow-Up Loop)。

【架构定位】
本模块是 Weyra 核心推理执行管道的最底层无状态实现。
完全适配自 Pi Agent 与 my-pi-agent 的工业级循环边界设计，彻底剔除对具体 SQLite 数据库、
物理文件系统路径、领域特定技能或前端界面的直接依赖。
所有持久化写入、权限确认、上下文预热与外部观测均通过 `LoopHost` 契约抽象注入，
实现了高度可复用、可单元测试、具备数学自洽性的智能体执行状态机。

【设计思想与开源血统】
1. 深度继承 Pi Agent (packages/agent/src/agent.ts & dg-ai-notes 第 3 章 Agent Loop 让模型转动起来的引擎)：
   - 函数式内层循环 (`run_agent_loop`): 严格步进上限控制（max_steps 熔断保护，防止模型死循环耗尽 Token）；
   - 响应完整性强校验：严格过滤 refusal、incomplete 异常，全局校验模型返回的工具调用 ID 唯一性（seen_calls），
     杜绝重复工具执行与非良构参数传递；
   - 步进级引导插话感知 (`has_steering`): 在单步完成后即时感知用户高优先级干预，若有插话则立即进入下一轮思考，
     支持长任务执行期间的人工实时纠偏；
   - 外部顺序追问队列 (`run_follow_up_loop`): 遵循 Pi Agent 外层排队准则，确保前序用户任务及其
     所有副作用完全落实并落盘后，才依次准入追问队列中的下一个输入项，防止状态机并发撕裂。
2. 借鉴 DeepSeek-Harness (packages/core & packages/tool_runtime):
   - 原子化工具执行步骤挂载 (`run_tool_step`)：工具调用批处理执行与两阶段视图对齐。

【核心不变性与安全约束】
- 步进熔断约束 (Step Limit Invariant)：单轮任务循环步数受限于 `max_steps`，一旦越界立即抛出 RuntimeError 并安全保存断点。
- 协同式取消感知：每步开始前必须显式调用 `ctx.check_cancelled()`，确保在收到 abort 信号时毫秒级停止。
"""

from dataclasses import dataclass
from typing import Callable, Any
from weyra.contracts import Message
from .tool_step import run_tool_step
from .model_runtime import ModelError


@dataclass
class LoopHost:
    """循环宿主依赖注入协议契约 (Loop Host Protocol Contract)。

    【职责定位】
    作为纯函数 `run_agent_loop` 与应用层上层运行时之间的依赖注入中介，
    封装所有与持久化、上下文管理、工具调度、环境交互相关的具体业务实现。

    Attributes:
        max_steps (int): 单轮任务允许的最大思考-行动循环步数（硬熔断保护阈值）。
        session (Any): 会话事实存储实例（SessionStore），提供 append、project、chain 等操作。
        context_manager (Any): 上下文容量管理器（ContextManager），负责窗口核算与请求打包。
        runtime (Any): 工具调度与执行运行时（ToolRuntime）。
        tools (Any): 工具声明集合，提供 declarations() 导出 JSON Schema。
        hooks (Any): 全局 Hook 注册表（HookRegistry），用于步进拦截与内容改写。
        before_step (Callable): 单步前置准备协程回调 `async def before_step(ctx) -> None`。
        prepare_response (Callable): 模型响应生成协程回调 `async def prepare_response(ctx) -> ModelResponse`。
        finish_step (Callable): 单步完成后的业务后置协程 `async def finish_step(ctx) -> bool`（返回 True 表示提前结束）。
        accept_result (Callable): 工具结果采纳回调 `def accept_result(result) -> None`。
        has_steering (Callable): 检查是否有高优先级引导插话待处理 `def has_steering() -> bool`。
    """
    max_steps: int
    session: Any
    context_manager: Any
    runtime: Any
    tools: Any
    hooks: Any
    before_step: Callable
    prepare_response: Callable
    finish_step: Callable
    accept_result: Callable
    has_steering: Callable


async def run_agent_loop(agent: LoopHost, ctx):
    """执行单个任务轮次的核心思考-行动循环 (Core Model-Tool ReAct Loop)。

    【执行流程与算法规范】
    1. 步进上限循环迭代（0 到 agent.max_steps - 1）；
    2. 检查协同式取消状态（`ctx.check_cancelled()`），若已取消立即安全退出；
    3. 触发步进前置钩子与上下文准备（`await agent.before_step(ctx)`）；
    4. 调用大模型生成响应（`await agent.prepare_response(ctx)`）；
    5. 严格校验响应合法性（stop_reason 合法集校验、工具调用支持度校验、工具 ID 防重校验）；
    6. 将 Assistant 消息持久化追加至会话树；
    7. 若模型返回工具调用，调度 `run_tool_step` 批处理执行并自动进入下一步；
    8. 若无工具调用，检查是否有高优先级插话（steering）；若无插话且 `finish_step` 确认完成，返回最终文本与状态。

    Args:
        agent: 实现 LoopHost 契约的宿主代理实例。
        ctx: 运行时上下文 RunContext 实例。

    Returns:
        str: 最终回复文本；结束状态写入 ctx.turn_state（completed 或 incomplete）。

    Raises:
        RuntimeError: 当步数达到 max_steps 上限时抛出，强制保存当前会话断点并终止。
        ModelError: 当模型响应异常、拒绝、工具标识重复或协议错乱时抛出。
    """
    for step in range(agent.max_steps):
        ctx.check_cancelled()
        await agent.before_step(ctx)
        response = await agent.prepare_response(ctx)
        if response.stop_reason not in {"stop", "tool_calls", "length", "max_tokens"}:
            raise ModelError("invalid", "Model response was refused or incomplete")
        if response.tool_calls and not agent.context_manager.model.supports_tools:
            raise ModelError("invalid", "Selected model does not support tool calls")
        seen_calls = {
            call.id for message in agent.session.project() for call in message.tool_calls
        }
        for call in response.tool_calls:
            if not isinstance(call.id, str) or not call.id or call.id in seen_calls:
                raise ModelError("invalid", "Duplicate or invalid tool call identifier")
            if (
                not isinstance(call.name, str)
                or not call.name
                or not isinstance(call.arguments, dict)
            ):
                raise ModelError("invalid", "Invalid tool invocation")
            seen_calls.add(call.id)
        agent.session.add_message(
            Message(
                "assistant",
                response.content,
                source="model",
                tool_calls=tuple(response.tool_calls),
                provider_data=response.provider_data,
            )
        )
        if response.tool_calls:
            await run_tool_step(agent, response, ctx)
            continue
        final = response.content
        if agent.has_steering():
            continue
        if await agent.finish_step(ctx):
            continue
        state = (
            "completed"
            if response.stop_reason not in {"length", "max_tokens"}
            else "incomplete"
        )
        setattr(ctx, "turn_state", state)
        return final
    raise RuntimeError("Task step limit reached; session saved")


async def run_follow_up_loop(prompt, queued, run_turn, next_input, prepare_input):
    """Pi Agent 风格外层追问队列执行器 (Outer Follow-up Queue Loop)。

    【设计背景与因果一致性保障】
    对齐 Pi Agent 的外层排队准则：智能体在处理长程复杂任务时，用户可能会连续追加多个追问任务。
    若并发启动多个轮次，将导致底层 Session Tree 产生多个交叉并行的叶节点分支，
    造成工具副作用冲突与上下文撕裂。
    本调度器确保前序轮次完整结束（`run_turn` 成功返回并落盘）后，才通过 `next_input` 获取并
    准入下一个排队请求，从根本上维持因果链条的严格单调递增。

    【执行流程】
    1. 执行初始用户提示词（`await run_turn(prompt, queued)`），获取首轮答案；
    2. 循环检查追问队列中是否有排队项（`item := next_input()`）；
    3. 若有，通过 `prepare_input(item)` 异步准入并转换为输入文本；
    4. 顺序启动新轮次执行（`await run_turn(text, item)`）；
    5. 当队列排干后，返回最终轮次的执行结果。

    Args:
        prompt: 初始轮次的用户输入文本。
        queued: 初始轮次关联的队列项字典或数据。
        run_turn: 轮次执行协程回调 `async def run_turn(text, item) -> str`。
        next_input: 从待决队列获取下一个输入项的同步或无参回调 `def next_input() -> Any`。
        prepare_input: 准备并转换输入项的协程回调 `async def prepare_input(item) -> str`。

    Returns:
        str: 整个追问任务序列中最后一轮执行完成返回的文本。
    """
    answer = await run_turn(prompt, queued)
    while (item := next_input()) is not None:
        text = await prepare_input(item)
        answer = await run_turn(text, item)
    return answer

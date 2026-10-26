"""专职 Hook 拦截点契约与 HookRegistry —— Agent 关键节点双向拦截干预与控制流门禁。

【架构定位】
本模块定义 Weyra 智能体在微内核运行期间的专职门禁拦截体系。
不同于只读、弱关联、单向广播的异步事件总线（EventBus），Hook 机制属于强一致、同步阻塞的“控制流拦截门禁”。
允许开发者与上层中间件在模型调用前 1ms、用户输入进入系统时、工具执行前以及工具结果产生后进行双向拦截、
入参合法性校验、敏感信息脱敏改写甚至直接物理阻断。

【设计思想与开源血统】
1. 深度借鉴 Pi Agent (packages/agent/src/agent.ts):
   - 五大专职 Hook 拦截契约（*Hook）：独立于业务事件，专职负责控制流门禁，类型安全且语义精准；
   - 统一干预结果模型（HookResult）：通过 `block`、`reason` 以及具体的 `updated_*` 属性向状态机指示处置策略；
   - 顺序组合执行与深度拷贝隔离：拦截器按注册顺序流水线串联，每次传递深拷贝副本，防御中间件并发篡改。
2. 借鉴 DeepSeek-Harness 的防脆弱安全设计 (Fail-Closed Safety):
   - 工具调用异常闭环熔断：当 `ToolCallHook` 自身执行发生未捕获异常时，自动回退至 Fail-Closed 安全闭环策略，
     主动将 `block` 置为 True 并记录审计日志，绝不放行未经审批通过的高危工具操作。

【核心不变性与安全约束】
- 异常隔离与安全降级：非工具调用的普通 Hook 发生内部异常时，仅记录日志并跳过，绝不击垮核心推理循环；
  但针对高危工具调用的 `ToolCallHook` 发生异常时，严格执行 Fail-Closed 阻断。
"""

from __future__ import annotations

import contextlib
import copy
import inspect
import logging
from dataclasses import dataclass, replace
from typing import Any

from weyra.contracts import Message

logger = logging.getLogger(__name__)


# ── 五大专职 Hook 拦截点契约（独立门禁系统，非 Event）
@dataclass(frozen=True)
class UserInputHook:
    """Hook 1 (input): 拦截或改写用户原始输入文本。

    Attributes:
        input_text (str): 用户提交的原始文本内容。
    """
    input_text: str


@dataclass(frozen=True)
class AgentStartHook:
    """Hook 2 (before_agent_start): 拦截启动或动态重写 system_prompt。

    Attributes:
        system_prompt (str): 当前准备装配的全局系统提示词。
    """
    system_prompt: str


@dataclass(frozen=True)
class BeforeModelCallHook:
    """Hook 3 (context): 调模型前 1ms 审查或临时改写发送视图。

    Attributes:
        messages (list[Message]): 即将送入大模型的标准化消息序列。
        iteration (int): 当前轮次内的迭代步数序号。
    """
    messages: list[Message]
    iteration: int


@dataclass(frozen=True)
class ToolCallHook:
    """Hook 4 (tool_call): 工具执行前安全审批、阻断高危命令或改写入参。

    Attributes:
        tool_call_id (str): 工具调用全局唯一标识符。
        tool_name (str): 目标工具名称。
        args (dict[str, Any]): 即将传递给工具的执行参数字典。
    """
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResultHook:
    """Hook 5 (tool_result): 工具执行后改写返回内容或篡改报错状态。

    Attributes:
        tool_call_id (str): 工具调用全局唯一标识符。
        tool_name (str): 目标工具名称。
        result (str): 工具原始输出结果字符串。
        is_error (bool): 工具是否执行出错。
    """
    tool_call_id: str
    tool_name: str
    result: str
    is_error: bool


@dataclass(frozen=True)
class HookResult:
    """Hook 统一干预与改写结果信封 (Hook Intervention Result Envelope)。

    Attributes:
        block (bool): 是否显式阻断后续执行，默认为 False。
        reason (str | None): 阻断或介入的业务原因说明。
        updated_input (str | None): 改写后的用户输入文本。
        updated_system_prompt (str | None): 改写后的系统提示词。
        updated_messages (list[Message] | None): 改写后的消息实体列表。
        updated_args (dict[str, Any] | None): 改写后的工具入参字典。
        updated_result (str | None): 改写后的工具执行结果。
        terminate (bool | None): 是否提前终止当前轮次。
    """
    block: bool = False
    reason: str | None = None
    updated_input: str | None = None
    updated_system_prompt: str | None = None
    updated_messages: list[Message] | None = None
    updated_args: dict[str, Any] | None = None
    updated_result: str | None = None
    terminate: bool | None = None


class HookRegistry:
    """按序组合执行的全局 Hook 拦截注册中心 (Ordered Compositional Hook Registry)。

    【设计模式与安全规范】
    负责统一管理五大专职 Hook 的挂载与流水线派发。
    支持同步与异步混合回调，对工具调用异常实行 Fail-Closed 强制阻断，协作式取消信号正常透传。
    """

    def __init__(self):
        """初始化空的 Hook 回调映射字典。"""
        self._handlers = {}

    def register(self, hook_cls, callback):
        """注册指定 Hook 类型的拦截回调函数。

        Args:
            hook_cls: 目标 Hook 类型类对象（必须为五大专职 Hook 之一）。
            callback: 同步或异步回调函数 `callback(payload) -> HookResult | None`。

        Raises:
            TypeError: 当 Hook 类型不被支持或回调不可调用时抛出。
        """
        if hook_cls not in {UserInputHook, AgentStartHook, BeforeModelCallHook, ToolCallHook, ToolResultHook}:
            raise TypeError("Unsupported hook type")
        if not callable(callback):
            raise TypeError("Hook callback must be callable")
        self._handlers.setdefault(hook_cls, []).append(callback)

    def unregister(self, hook_cls, callback):
        """注销指定 Hook 类型的拦截回调函数。

        Args:
            hook_cls: 目标 Hook 类型类对象。
            callback: 之前已注册的回调函数对象。
        """
        with contextlib.suppress(ValueError):
            self._handlers.get(hook_cls, []).remove(callback)

    async def emit(self, payload):
        """按注册顺序流水线派发 Hook 载荷，收集阻断信号与累积字段改写。

        【执行流程】
        1. 根据载荷类型映射目标属性字段；
        2. 遍历已注册的回调列表，每次传递当前累积改写后的深拷贝对象；
        3. 若回调返回 `HookResult.block=True`，立即中止流水线并返回阻断结果；
        4. 若回调提供了更新值，类型强校验后更新流水线累积状态；
        5. 若回调发生非预期异常：
           - 对 `ToolCallHook` 立即启动 Fail-Closed 机制，返回阻断结果；
           - 对其他 Hook 记录错误日志并继续执行后续流程。

        Args:
            payload: 派发的 Hook 数据实例（如 UserInputHook, BeforeModelCallHook 等）。

        Returns:
            HookResult | None: 最终生效的干预结果对象；若没有任何介入或改写则返回 None。

        Raises:
            TypeError: 当回调返回非法类型或改写值类型不匹配时抛出。
        """
        fields = {
            UserInputHook: ("updated_input", "input_text", str),
            AgentStartHook: ("updated_system_prompt", "system_prompt", str),
            BeforeModelCallHook: ("updated_messages", "messages", list),
            ToolCallHook: ("updated_args", "args", dict),
            ToolResultHook: ("updated_result", "result", str),
        }
        result_field, payload_field, expected = fields[type(payload)]
        current = copy.deepcopy(payload)
        changed = False
        for callback in tuple(self._handlers.get(type(payload), ())):
            try:
                result = callback(copy.deepcopy(current))
                if inspect.isawaitable(result):
                    result = await result
                if result is None:
                    continue
                if not isinstance(result, HookResult):
                    raise TypeError("Hook must return HookResult or None")
                if result.block and not isinstance(payload, ToolResultHook):
                    return HookResult(block=True, reason=result.reason)
                value = getattr(result, result_field)
                if value is not None:
                    if not isinstance(value, expected):
                        raise TypeError("Invalid hook transformation")
                    if isinstance(payload, BeforeModelCallHook) and any(not isinstance(m, Message) for m in value):
                        raise TypeError("Expected Message instances")
                    current = replace(current, **{payload_field: copy.deepcopy(value)})
                    changed = True
            except Exception:
                logger.exception("Hook failed: %s", type(payload).__name__)
                if isinstance(payload, ToolCallHook):
                    return HookResult(block=True, reason="Tool call hook failed")
        return HookResult(**{result_field: getattr(current, payload_field)}) if changed else None

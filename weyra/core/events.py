"""智能体内核只读观察者事件模型 (Core Read-Only Event Notification)。

【架构定位】
本模块定义内核层轻量级事件载荷 `CoreEvent`。
严格作为执行循环对外部观察者的只读通知基元，与面向终端产品、带全局单调序号（seq）、
分代追踪（session_generation）和背压熔断机制的 `AppEvent`（位于 `weyra.events`）形成清晰分层。
使得内核循环在保持高吞吐、零锁竞争的前提下，安全向外部上报步进状态。

【设计思想与开源血统】
1. 深度借鉴 Pi Agent (packages/agent & dg-ai-notes 第 7 章 事件驱动 —— Agent的神经系统):
   - 只读事实通知：内核事件仅作为事实记录向外广播，外部观察者无法篡改核心数据流；
   - 内存深拷贝隔离：通过不可变信封隔绝外界变异，保障内核推理流水线的状态纯洁性。
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CoreEvent:
    """内核观察者事件信封 (Core Observer Event Envelope)。

    封装内核层执行事件类型与只读数据快照。

    Attributes:
        kind (str): 内核事件类型枚举字符串（如 'text', 'response', 'attempt_reset' 等）。
        data (Any): 关联的只读数据快照。
    """
    kind: str
    data: Any

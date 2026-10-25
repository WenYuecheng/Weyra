"""结构化应用事件体系与带背压防护的异步发布订阅总线（EventBus & Pub/Sub）。

架构定位与设计哲学:
    本模块为 Weyra 提供统一的异步事件广播基础设施。
    在现代 Agent 架构中，UI 呈现层（Textual TUI / CLI / Web）与底层 Agent 循环、工具执行、
    子代理委托以及后台技能演化完全解耦。
    传统的简单回调或无限缓冲队列在面对高速流式输出（Token Streaming）或慢速消费者时极易引发内存泄露与 UI 假死。
    本模块借鉴现代分布式事件驱动架构，构建了具备单调自增序号、全维度因果追踪（Causation Tracing）
    以及双重容量/字节背压熔断（Backpressure & Resync Protection）的企业级事件总线。

开源框架借鉴与设计原则:
    1. DeepSeek-Harness (packages/core/src/events.ts & packages/api):
       - 借鉴其统一事件信封规范（SessionEventMap Envelope）：每个事件携带形式化的 `schema_version`、
         单调自增序号 `seq`、会话代数 `session_generation`、会话 ID `session_id` 以及细粒度因果追踪标识
         （turn_id, request_id, attempt_id），确保客户端能够实现幂等消费与严格按序重建。
       - 借鉴其背压溢出与全量快照重同步机制（Subscription Overflow & Resync）：
         每个订阅通道设置硬性队列槽位上限（max_items=2048）与内存字节上限（max_bytes=8MiB）；
         一旦消费者消费迟滞触发溢出，立即标记 `needs_resync = True` 并终止丢弃新事件，强制客户端
         调用 `get_snapshot()` 拉取全量状态快照，彻底根除脏读与雪崩。
    2. PiAgent:
       - 轻量高效的弱关联事件解绑与排干（drain），支持 TUI 退出或轮次终止时的无阻塞平滑释放。
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AppEvent:
    """不可变的结构化领域事件信封（Event Envelope）。

    字段设计遵循 DeepSeek-Harness 工业级标准:
        - schema_version: 事件契约版本号（固定为 1）。
        - service_id: 发出事件的 WeyraAppService 实例唯一标识。
        - seq: 实例生命周期内严格全局单调递增的序号，用于客户端乱序检测与重排。
        - timestamp: 事件生成的 Unix 纪元时间戳（精确到微秒）。
        - session_generation: 会话代数，每次会话重启或切换递增，用于防御陈旧异步事件污染。
        - session_id / turn_id / request_id / attempt_id: 因果追踪链，定位具体执行轮次与重试尝试。
        - kind: 事件类型枚举标识（如 "state_changed", "turn_start", "tool_result" 等）。
        - data: 具体的业务载荷字典（执行深拷贝防御外部篡改）。
    """
    schema_version: int
    service_id: str
    seq: int
    timestamp: float
    session_generation: int
    session_id: str
    turn_id: str | None
    request_id: str | None
    attempt_id: str | None
    kind: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """将事件对象序列化为只读字典表示。"""
        return {
            "schema_version": self.schema_version,
            "service_id": self.service_id,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "session_generation": self.session_generation,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "kind": self.kind,
            "data": copy.deepcopy(self.data),
        }


class Subscription:
    """具备双重容量与字节数背压熔断保护的有界异步事件订阅通道。

    工程防御机制:
        - 双阈值保护：槽位上限 max_items（默认 2048）与内存上限 max_bytes（默认 8MB）。
        - 溢出熔断机制：一旦任一阈值越界，将 `needs_resync` 置为 True，向队列压入 None 哨兵，
          通知消费端放弃增量事件流并立即向服务端请求全量快照（StateSnapshot）。
    """

    def __init__(self, subscription_id: str, max_items: int = 2048, max_bytes: int = 8 * 1024 * 1024):
        """初始化具备双重容量与字节数背压熔断保护的有界异步事件订阅通道。

        【架构定位】
        作为 Application Service 与异步消费者（TUI / CLI / WebSocket）之间的缓冲通道。
        防止慢速客户端或突发 Token 流导致内存暴涨。

        【设计思想与开源血统】
        遵循 DeepSeek-Harness `packages/api` 订阅通道规范：
        设置硬性槽位上限 `max_items`（默认 2048 槽位）与物理内存体积上限 `max_bytes`（默认 8MB）。
        一旦任一阈值触发溢出，立即熔断置 `needs_resync = True` 并入队 None 唤醒消费者拉取全量快照。

        Args:
            subscription_id: 订阅通道全局唯一标识符。
            max_items: 队列最大允许积压的事件数量上限，默认 2048，不能小于 2。
            max_bytes: 队列最大允许累积的 UTF-8 序列化字节数上限，默认 8MB，必须大于 0。

        Raises:
            ValueError: 当槽位小于 2 或字节限制非正数时抛出。
        """
        self.id = subscription_id
        self.max_items = max_items
        self.max_bytes = max_bytes
        if max_items < 2 or max_bytes < 1:
            raise ValueError("Subscription requires at least two slots and positive byte limit")
        self.queue: asyncio.Queue[AppEvent | None] = asyncio.Queue(maxsize=max_items)
        self.needs_resync = False
        self.approx_bytes = 0
        self._closed = False

    def push(self, event: AppEvent) -> bool:
        """非阻塞式向通道压入新事件，若容量或字节数越界则触发背压熔断。

        【算法机制】
        1. 若通道已关闭或已处于熔断重同步态，立即放弃压入并返回 False；
        2. 计算当前事件 UTF-8 序列化字节大小；
        3. 若槽位即将耗尽（`qsize() >= max_items - 1`）或内存逼近上限，将 `needs_resync` 标记为 True，
           非阻塞压入 None 终止哨兵，通知消费端放弃增量事件流并拉取全量快照；
        4. 正常情况下将事件压入队列并累加 `approx_bytes`。

        Args:
            event: 待压入的不可变领域事件对象。

        Returns:
            bool: 是否成功入队。
        """
        if self._closed or self.needs_resync:
            return False
        payload_size = self._size(event)
        # 背压检测：如果槽位即将满或内存逼近限制，触发重同步保护
        if self.queue.qsize() >= self.max_items - 1 or (self.approx_bytes + payload_size) > self.max_bytes:
            self.needs_resync = True
            self.queue.put_nowait(None)
            return False
        try:
            self.queue.put_nowait(event)
            self.approx_bytes += payload_size
            return True
        except asyncio.QueueFull:
            self.needs_resync = True
            return False

    @staticmethod
    def _size(event: AppEvent) -> int:
        """估算单个事件 JSON 序列化后的 UTF-8 物理字节体积。

        Args:
            event: 目标领域事件信封。

        Returns:
            int: 序列化后的字节长度。
        """
        return len(json.dumps(event.to_dict(), ensure_ascii=False, default=str).encode("utf-8"))

    async def get(self) -> AppEvent | None:
        """异步出队等待下一个事件，并自动扣减内存占用计数。

        Returns:
            AppEvent | None: 出队的事件对象，若通道关闭或熔断则返回 None 哨兵。
        """
        event = await self.queue.get()
        if event is not None:
            self.approx_bytes = max(0, self.approx_bytes - self._size(event))
        return event

    def get_nowait(self) -> AppEvent | None:
        """非阻塞式尝试出队当前就绪事件。

        Returns:
            AppEvent | None: 就绪的事件对象，若队列为空则返回 None。
        """
        try:
            event = self.queue.get_nowait()
            if event is not None:
                self.approx_bytes = max(0, self.approx_bytes - self._size(event))
            return event
        except asyncio.QueueEmpty:
            return None

    def close(self) -> None:
        """安全关闭订阅通道并压入哨兵唤醒所有等待中的消费协程。"""
        self._closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


class EventBus:
    """线程安全、异步解耦的领域事件发布总线。

    维护全局单调递增序号 seq，管理多个订阅通道，并保留最近 200 条历史事件环形缓冲区。
    """

    def __init__(self, service_id: str):
        """初始化线程安全、异步解耦的领域事件发布总线。

        【架构定位】
        Weyra 系统级唯一的事件广播调度器。
        维护实例生命周期内严格单调递增的因果序号 `seq`，管理所有活跃的 Subscriber 通道，
        并保留固定长度的最近历史事件环形缓冲区（Ring Buffer）。

        Args:
            service_id: 归属的宿主 WeyraAppService 实例标识符。
        """
        self.service_id = service_id
        self._seq = 0
        self._subscribers: dict[str, Subscription] = {}
        self._lock = asyncio.Lock()
        self._history: list[AppEvent] = []
        self._max_history = 200

    @property
    def current_seq(self) -> int:
        """获取当前已派发的最大全局单调自增因果序号。

        Returns:
            int: 序号整数值。
        """
        return self._seq

    def create_event(
        self,
        session_id: str,
        session_generation: int,
        kind: str,
        data: dict[str, Any],
        *,
        turn_id: str | None = None,
        request_id: str | None = None,
        attempt_id: str | None = None,
    ) -> AppEvent:
        """创建附带严格单调递增 seq 与因果追踪标识的新事件信封对象。

        Args:
            session_id: 归属的会话全局唯一 ID。
            session_generation: 会话代数计数器（防御异步幽灵事件）。
            kind: 领域事件类型标识（如 "state_changed", "tool_start" 等）。
            data: 业务载荷字典（执行深拷贝防御外部数据篡改）。
            turn_id: 可选的归属轮次 ID。
            request_id: 可选的客户端请求 ID。
            attempt_id: 可选的重试尝试 ID。

        Returns:
            AppEvent: 结构化不可变领域事件信封。
        """
        self._seq += 1
        return AppEvent(
            schema_version=1,
            service_id=self.service_id,
            seq=self._seq,
            timestamp=time.time(),
            session_generation=session_generation,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            attempt_id=attempt_id,
            kind=kind,
            data=copy.deepcopy(data),
        )

    def publish(self, event: AppEvent) -> None:
        """向所有活跃订阅者广播事件，并自动清理已关闭或断开的死连接。

        【执行流程】
        1. 追加至最近 200 条历史事件环形缓冲区；
        2. 遍历所有注册通道，若通道已关闭则标记移除，否则调用 `sub.push(event)` 广播；
        3. 批量注销所有死连接通道。

        Args:
            event: 待广播的领域事件对象。
        """
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        dead = []
        for sub_id, sub in self._subscribers.items():
            if sub._closed:
                dead.append(sub_id)
            else:
                sub.push(event)
        for sub_id in dead:
            self._subscribers.pop(sub_id, None)

    def subscribe(self, subscription_id: str) -> Subscription:
        """注册一个新的有界事件订阅通道（若已存在同名 ID 则替换并关闭旧通道）。

        Args:
            subscription_id: 订阅者全局唯一标识符。

        Returns:
            Subscription: 初始化完成的有界订阅通道实例。
        """
        self.unsubscribe(subscription_id)
        sub = Subscription(subscription_id)
        self._subscribers[subscription_id] = sub
        return sub

    def unsubscribe(self, subscription_id: str) -> None:
        """注销并安全关闭指定订阅通道。

        Args:
            subscription_id: 目标订阅通道 ID。
        """
        sub = self._subscribers.pop(subscription_id, None)
        if sub:
            sub.close()

    def close(self) -> None:
        """销毁事件总线，级联关闭并清空所有存量订阅者。"""
        for sub in self._subscribers.values():
            sub.close()
        self._subscribers.clear()


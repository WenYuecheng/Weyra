"""智能体有状态执行外壳与事件总线挂载点 (Stateful Execution Harness & Shell)。

【架构定位】
本模块封装纯函数式的 `run_agent_loop` 循环，提供有状态的生命周期外壳 `AgentHarness`。
对应 Pi Agent (`packages/agent/`) 的核心 Agent 外壳设计，负责托管全局 Hook 注册表（HookRegistry）、
运行态并发排他锁存器（active_task / running）、事件观察者发布订阅列表，
以及支持协同式异步任务取消（abort）。

【设计思想与开源血统】
1. 深度借鉴 Pi Agent (packages/agent & dg-ai-notes 第 3、7 章):
   - 执行外壳分层解耦：将无状态的单轮循环函数与有状态的钩子注册、观察者回调相解耦；
   - 防并发重入屏障：严格校验 `if self.active_task is not None and self.active_task is not task:`，
     禁止单个智能体实例在未完成前序轮次时并发执行新轮次，彻底杜绝状态机混乱；
   - 观察者释放模式 (Disposer Pattern)：`subscribe` 方法返回可调用的 `unsubscribe` 闭包函数，
     支持组件卸载时优雅自动解绑。
2. 深度借鉴 DeepSeek-Harness (packages/core):
   - 协同式软取消传播（Cooperative Cancellation）：通过 `abort()` 触发底层 `asyncio.Task.cancel()`，
     使执行循环在下一个步进点安全退出并保存断点。

【核心不变性与安全约束】
- 单任务排他性：单个 Harness 实例同一时刻物理禁止被两个并发 asyncio 协程运行。
"""

import asyncio
import inspect
import copy
import logging
from .hooks import HookRegistry
from .loop import run_agent_loop
from .events import CoreEvent


class AgentHarness:
    """智能体内核执行控制器 (Agent Core Harness & Lifecycle Controller)。

    【职责定位】
    协调核心循环调度、全局钩子挂载与状态观察者事件派发。
    管理当前活跃轮次任务生命周期与协同取消。

    Attributes:
        hooks (HookRegistry): 全局 Hook 注册表实例。
        running (bool): 当前是否正处于活跃轮次运行中。
        active_task (asyncio.Task | None): 当前正在执行的底层 asyncio Task 引用。
    """

    def __init__(self):
        """初始化空的执行控制器实例，创建 HookRegistry 与观察者监听列表。"""
        self.hooks = HookRegistry()
        self.running = False
        self.active_task = None
        self._subscribers = []

    def subscribe(self, callback):
        """注册状态观察者回调函数，返回符合 Disposer 模式的注销闭包。

        Args:
            callback: 接受 `CoreEvent` 参数的同步或异步回调函数。

        Returns:
            Callable[[], None]: 调用即可取消订阅的注销函数。
        """
        self._subscribers.append(callback)

        def unsubscribe():
            """注销观察者订阅（Disposer 闭包模式）。"""
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    async def emit(self, kind: str, data):
        """向所有已注册的观察者派发内核事件，深拷贝隔离数据防篡改。

        Args:
            kind: 事件类型标识（如 'text', 'response' 等）。
            data: 关联的业务数据载荷。
        """
        for callback in tuple(self._subscribers):
            try:
                result = callback(CoreEvent(kind, copy.deepcopy(data)))
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logging.getLogger(__name__).exception("Core observer failed")

    def abort(self):
        """向当前正在执行的轮次协程发送协同取消信号。"""
        if self.active_task:
            self.active_task.cancel()

    async def run_turn(self, host, context):
        """单轮次执行入口，包含防并发重入守卫与异常清理。

        【执行流程】
        1. 检查当前是否已有异构异步任务正在运行，若有则抛出 RuntimeError 拒绝重入；
        2. 登记当前协程为 `self.active_task`，标记 `self.running = True`；
        3. 调用底层纯函数循环 `run_agent_loop(host, context)`；
        4. 在 finally 块中原子清理运行状态与任务句柄。

        Args:
            host: 遵循 LoopHost 契约的应用层宿主实例。
            context: 当前轮次的执行上下文 RunContext 实例。

        Returns:
            str: 最终回答文本；执行终止状态保存在 context.turn_state。

        Raises:
            RuntimeError: 当检测到多个任务并发调用该 Harness 时抛出。
        """
        task = asyncio.current_task()
        if self.active_task is not None and self.active_task is not task:
            raise RuntimeError("Agent harness is already running")
        owns_run = self.active_task is None
        if owns_run:
            self.active_task, self.running = task, True
        try:
            return await run_agent_loop(host, context)
        finally:
            if owns_run:
                self.active_task, self.running = None, False

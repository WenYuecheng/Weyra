"""运行时执行上下文、预算闭环防御与目标自治追踪状态机 (Runtime State & Budget Ledger)。

【架构定位】
本模块负责 Weyra 智能体在轮次执行中的状态协调与资源安全隔离。
通过统一的执行上下文（RunContext）、严格的 Fail-Closed 预算锁存器（Budget）
以及防死循环的目标自主推进器（Goal），确保多步骤自主运行过程既具备目标驱动力，
又处于严格的成本与资源可控边界内。

【开源与工程设计借鉴】
1. 借鉴 DeepSeek-Harness (`packages/guard/`, `packages/core/`) 的 Fail-Closed 预算闭环防御机制 (Fail-Closed Budget Guard)：
   - 资金与 Token 双维度限流：请求物理发出前必须先在 `reserve` 中预扣保守上限，额度耗尽立即抛出 `BudgetExceeded` 熔断，
     杜绝任何可能导致账单透支的模型调用；
   - 会话持久化断点恢复：在智能体启动或崩溃重启时通过 `attach` 从 SessionStore 事件历史中回放恢复已挂起预扣额，
     彻底杜绝通过反复重启进程绕过预算限制的安全漏洞；
   - 细粒度子作用域配额隔离：通过 `set_scope_limit` 支持对特定子智能体或委派任务设定独立的 Token 预算硬上限；
2. 借鉴 Pi Agent (`packages/agent/`) 的执行上下文模型 (`RunContext`)：
   - 包含多层级取消事件（`cancel_event`, `parent_cancel`）、生命周期事件派发总线（`emit`）以及
     LIFO 顺序的资源清理栈（`register_resource` / `close`），确保文件句柄与网络连接安全释放；
3. 借鉴 DeepSeek-Harness (`packages/goal/`, `packages/todo/`) 的目标自主推进器与死循环熔断机制 (`Goal`):
   - 维护待办事项（`todos`）状态机，在模型回复后自动对比工具执行哈希进展（`progress`）；
   - 内置死循环防熔断算法：单目标最大自发继续 3 轮，若连续 2 轮无实质产物进展（`stagnant_rounds >= 2`）强制终止。
"""

from __future__ import annotations

import asyncio
import inspect
import copy
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from weyra.contracts import new_id


class BudgetExceeded(RuntimeError):
    """预算耗尽异常，当 Token 数量、模型调用次数或人民币金额超出配置限额时抛出。"""
    pass


class Budget:
    """会话根预算账本 (Session Root Budget Ledger)。

    负责资金（CNY）与 Token 的事前预扣（reserve）、事后核销（settle）以及事件检查点持久化。
    """

    def __init__(
        self, token_limit=None, call_limit=None, maintenance_limit=None, *, cost_limit_cny=None
    ):
        """初始化会话级预算账本 (Initialize Session Budget Ledger)。

        【设计思想与开源血统】
        遵循 DeepSeek-Harness (`packages/guard/`) 的 Fail-Closed 原则：
        支持 Token 预算上限、模型调用物理次数上限、维护任务独立配额（压缩/记忆）以及人民币金额硬上限。
        任何上限参数均要求为非负数或 None，保证账本初始化的确定性。

        Args:
            token_limit: 会话全局允许消耗的最大 Token 总数。
            call_limit: 会话全局允许调用的最大模型物理次数。
            maintenance_limit: 维护类任务（如上下文压缩、记忆管理、技能自演化）专用 Token 配额上限。
            cost_limit_cny: 会话全局允许消耗的人民币金额上限 (Decimal)。

        Raises:
            ValueError: 当传入的限额为负数或非法布尔值时抛出。
        """
        for value in (token_limit, call_limit, maintenance_limit):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError("Budget limits cannot be negative")
        self.token_limit, self.call_limit, self.maintenance_limit = (
            token_limit,
            call_limit,
            maintenance_limit,
        )
        self.cost_limit_cny = self._money(cost_limit_cny) if cost_limit_cny is not None else None
        self.total_tokens = self.calls = self.unknown_calls = self._maintenance = 0
        self.total_cost_cny = Decimal(0)
        self.records, self._pending, self._scopes = [], {}, {}
        self._lock, self._journal = asyncio.Lock(), None

    @staticmethod
    def _money(value):
        """格式化并校验高精度人民币金额 (Validate & Parse Decimal CNY Amount)。

        【安全设计】
        采用金融级 `Decimal` 运算，拒绝 NaN/Infinity 等非有限数值及负数金额。

        Args:
            value: 拟转换的金额数值或字符串。

        Returns:
            Decimal: 校验合规的非负有限 Decimal 金额。

        Raises:
            ValueError: 当输入无法解析为合规金额时抛出。
        """
        try:
            result = Decimal(str(value))
        except (ValueError, InvalidOperation):
            raise ValueError("Cost must be a finite nonnegative CNY amount") from None
        if not result.is_finite() or result < 0:
            raise ValueError("Cost must be a finite nonnegative CNY amount")
        return result

    def set_scope_limit(self, scope, token_limit):
        """为特定子作用域设置独立的 Token 预算硬上限 (Set Task Sub-Scope Token Limit)。

        【多代理资源隔离】
        借鉴 DeepSeek-Harness 子代理沙箱配额隔离规范，防止单一子任务（如搜索或代码审查）耗尽全局预算。

        Args:
            scope: 子作用域唯一标识（如子任务 ID 或代理角色）。
            token_limit: 允许该作用域消耗的最大 Token 上限。

        Raises:
            ValueError: 当配额为负数时抛出。
        """
        if token_limit is not None and token_limit < 0:
            raise ValueError("Task limit cannot be negative")
        self._scopes.setdefault(scope, {"limit": token_limit, "tokens": 0})["limit"] = token_limit

    def attach(self, session):
        """挂载持久化会话并从崩溃检查点恢复预算账本 (Attach Session & Replay Budget State)。

        【崩溃一致性与防绕过】
        借鉴 DeepSeek-Harness 预算恢复机制：
        从 SessionStore 历史事件中回溯最新的 `budget_checkpoint` 与所有的 `budget_settlement`，
        将上次进程异常退出时已挂起但未核销的 `pending` 预扣额全额重放计入总账，
        杜绝通过反复重启进程或刷新会话绕过预算上限的安全漏洞。

        Args:
            session: 底层持久化会话实例 (SessionStore)。
        """
        if self._journal is not None:
            return
        state = next(
            (r["data"] for r in reversed(session.records) if r["kind"] == "budget_checkpoint"), None
        )
        self.records = [r["data"] for r in session.records if r["kind"] == "budget_settlement"]
        if state:
            pending = state.get("pending", {})
            if isinstance(pending, list):
                pending = {
                    str(i): {"tokens": v[0], "purpose": v[1], "cost": "0", "scope": None}
                    for i, v in enumerate(pending)
                }
            self.total_tokens = state["total_tokens"] + sum(v["tokens"] for v in pending.values())
            self.calls = state["calls"]
            self.unknown_calls = state["unknown_calls"] + len(pending)
            self._maintenance = state.get("maintenance_tokens", 0) + sum(
                v["tokens"]
                for v in pending.values()
                if v["purpose"] in {"compression", "memory", "evolution", "maintenance"}
            )
            self.total_cost_cny = Decimal(state.get("total_cost_cny", "0")) + sum(
                (Decimal(v.get("cost", "0")) for v in pending.values()), Decimal(0)
            )
            self._scopes = state.get("scopes", {})
            for value in pending.values():
                scope = value.get("scope")
                if scope in self._scopes:
                    self._scopes[scope]["tokens"] += value["tokens"]
        self._journal = session
        self._checkpoint()

    def _state(self):
        """导出当前预算账本的完整快照字典 (Export Budget State Snapshot)。

        Returns:
            dict: 包含总消耗、挂起额度、各作用域配额的快照字典。
        """
        return {
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "unknown_calls": self.unknown_calls,
            "maintenance_tokens": self._maintenance,
            "total_cost_cny": str(self.total_cost_cny),
            "pending": self._pending,
            "scopes": self._scopes,
        }

    def _checkpoint(self):
        """向挂载的持久化会话记录最新的预算快照事件 (Record Budget Checkpoint Event)。"""
        if self._journal is not None:
            self._journal.append("budget_checkpoint", self._state(), True)

    def _restore(self, state):
        """从快照字典中全量恢复内存预算状态 (Restore Budget State from Snapshot)。

        Args:
            state: 历史快照数据字典。
        """
        self.total_tokens, self.calls, self.unknown_calls = (
            state["total_tokens"],
            state["calls"],
            state["unknown_calls"],
        )
        self._maintenance = state["maintenance_tokens"]
        self.total_cost_cny = Decimal(state["total_cost_cny"])
        self._pending, self._scopes = state["pending"], state["scopes"]

    async def reserve(self, amount, purpose="main", *, estimated_cost_cny=None, scope=None):
        """预先申请并锁存指定额度的预算配额 (Reserve Budget Before Model Invocation)。

        【Fail-Closed 核心防御机制】
        在真实向供应商发出物理网络请求前，强制以最保守的 Token 预估与费用上限进行账面预扣：
        1. 全局 Token 检查：`total_tokens + pending + amount <= token_limit`；
        2. 物理调用次数检查：`calls < call_limit`；
        3. 资金总额检查：`total_cost_cny + held_cost + cost <= cost_limit_cny`；
        4. 维护任务配额检查：若为压缩/演化调用，受 `maintenance_limit` 约束；
        5. 子任务隔离配额检查：受对应 scope 的上限约束；
        6. 原子状态回滚：若写盘检查点失败，自动还原内存状态并抛出异常。

        Args:
            amount: 拟预扣的保守 Token 数量。
            purpose: 请求用途（如 "main", "compression", "memory"）。
            estimated_cost_cny: 预估的人民币开销。若配置了金额上限则此项为必填。
            scope: 可选的子作用域标识。

        Returns:
            str: 全局唯一的预扣凭证 (reservation ID)。

        Raises:
            ValueError: 当申请额度为负数时抛出。
            BudgetExceeded: 当任一维度的预算不足以支撑本次请求时抛出熔断异常。
        """
        if isinstance(amount, bool) or amount < 0:
            raise ValueError("Negative reservation")
        if self.cost_limit_cny is not None and estimated_cost_cny is None:
            raise BudgetExceeded("Verified CNY pricing is required before a paid request")
        cost = self._money(estimated_cost_cny or 0)
        async with self._lock:
            pending = sum(v["tokens"] for v in self._pending.values())
            held_cost = sum((Decimal(v["cost"]) for v in self._pending.values()), Decimal(0))
            if (
                self.token_limit is not None
                and self.total_tokens + pending + amount > self.token_limit
            ):
                raise BudgetExceeded("Session token budget exhausted")
            if self.call_limit is not None and self.calls >= self.call_limit:
                raise BudgetExceeded("Session model-call budget exhausted")
            if (
                self.cost_limit_cny is not None
                and self.total_cost_cny + held_cost + cost > self.cost_limit_cny
            ):
                raise BudgetExceeded("Session CNY budget exhausted")
            maintenance = purpose in {"compression", "memory", "evolution", "maintenance"}
            if maintenance and self.maintenance_limit is not None:
                held = sum(
                    v["tokens"]
                    for v in self._pending.values()
                    if v["purpose"] in {"compression", "memory", "evolution", "maintenance"}
                )
                if self._maintenance + held + amount > self.maintenance_limit:
                    raise BudgetExceeded("Maintenance allowance exhausted")
            local = self._scopes.get(scope)
            if local and local["limit"] is not None:
                held = sum(v["tokens"] for v in self._pending.values() if v.get("scope") == scope)
                if local["tokens"] + held + amount > local["limit"]:
                    raise BudgetExceeded("Task token budget exhausted")
            before = copy.deepcopy(self._state())
            reservation = new_id()
            self._pending[reservation] = {
                "tokens": amount,
                "purpose": purpose,
                "cost": str(cost),
                "scope": scope,
            }
            self.calls += 1
            try:
                self._checkpoint()
            except BaseException:
                self._restore(before)
                raise
            return reservation

    async def settle(self, reservation, actual, *, actual_cost_cny=None, cost_known=None):
        """根据供应商返回的真实 Usage 核销预算预扣额 (Settle Reserved Budget with Actual Usage)。

        【事务性核销与差额结算】
        在模型调用完成或失败后，将 `reserve` 预扣凭证转换为真实的 `budget_settlement` 事件：
        1. 从 `_pending` 中移出该凭证；
        2. 将实际发生的 Token 与金额计入已确认总账 (`total_tokens`, `total_cost_cny`)；
        3. 若供应商未返回具体 Token 消耗（如流式中断），则按预扣额保守入账并标记 `unknown_calls += 1`；
        4. 在底层 SessionStore 中开启事务，将结算记录与最新快照原子落盘。

        Args:
            reservation: 预扣凭证 ID。
            actual: 供应商实际返回的 Token 数量（若未知则传入 None）。
            actual_cost_cny: 实际产生的人民币开销（若已知）。
            cost_known: 费用是否确切知晓的布尔标记。

        Raises:
            ValueError: 当实际消耗为负数，或传入未知的预扣凭证时抛出。
        """
        if actual is not None and actual < 0:
            raise ValueError("Negative usage")
        async with self._lock:
            if reservation not in self._pending:
                if any(r["reservation"] == reservation for r in self.records):
                    return
                raise ValueError("Unknown reservation")
            before = copy.deepcopy(self._state())
            value = self._pending.pop(reservation)
            charged = value["tokens"] if actual is None else actual
            cost = (
                Decimal(value["cost"]) if actual_cost_cny is None else self._money(actual_cost_cny)
            )
            self.total_tokens += charged
            self.total_cost_cny += cost
            if value["purpose"] in {"compression", "memory", "evolution", "maintenance"}:
                self._maintenance += charged
            self.unknown_calls += actual is None
            scope = value.get("scope")
            if scope in self._scopes:
                self._scopes[scope]["tokens"] += charged
            record = {
                "reservation": reservation,
                "purpose": value["purpose"],
                "scope": scope,
                "tokens": actual,
                "accounted_tokens": charged,
                "cost_cny": str(cost),
                "cost_known": actual_cost_cny is not None if cost_known is None else cost_known,
            }
            try:
                if self._journal is not None:
                    with self._journal.transaction():
                        self._journal.append("budget_settlement", record, True)
                        self._checkpoint()
                else:
                    self._checkpoint()
            except BaseException:
                self._restore(before)
                raise
            self.records.append(record)

    @property
    def pending_tokens(self):
        """当前所有处于飞行中（In-Flight）尚未核销的预扣 Token 总量。"""
        return sum(value["tokens"] for value in self._pending.values())


@dataclass
class RunContext:
    """轮次执行上下文载荷对象 (Turn Run Context Object)。

    【设计思想与开源血统】
    严格对齐 Pi Agent (`packages/agent/`) 的上下文架构：
    封装了工程工作目录、会话持久化实例、预算账本、权限控制模式，
    以及多层级协作取消信号与 LIFO 资源清理栈。

    Attributes:
        project: 智能体操作的目标工程绝对路径。
        session: 会话持久化与状态树管理器 (SessionStore)。
        budget: 会话根预算账本 (Budget)。
        permission_mode: 权限控制模式（"default", "readonly", "bypass" 等）。
        id: 当前轮次唯一执行标识符。
        cancel_event: 当前轮次的协程取消事件通知对象。
        parent_cancel: 父级轮次或外部调度的级联取消事件对象。
        budget_scope: 当前轮次挂钩的子任务预算作用域标识。
        plan_path: 关联的工程规划文件路径（若处于规划模式）。
        services: 上下文挂载的扩展服务字典。
        read_versions: 文件读取版本缓存，用于乐观并发写冲突校验。
        depth: 递归委托或子智能体调用深度（根智能体为 0）。
        on_event: 运行时生命周期事件订阅回调函数。
        _resources: 需在轮次结束时释放的资源回调列表（按注册顺序存储）。
    """
    project: Path
    session: Any
    budget: Budget
    permission_mode: str = "default"
    id: str = field(default_factory=new_id)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    parent_cancel: asyncio.Event | None = None
    budget_scope: str | None = None
    plan_path: Path | None = None
    services: dict = field(default_factory=dict)
    read_versions: dict = field(default_factory=dict)
    depth: int = 0
    on_event: Any = None
    _resources: list = field(default_factory=list)

    def check_cancelled(self):
        """检查当前运行轮次或父级调用是否已被用户显式取消 (Check Turn Cancellation State)。

        Raises:
            asyncio.CancelledError: 当当前 cancel_event 或父级 parent_cancel 处于 set 状态时抛出。
        """
        if self.cancel_event.is_set() or (self.parent_cancel and self.parent_cancel.is_set()):
            raise asyncio.CancelledError

    async def emit(self, kind: str, data: Any = None):
        """向生命周期监听器派发运行时事件 (Emit Runtime Lifecycle Event)。

        Args:
            kind: 事件类型标识（如 "chunk", "tool_intent", "cleanup_errors"）。
            data: 携带的结构化事件数据载荷。
        """
        if self.on_event:
            result = self.on_event(kind, data)
            if inspect.isawaitable(result):
                await result

    def register_resource(self, cleanup):
        """注册需在轮次结束时确保释放的资源清理回调 (Register LIFO Cleanup Resource)。

        Args:
            cleanup: 同步或异步的无参清理函数（如关闭文件句柄、停止临时子进程）。
        """
        self._resources.append(cleanup)

    async def close(self):
        """以后进先出 (LIFO) 顺序执行所有已注册资源的逆序释放清理 (Execute LIFO Cleanup Stack)。

        【设计原则】
        模拟操作系统栈帧清理：捕获所有清理异常，保证所有资源均被尝试释放，
        若存在异常则汇总派发 `cleanup_errors` 事件。
        """
        errors = []
        for cleanup in reversed(self._resources):
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                errors.append(type(error).__name__)
        self._resources.clear()
        if errors:
            await self.emit("cleanup_errors", errors)


@dataclass
class Goal:
    """目标驱动的多轮自主演进推进器 (Autonomous Goal Round Driver)。

    【设计思想与开源血统】
    完全遵循 DeepSeek-Harness (`packages/goal/` 与 `packages/todo/`) 的自主推进设计：
    智能体在用户下发一个大目标后，通过待办列表（todos）与产物哈希（progress）进行自主循环。
    为防止大模型陷入自我说服的无限死循环，内置严格的推进断路器（Circuit Breaker）。

    Attributes:
        objective: 目标文字描述。
        revision: 目标修订版本号。
        todos: 结构化待办清单列表。
        state: 状态机状态（"active", "stopped", "completed"）。
        continuations: 已连续自主继续推进的轮数计数（上限 3 轮）。
        stagnant_rounds: 连续停滞无实质产物推进的轮数计数（连续 2 轮无进展即熔断）。
        progress: 阶段性工具产物或变更哈希摘要，用于检测是否发生停滞。
    """
    objective: str
    revision: int = 1
    todos: list[dict] = field(default_factory=list)
    state: str = "active"
    continuations: int = 0
    stagnant_rounds: int = 0
    progress: str = ""

    def revise(self, objective: str):
        """更新修正当前目标并重置自主演进计数器 (Revise Goal & Reset Stagnation Counters)。

        Args:
            objective: 新的目标文本描述。
        """
        self.objective = objective
        self.revision += 1
        self.continuations = 0
        self.stagnant_rounds = 0
        self.state = "active"

    def should_continue(self, progress: str) -> bool:
        """评估当前智能体是否应当自动启动下一轮自主推理 (Evaluate Autonomous Continuation Eligibility)。

        【死循环防御与熔断判定】
        1. 终态检查：若当前状态非 "active"，或所有待办事项已完成 (`done=True`)，立即返回 False；
        2. 停滞检测：对比当前 progress 与上次记录，若完全一致则递增 `stagnant_rounds`，反之清零；
        3. 熔断判断：若自主轮数超过 3 轮 (`continuations >= 3`)，或连续 2 轮无实质产物变更 (`stagnant_rounds >= 2`)，
           将状态置为 "stopped" 并返回 False，将控制权交还给用户；
        4. 推进许可：递增 `continuations` 计数并返回 True。

        Args:
            progress: 当前轮次执行后汇总的产物指纹或变更哈希。

        Returns:
            bool: 若允许继续执行下一轮自主迭代返回 True，否则返回 False。
        """
        if self.state != "active" or not any(not item.get("done") for item in self.todos):
            return False
        self.stagnant_rounds = self.stagnant_rounds + 1 if progress == self.progress else 0
        self.progress = progress
        if self.continuations >= 3 or self.stagnant_rounds >= 2:
            self.state = "stopped"
            return False
        self.continuations += 1
        return True

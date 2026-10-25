"""Weyra 核心应用服务层（Application Service Layer）。

【架构定位】
本模块遵循领域驱动设计（DDD）与整洁架构中的“应用服务层（Application Service）”模式。
将底层 Agent 核心执行循环、SQLite 会话状态机、工具审批流与模型调用，与上层任何具体的展示层
（包括交互式 CLI、基于 Textual 的 Rich TUI，以及未来的 Web/JSON-RPC 服务端）完全解耦。
作为整个系统唯一的中央状态协调者（Single Source of Truth），对外暴露高内聚的异步控制契约，
统一维护会话代数（session_generation）、幂等提交凭证（SubmissionReceipt）以及全量状态快照（StateSnapshot）。

【设计思想与开源血统】
1. 深度借鉴 DeepSeek-Harness (packages/api, packages/host, packages/interaction):
   - 应用服务网关规范：统一封装 `CommandResult` 结构化返回值与错误码体系，
     杜绝将底层异常直接穿透抛至 UI 导致界面崩溃。
   - 工业级幂等提交凭据体系（SubmissionReceipt & Idempotency Cache）：针对高并发客户端请求，
     基于 `request_id` 缓存处理结果，防止网络重试或用户连击导致多轮重复轮次并发。
   - 状态机单向流转与全量快照投影（StateSnapshot）：统一管理应用生命周期（starting, ready, closing, closed）
     与当前活动细粒度状态（idle, generating, executing_tools, compacting, maintaining, awaiting_approval, cancelling）。
   - 统一两阶段审批流协调中枢（_pending_approvals & resolve_approval）：工具执行拦截时生成审批请求卡片并广播事件，
     异步挂起执行协程直至收到用户或 API 客户端的显式许可。
2. 深度借鉴 Pi Agent (packages/coding-agent & dg-ai-notes 第 10 章 Session Tree DAG):
   - 会话分支切换与独立分叉（Session Tree DAG）：彻底消除传统平铺日志的破坏性覆盖缺陷，
     通过会话树 `leaf` 指针在历史节点间无缝移动实现分支切换（open_session），
     并通过溯源回溯提取完整祖先链派生独立子会话沙箱（new_session / fork），所有历史记录严格只追加、永不物理删除。
   - 三态输入调度模型（prompt, steering, followup）：根据当前服务忙闲状态与用户意图，
     自动判定是开启新轮次、向当前运行中轮次注入抢占式干预指令（steering），还是排入未决队列（followup）。
   - 级联优雅取消（cancel_turn）：向活跃任务发送取消信号，清空未决审批并协同回滚至确定性稳定点。
3. 参考 Hermes Agent:
   - 仅借鉴其配置文件与敏感凭据物理隔离理念，本服务层核心完全依托 Pi Agent 与 DeepSeek-Harness 状态机构建。

【核心不变性与安全约束】
- 状态机单向排他性：任何破坏性会话操作（切换会话、创建会话）必须断言当前活动处于 "idle" 态。
- 路径穿越防御：所有涉及工件读取（read_tool_output）强制执行目录相对边界校验。
"""

from __future__ import annotations

import asyncio
import copy
import shlex
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from .contracts import new_id
from .events import AppEvent, EventBus, Subscription
from .presentation import OutputPage, paginate_text, project_raw_session_records
from .runtime_state import Budget


@dataclass(frozen=True)
class CommandResult:
    """服务层命令执行标准化结构体契约。

    【架构定位】
    遵循 DeepSeek-Harness `packages/api` 规范，对所有服务层派发命令（包括斜杠指令、会话切换、审批裁决）
    提供类型安全、防崩溃的标准化返回信封，阻断底层异常穿透至 UI 展示层。

    Attributes:
        ok: 操作是否成功执行。
        code: 机器可识别的标准化状态码（如 "success", "invalid_input", "busy", "permission_denied"）。
        message: 详细的结果说明或错误摘要。
        data: 随结果返回的业务数据载荷。
    """
    ok: bool
    code: str
    message: str
    data: dict[str, Any] | list[Any] | str | None = None

    def to_dict(self) -> dict[str, Any]:
        """将命令执行结果深拷贝序列化为只读字典表示。

        Returns:
            dict[str, Any]: 包含 ok, code, message 及深拷贝 data 的结构化字典。
        """
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "data": copy.deepcopy(self.data),
        }


@dataclass(frozen=True)
class SubmissionReceipt:
    """用户输入提交的幂等性回执凭证。

    【架构定位】
    作为客户端与服务层异步交互的幂等令牌。当 UI 或远程客户端向 Agent 提交用户输入时，
    服务层立即返回该凭证，建立客户端请求 ID (`request_id`) 与底层执行模式的因果绑定。

    Attributes:
        accepted: 输入是否已被服务层接纳。
        request_id: 客户端提交时指定的全局唯一请求标识符。
        mode: 实际分流模式（"prompt", "steering", "followup"）。
        message: 提交处理状态描述。
    """
    accepted: bool
    request_id: str
    mode: str  # "prompt", "steering", "followup"
    message: str

    def to_dict(self) -> dict[str, Any]:
        """将幂等回执序列化为字典表示。

        Returns:
            dict[str, Any]: 包含 accepted, request_id, mode, message 的字典。
        """
        return asdict(self)


@dataclass(frozen=True)
class SessionSummary:
    """会话摘要元数据契约。

    Attributes:
        session_id: 会话全局唯一标识符。
        project: 所属工程根目录字符串。
        created: 会话创建 ISO 时间戳。
        updated: 会话最后更新 ISO 时间戳。
        message_count: 累计记录条数。
        leaf: 会话树当前活跃叶节点 ID。
    """
    session_id: str
    project: str
    created: str
    updated: str
    message_count: int
    leaf: str | None


@dataclass(frozen=True)
class HistoryPage:
    """历史记录展示卡片分页数据。

    Attributes:
        session_id: 目标会话标识符。
        cards: 投影生成的 DisplayCard 字典列表。
        total_records: 当前有效历史记录总数。
        has_more: 是否存在更多分页记录。
    """
    session_id: str
    cards: list[dict[str, Any]]
    total_records: int
    has_more: bool


@dataclass
class StateSnapshot:
    """系统全局全量状态快照（StateSnapshot）。

    【架构定位】
    作为客户端首次连接、会话切换或事件订阅因背压发生溢出（needs_resync=True）时的全量状态重建基线。
    遵循 DeepSeek-Harness 客户端单向数据流原则。

    Attributes:
        schema_version: 快照协议版本号（固定为 1）。
        service_id: 发出快照的服务实例 ID。
        through_seq: 截止生成此快照时已派发的事件序号 seq。
        session_generation: 会话代数计数器。
        project: 项目路径与名称字典。
        session: 会话树状态（ID、leaf、depth）。
        routes: 模型路由表。
        permission_mode: 当前工具权限模式。
        lifecycle: 宿主生命周期状态。
        activity: 细粒度活动状态。
        budget: 预算消耗与未决成本账本。
        pending_inputs: 引导与追问未决队列快照。
        pending_approvals: 待决审批卡片列表。
        background: 后台任务统计。
        last_outcome: 上一轮退出状态。
    """
    schema_version: int
    service_id: str
    through_seq: int
    session_generation: int
    project: dict[str, str]
    session: dict[str, Any]
    routes: dict[str, Any]
    permission_mode: str
    lifecycle: str  # "starting", "ready", "closing", "closed", "error"
    activity: str  # "closing", "cancelling", "awaiting_approval", "compacting", "executing_tools", "generating", "maintaining", "idle"
    budget: dict[str, Any]
    pending_inputs: list[dict[str, Any]]
    pending_approvals: list[dict[str, Any]]
    background: dict[str, Any]
    last_outcome: str | None

    def to_dict(self) -> dict[str, Any]:
        """将状态快照深拷贝序列化为只读字典表示。

        Returns:
            dict[str, Any]: 全量快照字典。
        """
        return {
            "schema_version": self.schema_version,
            "service_id": self.service_id,
            "through_seq": self.through_seq,
            "session_generation": self.session_generation,
            "project": copy.deepcopy(self.project),
            "session": copy.deepcopy(self.session),
            "routes": copy.deepcopy(self.routes),
            "permission_mode": self.permission_mode,
            "lifecycle": self.lifecycle,
            "activity": self.activity,
            "budget": copy.deepcopy(self.budget),
            "pending_inputs": copy.deepcopy(self.pending_inputs),
            "pending_approvals": copy.deepcopy(self.pending_approvals),
            "background": copy.deepcopy(self.background),
            "last_outcome": self.last_outcome,
        }


class WeyraAppService:
    """Weyra 核心应用协调服务（Core Application Service Coordinator）。

    【架构定位】
    本类是整洁架构中 Application Service 层的核心实体，统一管控底层 Agent 执行、模型运行时生命周期、
    双队列调度（steering / follow_up）、两阶段工具审批流与因果事件总线。
    对外提供无状态展示层所需的幂等提交与快照同步机制。
    """

    def __init__(
        self,
        project: Path | str,
        runtime_factory=None,
        *,
        session_id: str | None = None,
        permission_mode: str = "default",
        max_steps: int = 40,
        task_token_limit: int | None = None,
        enable_memory: bool = True,
        enable_evolution: bool | None = None,
        budget_limit_tokens: int = 200000,
        budget_limit_calls: int = 50,
        budget_limit_cost_cny: str | None = None,
        maintenance_limit: int = 20000,
    ):
        """初始化 Weyra 核心应用协调服务实例。

        【架构定位】
        构建系统唯一的应用服务中枢，装配事件总线（EventBus）、审批协调器（Approval Manager）、
        幂等性缓存（Idempotency Cache）与活动状态机。

        【设计思想与开源血统】
        深度对齐 DeepSeek-Harness `packages/host` 与 Pi Agent 应用服务网关模式，提供单一事实真相（Single Source of Truth）。

        Args:
            project: 目标工作区工程根目录路径。
            runtime_factory: 可选的模型运行时构建工厂函数，为 None 时通过 load_config 延迟加载。
            session_id: 初始挂载的目标会话标识符，若为 None 则默认推导。
            permission_mode: 初始工具执行权限模式（"default", "plan", "dontAsk"）。
            max_steps: 单轮自主推理的最大交互步数上限，默认 40 步。
            task_token_limit: 单任务作用域的 Token 消耗上限防护阈值。
            enable_memory: 是否激活长期持久化记忆子系统。
            enable_evolution: 是否激活技能演化引擎（WikiSkill/SkillOpt）。
            budget_limit_tokens: 会话级别全局 Token 预算硬性上限，默认 200,000。
            budget_limit_calls: 会话级别大模型 API 调用次数上限，默认 50 次。
            budget_limit_cost_cny: 会话级别人民币计价财务熔断限额。
            maintenance_limit: 后台维护与记忆提取的 Token 配额预算。
        """
        self.project_path = Path(project).resolve()
        self.service_id = f"srv_{uuid.uuid4().hex[:12]}"
        self.session_generation = 1
        self.lifecycle = "starting"
        self._runtime_factory = runtime_factory
        self._initial_session_id = session_id
        self.permission_mode = permission_mode
        self.max_steps = max_steps
        self.task_token_limit = task_token_limit
        self.enable_memory = enable_memory
        self.enable_evolution = enable_evolution
        self.budget_limit_tokens = budget_limit_tokens
        self.budget_limit_calls = budget_limit_calls
        self.budget_limit_cost_cny = budget_limit_cost_cny
        self.maintenance_limit = maintenance_limit

        self.events = EventBus(self.service_id)
        self.runtime = None
        self.agent = None

        # Approval coordination
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        # Client idempotency cache for current service lifecycle
        self._processed_submissions: dict[str, SubmissionReceipt] = {}
        self._submission_modes: dict[str, str] = {}
        self._submission_texts: dict[str, str] = {}
        # Active turn execution task
        self._active_task: asyncio.Task | None = None
        self._last_outcome: str | None = None
        self._active_tools: dict[str, dict[str, Any]] = {}
        self._is_compacting = False
        self._is_cancelling = False
        self._is_generating = False
        self._stream_text = ""
        self._stream_thinking = ""

    @property
    def session_id(self) -> str:
        """获取当前活跃会话的全局唯一标识符。

        Returns:
            str: 活跃会话 ID 字符串，若底层会话未就绪则回退至初始会话 ID 或 "default"。
        """
        if self.agent and self.agent.session:
            return getattr(self.agent.session, "session_id", "default")
        return self._initial_session_id or "default"

    async def start(self) -> StateSnapshot:
        """启动应用服务，初始化模型运行时并构建 CodingAgent 宿主。

        【架构定位】
        驱动服务生命周期由 "starting" 迁移至 "ready" 稳定就绪态。

        【核心不变性与安全约束】
        幂等保护：若当前生命周期已处于 "ready" 态，直接返回当前状态快照，杜绝重复初始化。

        Returns:
            StateSnapshot: 系统就绪态的全量状态快照。
        """
        if self.lifecycle == "ready":
            return await self.get_snapshot()

        if self._runtime_factory is not None:
            self.runtime = self._runtime_factory()
        else:
            from .config import load_config

            self.runtime = load_config(self.project_path).build_runtime()

        await self._init_agent(self._initial_session_id)
        self.lifecycle = "ready"
        snapshot = await self.get_snapshot()
        self._emit_event("state_changed", snapshot.to_dict())
        return snapshot

    async def _init_agent(self, session_id: str | None) -> None:
        """实例化底层 CodingAgent 并绑定审批流与事件转发闭包。

        【执行流程】
        1. 组装异步审批回调 `service_confirm`，路由至 `_handle_approval_request`；
        2. 组装领域事件回调 `service_events`，路由至 `_handle_agent_event`；
        3. 初始化带有金融级限额的 Budget 预算账本；
        4. 实例化 CodingAgent 并挂载会话存储与运行时。

        Args:
            session_id: 目标会话标识符，为 None 时由 Agent 自动创建新会话。
        """
        from .agent import Agent

        async def service_confirm(spec, args, context):
            """桥接工具执行审批至服务层审批流协调器。"""
            return await self._handle_approval_request(spec, args, context)

        async def service_events(kind, data):
            """桥接底层内核事件至服务层事件总线分发器。"""
            await self._handle_agent_event(kind, data)

        budget = Budget(
            self.budget_limit_tokens,
            self.budget_limit_calls,
            self.maintenance_limit,
            cost_limit_cny=self.budget_limit_cost_cny,
        )

        self.agent = Agent(
            self.project_path,
            self.runtime,
            session_id=session_id,
            permission_mode=self.permission_mode,
            confirm=service_confirm,
            budget=budget,
            max_steps=self.max_steps,
            task_token_limit=self.task_token_limit,
            enable_memory=self.enable_memory,
            enable_evolution=self.enable_evolution,
            on_event=service_events,
        )

    def _derive_activity(self) -> str:
        """根据严格的优先级规则推导当前服务的细粒度活动状态。

        【设计思想与开源血统】
        遵循 DeepSeek-Harness `packages/host` 优先级裁决规则：
        closing -> cancelling -> awaiting_approval -> compacting ->
        executing_tools -> generating -> maintaining -> idle

        Returns:
            str: 当前细粒度活动状态枚举标识。
        """
        if self.lifecycle in {"closing", "closed"}:
            return "closing"
        if self._is_cancelling:
            return "cancelling"
        if self._pending_approvals:
            return "awaiting_approval"
        if self._is_compacting:
            return "compacting"
        if self._active_tools:
            return "executing_tools"
        if self._is_generating:
            return "generating"
        if self.agent and getattr(self.agent, "_background", None):
            return "maintaining"
        return "idle"

    async def get_snapshot(self) -> StateSnapshot:
        """生成不可变的系统全局状态全量快照（StateSnapshot）。

        【架构定位】
        作为客户端全量状态同步与断线重连（Resync）的核心数据源。
        聚合当前序号、生命周期、细粒度活动、模型路由、预算账本消耗、未决输入、待决审批与会话树深度。

        Returns:
            StateSnapshot: 全量不可变状态快照实例。
        """
        activity = self._derive_activity()
        budget_dict = {
            "calls": 0,
            "tokens": 0,
            "cost_cny": "0",
            "unknown_calls": 0,
            "pending_tokens": 0,
            "is_cost_reliable": False,
        }
        if self.agent and self.agent.budget:
            from .session_commands import budget_summary
            budget_dict = budget_summary(self.agent.budget)

        routes_dict = {}
        if self.runtime and hasattr(self.runtime, "routes"):
            for purpose, info in self.runtime.routes.items():
                routes_dict[purpose] = {
                    "provider": getattr(info, "provider_id", "unknown"),
                    "model": getattr(info, "model_id", "unknown"),
                    "context_window": getattr(info, "context_window", 0),
                    "max_output_tokens": getattr(info, "max_output_tokens", 0),
                }

        pending_approvals_list = [
            {
                "request_id": req_id,
                "name": item["spec"].name,
                "permission": item["spec"].permission,
                "arguments": item["arguments"],
                "created_at": item["timestamp"],
            }
            for req_id, item in self._pending_approvals.items()
        ]

        pending_inputs_list = []
        if self.agent and hasattr(self.agent, "_queues"):
            for item in self.agent._queues.get("steering", []):
                pending_inputs_list.append({"kind": "steering", "text": item["text"]})
            for item in self.agent._queues.get("follow_up", []):
                pending_inputs_list.append({"kind": "follow_up", "text": item["text"]})

        session_dict = {
            "session_id": self.session_id,
            "leaf": getattr(self.agent.session, "leaf", None) if self.agent else None,
            "parent": None,
            "branch_depth": getattr(self.agent, "depth", 0) if self.agent else 0,
        }

        background_dict = {
            "active_tasks": len(getattr(self.agent, "_background", [])) if self.agent else 0,
            "active_tools_count": len(self._active_tools),
        }

        return StateSnapshot(
            schema_version=1,
            service_id=self.service_id,
            through_seq=self.events.current_seq,
            session_generation=self.session_generation,
            project={"path": str(self.project_path), "name": self.project_path.name},
            session=session_dict,
            routes=routes_dict,
            permission_mode=self.agent.permission_mode if self.agent else self.permission_mode,
            lifecycle=self.lifecycle,
            activity=activity,
            budget=budget_dict,
            pending_inputs=pending_inputs_list,
            pending_approvals=pending_approvals_list,
            background=background_dict,
            last_outcome=self._last_outcome,
        )

    def _emit_event(self, kind: str, data: dict[str, Any], **kwargs) -> AppEvent:
        """向事件总线发布带有单调自增序号与因果追踪信封的新领域事件。

        Args:
            kind: 事件类型标识（如 "state_changed", "turn_start", "tool_end" 等）。
            data: 业务载荷字典。
            **kwargs: 可选的因果追踪字段（turn_id, request_id, attempt_id 等）。

        Returns:
            AppEvent: 发布成功的不可变领域事件对象。
        """
        event = self.events.create_event(
            self.session_id,
            self.session_generation,
            kind,
            data,
            **kwargs,
        )
        self.events.publish(event)
        return event

    async def _handle_agent_event(self, kind: str, data: Any) -> None:
        """将底层 Agent 执行内核的原始事件桥接转换并广播为标准应用事件。

        【事件映射机制】
        - text / thinking: 流式文本与思考过程增量更新（text_delta / thinking_delta）；
        - attempt_reset: 重试前清空当前流式累积缓存；
        - tool_start / tool_completed: 工具执行卡片状态生命周期广播与 Authoritative 结果填充；
        - turn_end: 轮次结束结算，更新最后退出状态；
        - compaction_start / compaction_end: 8 段式压缩状态标识切换与广播。

        Args:
            kind: 底层内核事件类型字符串。
            data: 底层事件附带的数据载荷。
        """
        if kind == "text":
            self._stream_text += str(data)
            self._is_generating = True
            self._emit_event("text_delta", {"delta": str(data)})
        elif kind == "thinking":
            self._stream_thinking += str(data)
            self._is_generating = True
            self._emit_event("thinking_delta", {"delta": str(data)})
        elif kind == "attempt_reset":
            self._stream_text = self._stream_thinking = ""
            self._emit_event("attempt_reset", data if isinstance(data, dict) else {"reason": str(data)})
        elif kind == "tool_start":
            self._stream_text = self._stream_thinking = ""
            call_id = data.get("call_id") if isinstance(data, dict) else None
            if call_id:
                self._active_tools[call_id] = data
            self._emit_event("tool_start", data if isinstance(data, dict) else {"data": data})
        elif kind == "tool_completed":
            call_id = data.get("call_id") if isinstance(data, dict) else None
            payload = dict(data) if isinstance(data, dict) else {}
            # Completion notifications are intentionally small in the kernel;
            # hydrate the display from the already committed authoritative result.
            if self.agent:
                record = next((r["data"] for r in reversed(self.agent.session.records)
                               if r["kind"] == "tool_result" and r["data"].get("call_id") == call_id), None)
                if record:
                    payload.update(name=record.get("name"), result=record.get("content", ""),
                                   artifact_path=record.get("output_ref"),
                                   preview=(record.get("structured") or {}).get("_display", {}))
            if call_id in self._active_tools:
                self._active_tools.pop(call_id, None)
            self._emit_event("tool_end", payload)
        elif kind == "turn_end":
            self._stream_text = self._stream_thinking = ""
            self._is_generating = False
            state = data.get("state") if isinstance(data, dict) else "completed"
            self._last_outcome = state
            self._emit_event("turn_end", data if isinstance(data, dict) else {"state": state})
        elif kind == "compaction_start":
            self._is_compacting = True
            self._emit_event("compaction_start", data if isinstance(data, dict) else {})
        elif kind == "compaction_end":
            self._is_compacting = False
            self._emit_event("compaction_end", data if isinstance(data, dict) else {})
        else:
            # Generic passthrough for custom events
            payload = data if isinstance(data, dict) else {"value": data}
            self._emit_event(kind, payload)

        # Notify state changed
        snapshot = await self.get_snapshot()
        self._emit_event("state_changed", snapshot.to_dict())

    async def _handle_approval_request(self, spec, args, context) -> bool:
        """挂起工具执行协程并向展示层广播工具审批请求。

        【设计思想与开源血统】
        借鉴 DeepSeek-Harness 两阶段审批屏障：
        1. 生成全局唯一审批 ID（`apr_<id>`）与异步 Future；
        2. 广播 `approval_requested` 事件并刷新系统快照为 `awaiting_approval`；
        3. 异步挂起等待客户端通过 `resolve_approval` 显式裁决；
        4. 收到许可或拒绝后，清理待决审批并广播 `approval_resolved` 事件。

        Args:
            spec: 待执行工具的元数据规范对象。
            args: 工具调用入参字典。
            context: 当前执行上下文。

        Returns:
            bool: 审批是否获准执行。
        """
        if self.lifecycle in {"closing", "closed"}:
            return False
        request_id = f"apr_{new_id()}"
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        approval_record = {
            "future": future,
            "spec": spec,
            "arguments": args,
            "context": context,
            "timestamp": time.time(),
        }
        self._pending_approvals[request_id] = approval_record

        self._emit_event(
            "approval_requested",
            {
                "request_id": request_id,
                "name": spec.name,
                "arguments": args,
                "permission": spec.permission,
                "created_at": approval_record["timestamp"],
            },
        )
        snapshot = await self.get_snapshot()
        self._emit_event("state_changed", snapshot.to_dict())

        try:
            approved = await future
            return bool(approved)
        finally:
            self._pending_approvals.pop(request_id, None)
            self._emit_event(
                "approval_resolved",
                {"request_id": request_id, "approved": future.done() and not future.cancelled() and bool(future.result())},
            )
            snapshot = await self.get_snapshot()
            self._emit_event("state_changed", snapshot.to_dict())

    async def resolve_approval(self, request_id: str, approved: bool) -> CommandResult:
        """裁决并唤醒指定的待决工具审批请求。

        Args:
            request_id: 待决审批请求唯一标识符。
            approved: 用户或客户端的裁决结果（True 准许执行，False 拒绝拦截）。

        Returns:
            CommandResult: 审批裁决结果信封（若请求过期或不存在则返回 stale_request 错误码）。
        """
        record = self._pending_approvals.get(request_id)
        if not record:
            return CommandResult(ok=False, code="stale_request", message="Approval request not found or already resolved")

        future = record["future"]
        if not future.done():
            future.set_result(approved)
            return CommandResult(
                ok=True,
                code="resolved",
                message=f"Approval {'granted' if approved else 'denied'}",
                data={"request_id": request_id, "approved": approved},
            )
        return CommandResult(ok=False, code="stale_request", message="Approval request already completed")

    async def submit(
        self,
        text: str,
        *,
        mode: Literal["auto", "steer", "followup"] = "auto",
        client_request_id: str,
    ) -> SubmissionReceipt:
        """提交文本输入供 Agent 执行处理，提供工业级严格幂等性与三态调度。

        【架构定位】
        人机交互与外部客户端调用的核心统一入口。

        【设计思想与开源血统】
        1. 幂等性防护（DeepSeek-Harness Idempotency Cache）：
           相同 `client_request_id` 与文本的重复提交直接返回已缓存回执；若相同 ID 携带冲突文本或模式则抛出 `request_conflict`。
        2. 三态输入流转（Pi Agent Steering / Follow-up 机制）：
           - auto: 空闲时启动新自主轮次（prompt），忙碌生成时抢占式压入引导队列（steering）；
           - steer: 强制作为干预引导指令（若当前未在运行则拒绝）；
           - followup: 顺序追加至排队追问队列，空闲时触发未决处理。

        Args:
            text: 用户输入的指令文本（长度不超过 200,000 字符）。
            mode: 输入分流策略（"auto", "steer", "followup"）。
            client_request_id: 客户端生成的全局唯一幂等请求 ID。

        Returns:
            SubmissionReceipt: 提交接纳凭据回执。

        Raises:
            RuntimeError: 服务尚未就绪或正在进行手动压缩。
            ValueError: 输入文本为空、超长或存在幂等性冲突。
        """
        if self.lifecycle != "ready":
            raise RuntimeError(f"Service not ready (state: {self.lifecycle})")

        if self._is_compacting and not (self._active_task and not self._active_task.done()):
            raise RuntimeError("busy: wait for manual compaction")

        # 1. Idempotency check
        if client_request_id in self._processed_submissions:
            if self._submission_texts.get(client_request_id) != text:
                raise ValueError("request_conflict: same request_id with differing text")
            prior = self._processed_submissions[client_request_id]
            original_mode = self._submission_modes.get(client_request_id, prior.mode)
            if mode == original_mode or mode == prior.mode or (mode == "auto" and prior.mode in {"prompt", "steering"}):
                return prior
            raise ValueError("request_conflict: same request_id with differing mode")

        # 2. Validation
        if not isinstance(text, str) or not text.strip():
            raise ValueError("invalid_input: text must be non-empty")
        if len(text) > 200000:
            raise ValueError("invalid_input: input text exceeds 200,000 characters limit")

        is_running = bool(self._active_task and not self._active_task.done()) or bool(self.agent and getattr(self.agent, "_running", False))

        if mode == "auto":
            if is_running:
                self.agent.steer(text)
                receipt = SubmissionReceipt(True, client_request_id, "steering", "Queued as steering")
            else:
                self._is_generating = True
                self._emit_event("turn_start", {"prompt": text})
                self._active_task = asyncio.create_task(self._run_prompt(text))
                receipt = SubmissionReceipt(True, client_request_id, "prompt", "Turn initiated")
        elif mode == "steer":
            if not is_running:
                raise ValueError("not_running: cannot steer when no active turn is running")
            self.agent.steer(text)
            receipt = SubmissionReceipt(True, client_request_id, "steering", "Queued as steering")
        elif mode == "followup":
            self.agent.follow_up(text)
            if not is_running:
                self._active_task = asyncio.create_task(self._run_followup())
            receipt = SubmissionReceipt(True, client_request_id, "followup", "Queued as followup")
        else:
            raise ValueError(f"invalid_input: unknown mode {mode}")

        self._processed_submissions[client_request_id] = receipt
        self._submission_modes[client_request_id] = mode
        self._submission_texts[client_request_id] = text
        snapshot = await self.get_snapshot()
        self._emit_event("state_changed", snapshot.to_dict())
        return receipt

    async def _run_prompt(self, text: str) -> None:
        """异步执行单轮自主推理轮次，提供异常捕获与状态复位。

        Args:
            text: 本轮提示词输入字符串。
        """
        try:
            await self.agent.run_once(text)
        except asyncio.CancelledError:
            self._last_outcome = "cancelled"
        except Exception as error:
            self._last_outcome = "failed"
            self._emit_event("service_error", {"code": "turn_failed", "message": type(error).__name__})
        finally:
            self._is_generating = False
            self._is_cancelling = False
            snapshot = await self.get_snapshot()
            self._emit_event("state_changed", snapshot.to_dict())

    async def _run_followup(self) -> None:
        """异步消费处理排队队列中的未决追问输入。"""
        try:
            await self.agent.resume_pending()
        except asyncio.CancelledError:
            self._last_outcome = "cancelled"
        except Exception as error:
            self._last_outcome = "failed"
            self._emit_event("service_error", {"code": "followup_failed", "message": type(error).__name__})
        finally:
            self._is_generating = False
            self._is_cancelling = False
            snapshot = await self.get_snapshot()
            self._emit_event("state_changed", snapshot.to_dict())

    async def cancel_turn(self) -> CommandResult:
        """非阻塞式级联取消活跃轮次并拒绝所有挂起中的工具审批。

        【核心不变性与安全约束】
        优雅中断：向活跃任务派发 `cancel()` 信号，唤醒并驳回所有等待中的审批 Future，
        重置生成与取消标志位，确保状态机安全回滚至闲置就绪态。

        Returns:
            CommandResult: 取消请求受理结果信封。
        """
        is_running = bool(self._active_task and not self._active_task.done()) or bool(self.agent and getattr(self.agent, "_running", False))
        if not is_running and not self._pending_approvals:
            return CommandResult(ok=False, code="not_running", message="No active turn to cancel")

        self._is_cancelling = True
        # Reject pending approvals
        for req_id, item in list(self._pending_approvals.items()):
            fut = item["future"]
            if not fut.done():
                fut.set_result(False)

        if self.agent:
            self.agent.abort()

        if self._active_task and not self._active_task.done():
            self._active_task.cancel()

        snapshot = await self.get_snapshot()
        self._emit_event("state_changed", snapshot.to_dict())
        return CommandResult(ok=True, code="cancelling", message="Cancellation requested")

    async def execute_command(self, line: str, *, client_request_id: str) -> CommandResult:
        """解析并派发执行斜杠控制命令（Slash Commands）。

        【架构定位】
        统一接入层命令派发网关，保证 CLI 与 TUI 语义完全一致。

        【执行规则】
        - /cancel: 取消当前活跃轮次；
        - /steer, /followup: 路由至 submit() 的抢占引导与排队逻辑；
        - /sessions, /new, /models, /usage, /quit: 服务层专属会话与运行时元数据查询；
        - /branch, /fork, /clear, /goal, /plan, /compact, /skills 等:
          互斥检查通过后派发至底层 session_commands 统一执行。

        Args:
            line: 斜杠命令原始字符串。
            client_request_id: 客户端幂等请求标识符。

        Returns:
            CommandResult: 标准化执行结果结构体。
        """
        from .session_commands import run_session_command

        if self.lifecycle != "ready" or self.agent is None:
            return CommandResult(False, "closed", "Service is not ready")
        try:
            parts = shlex.split(line)
        except ValueError:
            return CommandResult(False, "invalid_input", "Unclosed quote in command")
        if not parts or not parts[0].startswith("/"):
            return CommandResult(False, "invalid_input", "Commands must begin with '/'")
        name, args = parts[0], parts[1:]
        try:
            if name == "/cancel":
                return await self.cancel_turn()
            if name in {"/steer", "/followup"}:
                if not args:
                    raise ValueError("Provide a message")
                mode = "steer" if name == "/steer" else "followup"
                receipt = await self.submit(" ".join(args), mode=mode, client_request_id=client_request_id)
                return CommandResult(True, "submitted", receipt.message, receipt.to_dict())
            if name == "/sessions":
                return CommandResult(True, "success", "Sessions", [asdict(s) for s in await self.list_sessions()])
            if name == "/new":
                snapshot = await self.new_session()
                return CommandResult(True, "success", "Session created", snapshot.to_dict())
            if name == "/models":
                return CommandResult(True, "success", "Model routes", (await self.get_snapshot()).routes)
            if name in {"/usage", "/cost"} and not args:
                return CommandResult(True, "success", "Usage", (await self.get_snapshot()).budget)
            if name in {"/quit", "/exit"}:
                return CommandResult(True, "quit", "Quit requested")
            mutating = {"/branch", "/fork", "/refresh", "/skill-rollback", "/goal", "/mcp-trust", "/plan", "/compact", "/clear"}
            if name in mutating and self._derive_activity() != "idle":
                return CommandResult(False, "busy", "Wait for active work before changing session state")
            if name == "/memory" and args and args[0] == "save" and self._derive_activity() != "idle":
                return CommandResult(False, "busy", "Wait for active work before writing memory")
            outputs = []
            await run_session_command(self.agent, line, outputs.append)
            self.permission_mode = self.agent.permission_mode
            result = outputs[-1] if outputs else None
            if isinstance(result, dict) and result.get("status") in {"rejected", "discarded", "error"}:
                return CommandResult(False, "permission_denied" if result.get("reason") == "Permission denied" else "rejected",
                                     result.get("reason", "Operation rejected"), result)
            return CommandResult(True, "success", "Command completed", result)
        except (ValueError, RuntimeError, PermissionError, KeyError, OSError) as error:
            return CommandResult(False, "invalid_input", str(error))

    async def list_sessions(self) -> list[SessionSummary]:
        """以只读安全模式查询工作区 state.db 中已持久化的所有历史会话。

        Returns:
            list[SessionSummary]: 会话元数据摘要列表（包含 ID、创建时间、更新时间、记录数与 leaf 指针）。
        """
        from .session import SessionStore

        rows = SessionStore.list_sessions(self.project_path)
        summaries = []
        for r in rows:
            summaries.append(
                SessionSummary(
                    session_id=r["session_id"],
                    project=r.get("project", str(self.project_path)),
                    created=r.get("created", ""),
                    updated=r.get("updated", ""),
                    message_count=r.get("message_count", 0),
                    leaf=r.get("leaf"),
                )
            )
        return summaries

    async def open_session(self, session_id: str) -> StateSnapshot:
        """安全切换至已存在的指定会话。

        【核心不变性与安全约束】
        互斥栅栏：切换前必须保证当前活动状态处于 "idle" 闲置态，严禁在轮次执行或工具调用中并发换轨。
        切换时递增 `session_generation` 会话代数，彻底防御旧会话残余异步事件的幽灵污染。

        Args:
            session_id: 目标会话标识符。

        Returns:
            StateSnapshot: 切换后新会话的系统状态快照。

        Raises:
            RuntimeError: 当后台任务或工具正在执行处于忙碌状态时抛出。
        """
        if self._derive_activity() != "idle":
            raise RuntimeError("busy: cannot switch session while tasks or tools are active")

        from .session_commands import session_path
        session_id = session_path(self.project_path, session_id)

        if self.agent:
            await self.agent.close(close_runtime=False)
            self.agent = None

        self.session_generation += 1
        await self._init_agent(session_id)
        snapshot = await self.get_snapshot()
        self._emit_event("state_changed", snapshot.to_dict())
        return snapshot

    async def new_session(self, *, main_model: Any | None = None) -> StateSnapshot:
        """创建全新的独立会话并完成上下文切换。

        Args:
            main_model: 可选的主模型配置覆盖。

        Returns:
            StateSnapshot: 新建会话后的系统状态快照。

        Raises:
            RuntimeError: 当后台任务或工具处于忙碌状态时抛出。
        """
        if self._derive_activity() != "idle":
            raise RuntimeError("busy: cannot create new session while tasks or tools are active")

        if self.agent:
            await self.agent.close(close_runtime=False)
            self.agent = None

        new_sess_id = f"sess_{new_id()}"
        self.session_generation += 1
        await self._init_agent(new_sess_id)
        snapshot = await self.get_snapshot()
        self._emit_event("state_changed", snapshot.to_dict())
        return snapshot

    async def get_history(self, *, before_record_id: str | None = None, limit: int = 100) -> HistoryPage:
        """检索底层会话原始日志并投影转换为 UI 展示卡片分页数据。

        【设计思想与开源血统】
        遵循 Presenter Purity 规范，底层 SQLite 仅存储只读事件，展示层通过 `project_raw_session_records`
        纯函数将其无损投影为包含文本、思考、工具调用与结果展示卡片。

        Args:
            before_record_id: 分页游标起始记录 ID。
            limit: 单页最大记录条数（0 表示拉取全量记录）。

        Returns:
            HistoryPage: 包含投影卡片列表与分页标识的数据结构体。
        """
        if not self.agent or not self.agent.session:
            return HistoryPage(self.session_id, [], 0, False)

        records = getattr(self.agent.session, "records", [])
        clears = [i for i, r in enumerate(records) if r["kind"] == "seed" and r["data"].get("reason") == "user_clear"]
        if clears:
            records = records[clears[-1] + 1:]
        total_records = len(records)
        selected_records = records[-limit:] if limit > 0 else records
        cards = project_raw_session_records(selected_records)
        card_dicts = [asdict(c) for c in cards]
        return HistoryPage(
            session_id=self.session_id,
            cards=card_dicts,
            total_records=total_records,
            has_more=limit > 0 and total_records > limit,
        )

    async def read_tool_output(self, output_id: str, *, offset: int = 0, limit: int = 200) -> OutputPage:
        """安全分页读取大体积工具输出 Artifacts 工件文件。

        【核心不变性与安全约束】
        路径遍历防御（Path Traversal Defense）：强制通过 `Path(output_id).name` 提取纯文件名，
        并严格断言目标文件必须解析位于 `.weyra/artifacts/` 目录内部，杜绝绝对路径或 `../` 越界读取。

        Args:
            output_id: 工件文件标识符。
            offset: 起始行号偏移。
            limit: 单页读取最大行数。

        Returns:
            OutputPage: 分页文本行与截断指示结构体。

        Raises:
            FileNotFoundError: 工件文件不存在或超出合法工作区边界。
        """
        artifacts_dir = self.project_path / ".weyra" / "artifacts"
        # Sanitize output_id against path traversal
        clean_name = Path(output_id).name
        target_file = (artifacts_dir / clean_name).resolve()

        if not target_file.is_relative_to(artifacts_dir) or not target_file.is_file():
            raise FileNotFoundError(f"not_found: Artifact {clean_name} does not exist")

        text = target_file.read_text(encoding="utf-8", errors="replace")
        return paginate_text(text, output_id=output_id, offset=offset, limit=limit)

    async def subscribe(self, subscriber_id: str | None = None) -> tuple[StateSnapshot, Subscription]:
        """为客户端注册异步事件流订阅通道，并原子化返回初始全量状态快照。

        Args:
            subscriber_id: 可选的订阅者唯一标识符，若为 None 则自动生成。

        Returns:
            tuple[StateSnapshot, Subscription]: 当前状态全量快照与具有背压保护的订阅通道。
        """
        sub_id = subscriber_id or f"sub_{new_id()}"
        sub = self.events.subscribe(sub_id)
        snapshot = await self.get_snapshot()
        return snapshot, sub

    async def resubscribe(self, subscriber_id: str) -> tuple[StateSnapshot, HistoryPage, Subscription, list[dict[str, Any]]]:
        """客户端断线重连或背压溢出后的原子化重同步通道。

        【架构定位】
        在单个事件循环边界内原子化获取最新快照、全量卡片历史、新订阅通道以及正在流式传输中的未决思考与工具增量，
        确保客户端无缝缝合断点。

        Args:
            subscriber_id: 重连客户端的订阅标识符。

        Returns:
            tuple: 包含 snapshot, history, subscription 与 live_deltas 的四元组。
        """
        # These snapshot/history readers contain no suspension: one event-loop boundary.
        snapshot, sub = await self.subscribe(subscriber_id)
        history = await self.get_history(limit=0)
        live = []
        for kind, text in (("thinking_delta", self._stream_thinking), ("text_delta", self._stream_text)):
            if text:
                live.append({"kind": kind, "data": {"delta": text}})
        for data in self._active_tools.values():
            live.append({"kind": "tool_start", "data": copy.deepcopy(data)})
        return snapshot, history, sub, live

    def unsubscribe(self, subscriber_id: str) -> bool:
        """注销并安全释放指定的事件订阅通道。

        Args:
            subscriber_id: 待注销的订阅者 ID。

        Returns:
            bool: 是否成功移除该订阅通道。
        """
        return self.events.unsubscribe(subscriber_id)

    async def close(self) -> CommandResult:
        """优雅关闭服务中枢，释放运行时、驳回未决审批并销毁事件总线。

        【执行流程】
        1. 状态置为 "closing"；
        2. 唤醒并以 False 结果清空所有待决工具审批；
        3. 协同取消当前活跃的推理任务；
        4. 关闭 Agent 底层会话并视情况释放模型运行时；
        5. 关闭事件总线，状态最终迁移至 "closed"。

        Returns:
            CommandResult: 关闭操作执行结果信封。
        """
        self.lifecycle = "closing"
        # Reject pending approvals
        for req_id, item in list(self._pending_approvals.items()):
            fut = item["future"]
            if not fut.done():
                fut.set_result(False)

        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            try:
                await asyncio.wait_for(self._active_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        if self.agent:
            try:
                await self.agent.close(close_runtime=True)
            except Exception:
                pass
            self.agent = None

        self.events.close()
        self.lifecycle = "closed"
        return CommandResult(ok=True, code="closed", message="Weyra service closed cleanly")

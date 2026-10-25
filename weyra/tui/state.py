"""TUI 客户端响应式状态容器与状态规约器（UI State Container & Reducer）。

架构定位与设计哲学:
    本模块遵循单向数据流（Unidirectional Data Flow）与 Redux 状态规约设计原则。
    负责在客户端内存中维护 UI 的只读响应式快照（UIState），
    通过接收服务端的全量快照（apply_snapshot）或增量事件（apply_event）进行局部原子更新，
    避免 UI 组件直接穿透调用底层服务导致状态撕裂（State Tearing）。
"""

from __future__ import annotations


import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from weyra.events import AppEvent
from weyra.service import StateSnapshot


@dataclass
class UIState:
    """Reactive snapshot of UI presentation state."""

    project_path: Path = field(default_factory=Path.cwd)
    project_name: str = ""
    session_id: str = "default"
    permission_mode: str = "default"
    activity: str = "idle"
    main_model: str = "unknown"
    routes: dict[str, Any] = field(default_factory=dict)
    calls: int = 0
    tokens: int = 0
    cost_cny: str | None = None
    is_cost_reliable: bool = False
    active_tool_count: int = 0
    is_compacting: bool = False
    is_cancelling: bool = False
    is_evolving: bool = False
    pending_approval: dict[str, Any] | None = None
    input_history: list[str] = field(default_factory=list)
    history_index: int = -1
    ascii_only: bool = False
    no_color: bool = False

    @classmethod
    def create(cls, project_path: Path, *, ascii_only: bool = False) -> UIState:
        """从项目工程根目录与环境配置初始化 TUI 状态快照。"""
        no_color = bool(os.environ.get("NO_COLOR", "").strip())
        return cls(
            project_path=project_path,
            project_name=project_path.name,
            ascii_only=ascii_only,
            no_color=no_color,
        )

    def apply_snapshot(self, snapshot: StateSnapshot) -> None:
        """Update reactive state fields from an authoritative backend snapshot."""
        self.session_id = snapshot.session.get("session_id", self.session_id)
        self.permission_mode = snapshot.permission_mode
        self.activity = snapshot.activity
        self.is_compacting = snapshot.activity == "compacting"
        self.is_cancelling = snapshot.activity == "cancelling"
        self.is_evolving = snapshot.activity == "maintaining"
        self.routes = snapshot.routes
        if "main" in snapshot.routes:
            self.main_model = snapshot.routes["main"].get("model", "unknown")

        b = snapshot.budget
        self.calls = b.get("calls", 0)
        self.tokens = b.get("tokens", 0)
        self.cost_cny = b.get("cost_cny", "0")
        self.is_cost_reliable = b.get("is_cost_reliable", False)

        bg = snapshot.background
        self.active_tool_count = bg.get("active_tools_count", 0)

        # Pending approvals
        if snapshot.pending_approvals:
            self.pending_approval = snapshot.pending_approvals[0]
        else:
            self.pending_approval = None

    def apply_event(self, event: AppEvent) -> None:
        """Reduce transient event updates into presentation state."""
        kind = event.kind
        data = event.data

        if kind == "state_changed":
            # StateSnapshot dict payload
            if "session" in data and "budget" in data:
                self.session_id = data.get("session", {}).get("session_id", self.session_id)
                self.permission_mode = data.get("permission_mode", self.permission_mode)
                self.activity = data.get("activity", self.activity)
                routes = data.get("routes", {})
                if "main" in routes:
                    self.main_model = routes["main"].get("model", self.main_model)
                b = data.get("budget", {})
                self.calls = b.get("calls", self.calls)
                self.tokens = b.get("tokens", self.tokens)
                self.cost_cny = b.get("cost_cny", self.cost_cny)
                self.is_cost_reliable = b.get("is_cost_reliable", self.is_cost_reliable)
                bg = data.get("background", {})
                self.active_tool_count = bg.get("active_tools_count", self.active_tool_count)
        elif kind == "turn_start":
            self.activity = "generating"
        elif kind == "turn_end":
            self.activity = "idle"
            self.is_cancelling = False
        elif kind == "tool_start":
            self.active_tool_count += 1
            if self.activity not in {"awaiting_approval", "cancelling"}:
                self.activity = "executing_tools"
        elif kind == "tool_end":
            self.active_tool_count = max(0, self.active_tool_count - 1)
            if self.active_tool_count == 0 and self.activity == "executing_tools":
                self.activity = "generating"
        elif kind == "compaction_start":
            self.is_compacting = True
        elif kind == "compaction_end":
            self.is_compacting = False
        elif kind == "skill_evolution_start":
            self.is_evolving = True
        elif kind == "skill_evolution_end":
            self.is_evolving = False
        elif kind == "approval_requested":
            self.pending_approval = data
            self.activity = "awaiting_approval"
        elif kind == "approval_resolved":
            self.pending_approval = None
            if self.activity == "awaiting_approval":
                self.activity = "executing_tools"

    def record_input(self, text: str) -> None:
        """Add non-empty input to traversal history."""
        cleaned = text.strip()
        if not cleaned:
            return
        if not self.input_history or self.input_history[-1] != cleaned:
            self.input_history.append(cleaned)
        self.history_index = -1

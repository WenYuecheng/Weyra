"""Compact identity and activity bar with Starship / Powerlevel10k pill aesthetic."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rich.text import Text
from textual.widgets import Static

from weyra.tui.state import UIState


class StatusBar(Static):
    """顶部胶囊风格状态指示栏组件 (Powerlevel10k / Starship Aesthetic)。"""

    def __init__(self, **kwargs):
        """初始化顶部状态栏组件。"""
        super().__init__(**kwargs)
        self._state: UIState | None = None

    def update_status(self, state: UIState) -> None:
        """更新当前绑定的响应式状态快照并触发界面重绘。"""
        self._state = state
        self.refresh()

    def render(self) -> Text:
        """渲染顶部两行式状态栏富文本（第一行身份与模型，第二行活动胶囊与消耗遥测）。"""
        state = self._state
        if state is None:
            return Text("WEYRA  正在准备工作区…")

        width = max(20, self.content_size.width)

        # ── Line 1: Brand, Workspace, Session, and Model ───────────────────
        sid = state.session_id
        sid_short = sid if len(sid) < 18 else sid[:10] + "…"

        if state.no_color:
            left_1 = Text(f"[WEYRA] [{state.project_name}] · <{sid_short}>")
            right_1 = Text(f"Model: {state.main_model}")
        else:
            left_1 = Text.from_markup(
                f"[bold black on #00f2fe] ⚡ WEYRA [/]"
                f"[bold black on #c084fc] {state.project_name} [/] "
                f"[dim #64748b]·[/] [bold #38bdf8]‹{sid_short}›[/]"
            )
            right_1 = Text.from_markup(
                f"[dim #64748b]Model:[/] [bold #38bdf8]{state.main_model}[/]"
            )

        left_1.truncate(max(10, width // 2), overflow="ellipsis")
        right_1.truncate(max(1, width - left_1.cell_len - 2), overflow="ellipsis")
        spacer_1 = " " * max(1, width - left_1.cell_len - right_1.cell_len)
        line_1 = Text()
        line_1.append_text(left_1)
        line_1.append(spacer_1)
        line_1.append_text(right_1)

        # ── Line 2: Activity Pill, Permission, and Telemetry ───────────────
        activity_map = {
            "idle": ("● 就绪", "#10b981", "#0b0f19"),
            "generating": ("⠋ 生成中", "#fbbf24", "#0b0f19"),
            "executing_tools": (f"⚡ 工具执行中 · {state.active_tool_count}", "#00f2fe", "#0b0f19"),
            "awaiting_approval": ("🛡 等待审批", "#f43f5e", "#ffffff"),
            "compacting": ("🗜 压缩中", "#c084fc", "#0b0f19"),
            "cancelling": ("✖ 取消中", "#f43f5e", "#ffffff"),
            "maintaining": ("🧬 技能维护", "#38bdf8", "#0b0f19"),
            "closing": ("⏹ 正在关闭", "#64748b", "#ffffff"),
        }
        label, bg_color, fg_color = activity_map.get(
            state.activity, (state.activity, "#334155", "#f8fafc")
        )

        tokens = f"{state.tokens / 1000:.1f}k" if state.tokens >= 1000 else str(state.tokens)
        try:
            amount = Decimal(state.cost_cny)
            cost = f"¥{amount:.3f}" if state.is_cost_reliable else (f"约 ¥{amount:.3f}" if amount else "费用未定")
        except (InvalidOperation, TypeError, ValueError):
            cost = "费用未定"

        if state.no_color:
            left_2 = Text(f"[{label}] 权限: {state.permission_mode}")
            if state.is_evolving:
                left_2.append(" · 技能维护")
            right_2 = Text(f"Tokens: {tokens} · 调用: {state.calls} · 费用: {cost}")
        else:
            left_2 = Text.from_markup(
                f"[bold {fg_color} on {bg_color}] {label} [/] "
                f"[dim #64748b]权限:[/] [bold #fbbf24]{state.permission_mode}[/]"
            )
            if state.is_evolving:
                left_2.append_text(Text.from_markup(" [dim #64748b]·[/] [bold #38bdf8]🧬 演化[/]"))
            right_2 = Text.from_markup(
                f"[dim #64748b]Tokens:[/] [bold #38bdf8]{tokens}[/] "
                f"[dim #64748b]·[/] [dim #64748b]调用:[/] [bold #34d399]{state.calls}[/] "
                f"[dim #64748b]·[/] [dim #64748b]费用:[/] [bold #34d399]{cost}[/]"
            )

        left_2.truncate(max(10, width - right_2.cell_len - 2), overflow="ellipsis")
        right_2.truncate(max(1, width - left_2.cell_len - 1), overflow="ellipsis")
        spacer_2 = " " * max(1, width - left_2.cell_len - right_2.cell_len)
        line_2 = Text()
        line_2.append_text(left_2)
        line_2.append(spacer_2)
        line_2.append_text(right_2)

        result = Text()
        result.append_text(line_1)
        result.append("\n")
        result.append_text(line_2)
        return result

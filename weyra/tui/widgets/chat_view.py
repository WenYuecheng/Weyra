"""Scrollable chat container with smart auto-scroll pinning and history reconstruction."""

from __future__ import annotations

from typing import Any

from rich import box
from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from weyra.tui.widgets.message import AssistantMessageWidget, SystemNoticeWidget, UserMessageWidget
from weyra.tui.widgets.thinking_card import ThinkingCardWidget
from weyra.tui.widgets.tool_card import ToolCardWidget

LOGO_WEYRA = r"""
  ██╗    ██╗███████╗██╗   ██╗██████╗  █████╗ 
  ██║    ██║██╔════╝╚██╗ ██╔╝██╔══██╗██╔══██╗
  ██║ █╗ ██║█████╗   ╚████╔╝ ██████╔╝███████║
  ██║███╗██║██╔══╝    ╚██╔╝  ██╔══██╗██╔══██║
  ╚███╔███╔╝███████╗   ██║   ██║  ██║██║  ██║
   ╚══╝╚══╝ ╚══════╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝"""

LOGO_HARNESS = r"""
  ██╗  ██╗ █████╗ ██████╗ ███╗   ██╗███████╗███████╗███████╗
  ██║  ██║██╔══██╗██╔══██╗████╗  ██║██╔════╝██╔════╝██╔════╝
  ███████║███████║██████╔╝██╔██╗ ██║█████╗  ███████╗███████╗
  ██╔══██║██╔══██║██╔══██╗██║╚██╗██║██╔══╝  ╚════██║╚════██║
  ██║  ██║██║  ██║██║  ██║██║ ╚████║███████╗███████║███████║
  ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚══════╝"""


class ChatView(VerticalScroll):
    """Vertical scroll container for messages, tools, and thoughts."""

    class PinChanged(Message):
        """视图滚动是否贴底锁定状态变更事件。"""

        def __init__(self, pinned: bool):
            """初始化滚动锁定事件。"""
            super().__init__()
            self.pinned = pinned

    def __init__(self, *, ascii_only: bool = False, no_color: bool = False, **kwargs) -> None:
        """初始化聊天主滚动容器组件。"""
        super().__init__(**kwargs)
        self.ascii_only = ascii_only
        self.no_color = no_color
        self._auto_scroll_pinned = True
        self._active_assistant: AssistantMessageWidget | None = None
        self._active_thinking: ThinkingCardWidget | None = None
        self._active_tools: dict[str, ToolCardWidget] = {}
        self.tool_cards: dict[str, ToolCardWidget] = {}

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """监听 Y 轴滚动位置，自动计算是否锁定贴底并广播 PinChanged 事件。"""
        super().watch_scroll_y(old_value, new_value)
        dist_from_bottom = self.max_scroll_y - self.scroll_y
        self._auto_scroll_pinned = dist_from_bottom <= 2
        self.post_message(self.PinChanged(self._auto_scroll_pinned))

    def check_auto_scroll(self) -> None:
        """Scroll to bottom only if currently pinned."""
        if self._auto_scroll_pinned:
            self.call_after_refresh(self.scroll_end, animate=False)

    def jump_to_bottom(self) -> None:
        """Explicitly re-pin and jump to bottom."""
        self._auto_scroll_pinned = True
        self.scroll_end(animate=False)
        self.post_message(self.PinChanged(True))

    async def reset(self) -> None:
        """重置视图，清空所有活跃助手/思考/工具卡片并移除所有子组件。"""
        self._active_assistant = None
        self._active_thinking = None
        self._active_tools.clear()
        self.tool_cards.clear()
        await self.remove_children()

    def show_welcome(self, project_name: str) -> None:
        """Render the BearCode cyberpunk dashboard welcome banner."""
        if self.no_color:
            t = Table.grid()
            t.add_row(Align.center("WEYRA - EVOLVABLE CODING AGENT HARNESS"))
            t.add_row(Align.center(f"Workspace: {project_name} | Dual-SDK Engine"))
            t.add_row("")
            t.add_row("Commands: /help, /session, /skills, /memory, /models, /usage, /exit")
            panel = Panel(
                t,
                title="[ WEYRA HARNESS · READY ]",
                subtitle="Type your request or slash command below",
                box=box.ROUNDED,
                padding=(1, 2),
            )
            self.mount(Static(panel, id="welcome-banner"))
            return

        specs_table = Table.grid(expand=True, padding=(0, 2))
        specs_table.add_column(style="bold #c084fc", no_wrap=True)
        specs_table.add_column(style="white")
        specs_table.add_column(style="bold #c084fc", no_wrap=True)
        specs_table.add_column(style="white")

        specs_table.add_row(
            "ENGINE", "[bold #38bdf8]DeepSeek-V3[/] [dim](deepseek-chat)[/dim]",
            "WORKSPACE", f"[bold #34d399]{project_name}[/]",
        )
        specs_table.add_row(
            "HARNESS", "Autonomous Tools / BM25 Evolution",
            "SANDBOX", "[bold #fbbf24]Permission Guard / Microcompact[/]",
        )

        cmd_table = Table.grid(expand=True, padding=(0, 2))
        cmd_table.add_column(style="bold #00f2fe", no_wrap=True)
        cmd_table.add_column(style="#64748b")
        cmd_table.add_column(style="bold #00f2fe", no_wrap=True)
        cmd_table.add_column(style="#64748b")

        cmd_table.add_row(
            "/help", "Show all shortcuts & command guides",
            "/skills", "Explore & invoke registered skills",
        )
        cmd_table.add_row(
            "/session", "Manage, switch & create sessions",
            "/memory", "Inspect persistent project memories",
        )
        cmd_table.add_row(
            "/models", "Inspect model routes & configs",
            "/usage", "Inspect real-time token expenditure",
        )
        cmd_table.add_row(
            "/goal", "View or set current session goal",
            "/exit", "Safely exit Weyra Agent",
        )

        main_layout = Table.grid(expand=True)
        main_layout.add_row(Align.center(f"[bold #00f2fe]{LOGO_WEYRA}[/bold #00f2fe]"))
        main_layout.add_row(Align.center(f"[bold #c084fc]{LOGO_HARNESS}[/bold #c084fc]"))
        main_layout.add_row("")
        main_layout.add_row(
            Align.center("[bold #38bdf8]Autonomous Coding Agent & Self-Evolving Evaluation Harness[/bold #38bdf8]")
        )
        main_layout.add_row(
            Align.center("[dim #64748b]Creator:[/] [bold #00f2fe]温悦成 (Wen Yuecheng)[/] [dim #64748b]| v1.0.0 | Dual-SDK Engine[/dim #64748b]")
        )
        main_layout.add_row("")
        main_layout.add_row("[bold #c084fc]SYSTEM SPECIFICATIONS[/bold #c084fc]")
        main_layout.add_row(specs_table)
        main_layout.add_row("")
        main_layout.add_row("[bold #00f2fe]QUICK COMMAND NAVIGATION[/bold #00f2fe]")
        main_layout.add_row(cmd_table)

        panel = Panel(
            main_layout,
            title="[bold black on #00f2fe] WEYRA-HARNESS [/] [bold black on #c084fc] v1.0.0 [/] [bold #34d399]● READY[/]",
            title_align="left",
            border_style="#334155",
            box=box.ROUNDED,
            padding=(1, 2),
            subtitle="[dim #64748b]Type your software engineering request or slash command below[/dim #64748b]",
            subtitle_align="center",
        )
        self.mount(Static(panel, id="welcome-banner"))

    def add_user_message(self, text: str) -> UserMessageWidget:
        """Mount a user prompt message."""
        for banner in self.query("#welcome-banner"):
            banner.remove()
        widget = UserMessageWidget(text, no_color=self.no_color)
        self.mount(widget)
        self.check_auto_scroll()
        return widget

    def start_assistant_message(self, initial_text: str = "") -> AssistantMessageWidget:
        """Mount and return a new assistant message widget."""
        widget = AssistantMessageWidget(initial_text, no_color=self.no_color)
        self._active_assistant = widget
        self.mount(widget)
        self.check_auto_scroll()
        return widget

    def get_or_create_assistant_message(self) -> AssistantMessageWidget:
        """获取当前活跃的助手消息组件；若不存在则自动挂载新组件。"""
        if self._active_assistant is None:
            return self.start_assistant_message()
        return self._active_assistant

    def finish_assistant_message(self, final_text: str | None = None) -> None:
        """完成当前活跃助手消息的流式输出并解除活跃绑定。"""
        if self._active_assistant is not None:
            self._active_assistant.finish(final_text)
            self._active_assistant = None
            self.check_auto_scroll()

    def start_thinking(self, initial_text: str = "") -> ThinkingCardWidget:
        """Mount thinking card for genuine reasoning tokens."""
        widget = ThinkingCardWidget(initial_text, no_color=self.no_color)
        self._active_thinking = widget
        self.mount(widget)
        self.check_auto_scroll()
        return widget

    def get_or_create_thinking(self) -> ThinkingCardWidget:
        """获取当前活跃的模型思考卡片组件；若不存在则自动挂载新卡片。"""
        if self._active_thinking is None:
            return self.start_thinking()
        return self._active_thinking

    def finish_thinking(self, final_text: str | None = None) -> None:
        """完成当前活跃思考卡片的流式输出并解除活跃绑定。"""
        if self._active_thinking is not None:
            self._active_thinking.finish(final_text)
            self._active_thinking = None
            self.check_auto_scroll()

    def start_tool(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolCardWidget:
        """Mount a new running tool card."""
        widget = ToolCardWidget(
            call_id,
            name,
            arguments,
            ascii_only=self.ascii_only,
            no_color=self.no_color,
        )
        self._active_tools[call_id] = widget
        self.tool_cards[call_id] = widget
        self.mount(widget)
        self.check_auto_scroll()
        return widget

    def complete_tool(
        self,
        call_id: str,
        status: str,
        result: str,
        duration: float | None = None,
        preview: dict[str, Any] | None = None,
        artifact_path: str | None = None,
    ) -> None:
        """Complete an existing tool card or create completed card."""
        widget = self._active_tools.pop(call_id, None)
        if widget is not None:
            widget.complete(status, result, duration, preview, artifact_path)
        else:
            # Reconstruct from event
            widget = ToolCardWidget(
                call_id,
                "tool",
                {},
                ascii_only=self.ascii_only,
                no_color=self.no_color,
            )
            widget.complete(status, result, duration, preview, artifact_path)
            self.mount(widget)
        self.tool_cards[call_id] = widget
        self.check_auto_scroll()

    def add_notice(self, title: str, message: str, kind: str = "info") -> None:
        """Mount system notice."""
        widget = SystemNoticeWidget(title, message, kind=kind, no_color=self.no_color)
        self.mount(widget)
        self.check_auto_scroll()

    def load_history_cards(self, cards: list[dict[str, Any]]) -> None:
        """Consume the actual DisplayCard schema emitted by the service."""
        for card in cards:
            kind = card.get("kind")
            content = card.get("content", card.get("text", ""))
            metadata = card.get("metadata") or {}
            if kind in {"user", "user_message"}:
                self.add_user_message(content)
            elif kind in {"assistant", "assistant_message"}:
                self.start_assistant_message(content)
                self.finish_assistant_message()
            elif kind == "thinking":
                self.start_thinking(content)
                self.finish_thinking()
            elif kind in {"tool", "tool_call"}:
                call_id = metadata.get("call_id", card.get("call_id", ""))
                tool = self.start_tool(call_id, metadata.get("name", card.get("name", "tool")),
                                       metadata.get("arguments", card.get("arguments", {})))
                tool.complete(card.get("status", "success"), content,
                               preview=metadata, artifact_path=metadata.get("output_ref"))
                self._active_tools.pop(call_id, None)
            else:
                self.add_notice(card.get("title", "运行记录"), content)
        self.check_auto_scroll()

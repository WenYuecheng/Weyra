"""Message widgets for User prompts, streamed Assistant answers, and System notices."""

from __future__ import annotations

import time
from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Markdown, Static


class UserMessageWidget(Static):
    """User prompt widget with Starship / Powerlevel10k pill aesthetic."""

    def __init__(self, content: str, *, no_color: bool = False, **kwargs) -> None:
        """初始化用户消息气泡组件。"""
        super().__init__(**kwargs)
        self.content = content
        self.no_color = no_color

    def render(self) -> Text:
        """渲染带 Powerlevel10k 胶囊徽章的用户输入提示词富文本。"""
        if self.no_color:
            t = Text(f"> [USER]\n{self.content}")
            return t

        t = Text.from_markup("[bold #00f2fe]╭─[/] [bold black on #00f2fe] ⚡ USER [/]\n[bold #00f2fe]╰─❯[/] ")
        t.append(self.content, style="#f8fafc")
        return t


class AssistantMessageWidget(Vertical):
    """Assistant message container with throttled (50ms/20fps) Markdown streaming."""

    def __init__(self, initial_text: str = "", *, no_color: bool = False, **kwargs) -> None:
        """初始化助手流式回复组件（内置 50ms/20fps 节流渲染）。"""
        super().__init__(**kwargs)
        self._buffer: list[str] = []
        self._rendered_text: str = initial_text
        self._last_flush_time: float = 0.0
        self._flush_interval: float = 0.05  # 50ms = 20 fps throttle
        self._is_finished: bool = False
        self.no_color = no_color
        self._markdown = Markdown(initial_text or "...")
        self._header = Static("", classes="message-label")

    def compose(self):
        """组合助手消息头部徽章与 Markdown 正文容器。"""
        self._update_header()
        yield self._header
        yield self._markdown

    def _update_header(self) -> None:
        """刷新助手回复顶部的 STREAMING / COMPLETE 状态徽章。"""
        if self.no_color:
            self._header.update(Text("[WEYRA ASSISTANT]", style="bold"))
        else:
            if self._is_finished:
                markup = (
                    "[bold #c084fc]╭─[/] [bold black on #c084fc] 🤖 WEYRA ASSISTANT [/] "
                    "[bold #10b981]● COMPLETE[/]"
                )
            else:
                markup = (
                    "[bold #c084fc]╭─[/] [bold black on #c084fc] 🤖 WEYRA ASSISTANT [/] "
                    "[bold #fbbf24]⠋ STREAMING[/]"
                )
            self._header.update(Text.from_markup(markup))

    def on_mount(self) -> None:
        """组件挂载时启动定时缓冲区冲刷定时器。"""
        self.set_interval(self._flush_interval, self._flush_buffer)
        self.call_after_refresh(self._render_markdown)

    def _render_markdown(self) -> None:
        """将当前累积的回复文本渲染为 Markdown 视图。"""
        if self._markdown.is_mounted:
            self._markdown.update(self._rendered_text or ("(无回复内容)" if self._is_finished else "…"))

    def append_text(self, delta: str) -> None:
        """Buffer streamed text delta and throttled-render."""
        if self._is_finished:
            return
        self._buffer.append(delta)
        now = time.monotonic()
        if now - self._last_flush_time >= self._flush_interval:
            self._flush_buffer()

    def _flush_buffer(self) -> None:
        """将增量字符缓冲区合并并冲刷至已渲染文本，重置计时器。"""
        if not self._buffer:
            return
        self._rendered_text += "".join(self._buffer)
        self._buffer.clear()
        self._last_flush_time = time.monotonic()
        self._render_markdown()

    def finish(self, final_text: str | None = None) -> None:
        """Finalize message stream, immediately flushing all remaining text."""
        self._is_finished = True
        if final_text is not None:
            self._rendered_text = final_text
            self._buffer.clear()
        else:
            if self._buffer:
                self._rendered_text += "".join(self._buffer)
                self._buffer.clear()
        self._update_header()
        self._render_markdown()


class SystemNoticeWidget(Static):
    """System banner for compaction, retry resets, or warnings in BearCode cyber style."""

    def __init__(self, title: str, message: str, *, kind: str = "info", no_color: bool = False, **kwargs) -> None:
        """初始化系统通知与压缩横幅组件。"""
        super().__init__(**kwargs)
        self.notice_title = title
        self.notice_message = message
        self.kind = kind
        self.no_color = no_color

    def render(self) -> Text:
        """渲染系统通知横幅富文本（支持 info, warning, error, compaction 等风格）。"""
        if self.no_color:
            return Text(f"[{self.kind.upper()}] {self.notice_title}: {self.notice_message}")

        kind_styles = {
            "info": ("✦ INFO", "#00f2fe", "#0b0f19"),
            "warning": ("▲ NOTICE", "#fbbf24", "#0b0f19"),
            "error": ("✖ ERROR", "#f43f5e", "#ffffff"),
            "compaction": ("🗜 COMPACT", "#c084fc", "#0b0f19"),
        }
        tag, bg, fg = kind_styles.get(self.kind, ("✦ INFO", "#00f2fe", "#0b0f19"))
        t = Text.from_markup(f"[bold {fg} on {bg}] {tag} [/] [bold #f8fafc]{self.notice_title}[/] [dim #64748b]·[/] ")
        t.append(self.notice_message, style="#94a3b8")
        return t

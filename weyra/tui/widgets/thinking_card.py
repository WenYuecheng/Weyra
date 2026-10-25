"""Collapsible Thinking Card for genuine model reasoning tokens."""

from __future__ import annotations

import time
from textual.content import Content
from textual.widgets import Collapsible, Static


class ThinkingCardWidget(Collapsible):
    """Collapsible card displaying genuine model thinking / reasoning."""

    def __init__(
        self,
        initial_text: str = "",
        *,
        no_color: bool = False,
        **kwargs,
    ) -> None:
        """初始化可折叠模型思考卡片组件 (DeepSeek-R1 CoT Collapsible Widget)。"""
        self.no_color = no_color
        self._start_time = time.monotonic()
        self._thinking_text = initial_text
        self._is_active = True
        self._content_static = Static(initial_text or "思考中...", classes="thinking-content", markup=False)

        initial_title = self._format_title(0.0)
        super().__init__(
            self._content_static,
            title=initial_title,
            collapsed=True,
            **kwargs,
        )

    def _format_title(self, elapsed: float) -> Content | str:
        """格式化思考卡片标题栏徽章与耗时指示器。"""
        if self.no_color:
            return f"[THINKING] 思考中 ({elapsed:.1f}s)..."
        return Content.from_markup(
            f"[bold black on #c084fc] ⠋ REASONING [/] [bold #c084fc]› DeepSeek-R1 CoT ({elapsed:.1f}s)...[/]"
        )

    def append_delta(self, delta: str) -> None:
        """Stream new thinking token."""
        if not self._is_active:
            return
        self._thinking_text += delta
        elapsed = time.monotonic() - self._start_time
        self.title = self._format_title(elapsed)
        self._content_static.update(self._thinking_text)

    def finish(self, final_text: str | None = None) -> None:
        """Finish thinking phase and auto-collapse."""
        self._is_active = False
        if final_text is not None:
            self._thinking_text = final_text
        self._content_static.update(self._thinking_text or "(无思考细节)")
        char_count = len(self._thinking_text)
        if self.no_color:
            self.title = f"[THINKING] 已折叠思考过程 ({char_count} 字)"
        else:
            self.title = Content.from_markup(
                f"[dim #64748b]💡 THINKING ARCHIVED › ({char_count} chars CoT)[/dim #64748b]"
            )

"""可折叠模型链式思考卡片组件 (Collapsible Reasoning & CoT Card Widget)。

【架构定位】
专门用于呈现 DeepSeek-R1 等推理模型的原生思考过程 (Reasoning Tokens / Chain-of-Thought)。

【开源设计思想与架构血统】
借鉴 DeepSeek-Harness 对推理模型的交互设计准则：
1. 思考过程具有高 Token 密度但并非最终答案本身；
2. 在流式推理阶段实时展开并更新计时器徽章（如 `DeepSeek-R1 CoT (2.3s)...`）；
3. 推理完成后自动折叠为紧凑徽章（如 `THINKING ARCHIVED › (1,240 chars CoT)`），
   既保证用户随时可展开追溯推理逻辑，又避免污染主聊天窗口的阅读体验。
"""

from __future__ import annotations

import time
from textual.content import Content
from textual.widgets import Collapsible, Static


class ThinkingCardWidget(Collapsible):
    """模型链式思考与深度推理折叠卡片组件 (DeepSeek-R1 Reasoning Card Widget)。

    【架构定位】
    封装 Textual Collapsible，实时接收思考 Token 增量并更新耗时标题。
    """

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
        """流式追加思考过程 Token 增量并动态更新耗时秒数 (Append Reasoning Token Delta)。

        Args:
            delta: 思考增量字符片段。
        """
        if not self._is_active:
            return
        self._thinking_text += delta
        elapsed = time.monotonic() - self._start_time
        self.title = self._format_title(elapsed)
        self._content_static.update(self._thinking_text)

    def finish(self, final_text: str | None = None) -> None:
        """终结思考阶段并自动转为折叠归档标题 (Finalize Thinking & Auto-collapse)。

        Args:
            final_text: 可选的全量最终思考文本。
        """
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

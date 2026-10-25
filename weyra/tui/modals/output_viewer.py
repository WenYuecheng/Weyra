"""Fullscreen paginated viewer for large tool outputs and artifacts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from weyra.presentation import OutputPage, paginate_text


class OutputViewerModal(ModalScreen[None]):
    """Fullscreen modal to inspect full tool outputs and artifacts with pagination."""

    BINDINGS = [
        Binding("escape", "close", "关闭", priority=True),
        Binding("pageup", "prev_page", "上一页"),
        Binding("pagedown", "next_page", "下一页"),
    ]

    def __init__(
        self,
        title: str,
        full_content: str,
        *,
        artifact_id: str = "output",
        page_loader: Callable[[int], Awaitable[OutputPage]] | None = None,
        no_color: bool = False,
        **kwargs,
    ) -> None:
        """初始化大产物全屏分页查看弹窗。"""
        super().__init__(**kwargs)
        self.viewer_title = title
        self.full_content = full_content
        self.artifact_id = artifact_id
        self.page_loader = page_loader
        self.no_color = no_color
        self._has_more = False
        self.current_offset = 0
        self.page_size = 200
        self._content_static = Static()
        self._page_label = Static()

    def compose(self) -> ComposeResult:
        """组合分页文本查看区、页码指示与翻页按钮。"""
        with Vertical(classes="modal-dialog", id="output-modal-container"):
            if self.no_color:
                title = Text(f"[OUTPUT VIEWER] {self.viewer_title}")
            else:
                title = Text.from_markup(
                    f"[bold black on #00f2fe] 📋 FULL OUTPUT VIEWER [/] [bold #00f2fe]› {self.viewer_title}[/]"
                )
            yield Static(title, classes="modal-title")
            with VerticalScroll(id="output-scroll"):
                yield self._content_static
            with Horizontal(classes="modal-buttons"):
                yield self._page_label
                yield Button("◀ 上一页 (PageUp)", id="btn-prev")
                yield Button("下一页 (PageDown) ▶", id="btn-next")
                yield Button("关闭 (Esc)", id="btn-close", variant="primary")

    async def on_mount(self) -> None:
        """组件挂载时加载并渲染第一页内容。"""
        await self._render_page()

    async def _render_page(self) -> None:
        """按当前偏移量计算切片、格式化行号并刷新翻页按钮可用状态。"""
        if self.page_loader:
            try:
                page = await self.page_loader(self.current_offset)
            except (OSError, ValueError):
                self._content_static.update("完整输出不可用，文件可能已被移除。")
                return
        else:
            page = paginate_text(
                self.full_content,
                self.artifact_id,
                offset=self.current_offset,
                limit=self.page_size,
            )
        self._has_more = page.has_more
        self.current_offset = page.offset
        self.page_size = page.limit
        t = Text()
        start_line = page.offset + 1
        for idx, line in enumerate(page.lines):
            line_no = f"{start_line + idx:4d} │ "
            t.append(line_no, style="dim #64748b" if not self.no_color else "dim")
            t.append(line + "\n")

        self._content_static.update(t)
        end_line = min(page.total_lines, page.offset + len(page.lines))
        self._page_label.update(f"行 {start_line}-{end_line} / 共 {page.total_lines} 行")

        btn_prev = self.query_one("#btn-prev", Button)
        btn_next = self.query_one("#btn-next", Button)
        btn_prev.disabled = self.current_offset == 0
        btn_next.disabled = not page.has_more

    async def action_prev_page(self) -> None:
        """切换至上一页。"""
        if self.current_offset > 0:
            self.current_offset = max(0, self.current_offset - self.page_size)
            await self._render_page()

    async def action_next_page(self) -> None:
        """切换至下一页。"""
        if self._has_more:
            self.current_offset += self.page_size
            await self._render_page()

    def action_close(self) -> None:
        """关闭查看弹窗。"""
        self.dismiss(None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """处理翻页与关闭按钮点击事件。"""
        if event.button.id == "btn-prev":
            await self.action_prev_page()
        elif event.button.id == "btn-next":
            await self.action_next_page()
        else:
            self.action_close()

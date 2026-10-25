"""Model picker and route inspector modal."""

from __future__ import annotations

from typing import Any
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static


class ModelPickerModal(ModalScreen[None]):
    """Modal displaying configured model routes and parameters."""

    BINDINGS = [
        Binding("escape", "close", "关闭", priority=True),
    ]

    def __init__(self, routes: dict[str, Any], *, no_color: bool = False, **kwargs) -> None:
        """初始化模型路由配置与容量参数查看弹窗。"""
        super().__init__(**kwargs)
        self.routes = routes
        self.no_color = no_color
        self._table = DataTable(cursor_type="row")

    def compose(self) -> ComposeResult:
        """组合模型路由表格与关闭操作按钮。"""
        with Vertical(classes="modal-dialog"):
            if self.no_color:
                title = Text("[MODEL CONFIG] Model Routing & Parameters")
            else:
                title = Text.from_markup(
                    "[bold black on #38bdf8] ⚡ MODEL & ROUTING CONFIG [/] [bold #38bdf8]› Dual-SDK Routes[/]"
                )
            yield Static(title, classes="modal-title")
            yield self._table
            with Horizontal(classes="modal-buttons"):
                yield Button("关闭 (Esc)", id="btn-close", variant="primary")

    def on_mount(self) -> None:
        """组件挂载时填充各个用途（Purpose）绑定的 Provider 与上下文窗口容量数据。"""
        self._table.add_columns("用途 (Purpose)", "Provider", "Model ID", "Context Window", "Max Output")
        for purpose, info in self.routes.items():
            prov = info.get("provider", "unknown")
            model = info.get("model", "unknown")
            ctx_win = str(info.get("context_window", "unknown"))
            max_out = str(info.get("max_output_tokens", "unknown"))
            self._table.add_row(purpose, prov, model, ctx_win, max_out)
        self._table.focus()

    def action_close(self) -> None:
        """关闭模型配置弹窗。"""
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """响应按钮点击关闭弹窗。"""
        self.action_close()

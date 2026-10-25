"""Approval modal for sensitive tool execution."""

from __future__ import annotations

import json
from typing import Any
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ApprovalModal(ModalScreen[bool]):
    """Modal dialog prompting user approval for sensitive tool operations.

    Default focus is strictly on Reject.
    """

    BINDINGS = [
        Binding("escape", "reject", "拒绝", priority=True),
        Binding("n", "reject", "拒绝", priority=True),
        Binding("y", "approve", "批准", priority=True),
    ]

    def __init__(
        self,
        request_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        permission: str = "write",
        *,
        no_color: bool = False,
        **kwargs,
    ) -> None:
        """初始化敏感操作审批弹窗。"""
        super().__init__(**kwargs)
        self.request_id = request_id
        self.tool_name = tool_name
        self.arguments = arguments
        self.permission = permission
        self.no_color = no_color

    def compose(self) -> ComposeResult:
        """组合审批弹窗标题、工具参数与拒绝/批准操作按钮。"""
        with Vertical(classes="modal-dialog"):
            if self.no_color:
                title_text = Text("[CONFIRMATION REQUIRED] Tool Approval Required")
            else:
                title_text = Text.from_markup(
                    "[bold black on #f43f5e] 🛡 CONFIRMATION REQUIRED [/] [bold #f43f5e]› Tool Execution Approval[/]"
                )
            yield Static(title_text, classes="modal-title")

            info = Text()
            if self.no_color:
                info.append("High-risk operation requires explicit authorization:\n\n")
                info.append(f"Tool: {self.tool_name}\n")
                info.append(f"Permission: {self.permission}\n\n")
                info.append("Parameters:\n")
                arg_str = json.dumps(self.arguments, ensure_ascii=False, indent=2)
                info.append(f"{arg_str}\n\n")
                info.append("Authorize this tool to execute in current workspace?\n")
            else:
                info.append("High-risk operation requires explicit authorization:\n\n", style="bold #f43f5e")
                info.append("▶ Tool: ", style="bold #fbbf24")
                info.append(f"{self.tool_name}\n", style="bold #00f2fe")
                info.append("▶ Permission: ", style="bold #fbbf24")
                info.append(f"{self.permission}\n\n", style="bold #f43f5e")
                info.append("▶ Parameters (Arguments):\n", style="bold #38bdf8")
                arg_str = json.dumps(self.arguments, ensure_ascii=False, indent=2)
                info.append(f"{arg_str}\n\n", style="dim #64748b")
                info.append("Authorize this tool to execute in current workspace?\n", style="bold #34d399")

            yield Static(info)

            with Horizontal(classes="modal-buttons"):
                yield Button("✖ 拒绝 (Esc / N)", id="btn-reject", variant="error")
                yield Button("✔ 批准 (Y)", id="btn-approve", variant="success")

    def on_mount(self) -> None:
        """组件挂载时将焦点默认锁定在拒绝按钮上，防止误回车批准。"""
        # Default focus MUST be on Reject to prevent accidental approvals
        self.query_one("#btn-reject", Button).focus()

    def action_reject(self) -> None:
        """拒绝审批并关闭弹窗（返回 False）。"""
        self.dismiss(False)

    def action_approve(self) -> None:
        """批准操作并关闭弹窗（返回 True）。"""
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """处理批准或拒绝按钮点击事件。"""
        if event.button.id == "btn-approve":
            self.action_approve()
        else:
            self.action_reject()

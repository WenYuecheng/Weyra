"""Help and shortcut cheat-sheet modal."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from weyra.tui.commands import TUI_COMMANDS


class HelpModal(ModalScreen[None]):
    """Modal displaying Weyra TUI keyboard shortcuts and slash commands."""

    BINDINGS = [
        Binding("escape", "close", "关闭", priority=True),
    ]

    def __init__(self, *, no_color: bool = False, **kwargs) -> None:
        """初始化快捷键速查与帮助手册弹窗。"""
        super().__init__(**kwargs)
        self.no_color = no_color

    def compose(self) -> ComposeResult:
        """组合快捷键表格、斜杠命令一览表与关闭按钮。"""
        with Vertical(classes="modal-dialog"):
            if self.no_color:
                title = Text("[HELP] Weyra Shortcuts & Commands")
            else:
                title = Text.from_markup(
                    "[bold black on #00f2fe] ✦ WEYRA CHEAT SHEET [/] [bold #00f2fe]› Help & Keybindings[/]"
                )
            yield Static(title, classes="modal-title")
            with VerticalScroll():
                yield Static(self._build_shortcuts_table())
                yield Static("\n")
                yield Static(self._build_commands_table())
            with Horizontal(classes="modal-buttons"):
                yield Button("关闭 (Esc)", id="btn-close", variant="primary")

    def _build_shortcuts_table(self) -> Table:
        """构建全量键盘快捷键映射富文本表格。"""
        t = Table(title="[bold #00f2fe]键盘快捷键 (Keyboard Shortcuts)[/bold #00f2fe]", box=None, padding=(0, 2))
        t.add_column("按键", style="bold #00f2fe", no_wrap=True)
        t.add_column("功能说明", style="white")

        t.add_row("Enter / Ctrl + S", "发送当前输入的提示或指令")
        t.add_row("Shift+Enter / Alt+Enter / Ctrl+J", "换行 (支持多行文本与代码编写)")
        t.add_row("Ctrl + C", "任务运行中取消任务；空闲时清空草稿；草稿为空时退出")
        t.add_row("F2 / Ctrl + P", "打开会话管理器 (查看、切换与新建会话)")
        t.add_row("F3 / Ctrl + K", "打开技能目录 (查看已演化技能、版本及回滚)")
        t.add_row("F4", "查看模型路由、上下文窗口与 Provider 配置")
        t.add_row("F1", "打开本帮助速查手册")
        t.add_row("Tab", "命令推荐打开时补全高亮项；其他情况切换焦点")
        t.add_row("Up / Down", "命令推荐打开时移动选择；其他情况移动光标")
        t.add_row("Alt + Up / Down", "翻查本次运行的输入历史")
        t.add_row("Esc", "恢复草稿；关闭命令推荐；关闭弹窗")
        return t

    def _build_commands_table(self) -> Table:
        """构建内置支持的全部斜杠命令及参数说明表格。"""
        t = Table(title="[bold #c084fc]斜杠命令 (Slash Commands)[/bold #c084fc]", box=None, padding=(0, 2))
        t.add_column("命令", style="bold #c084fc", no_wrap=True)
        t.add_column("说明", style="#94a3b8")

        for cmd in TUI_COMMANDS:
            cmd_display = f"{cmd.name} {cmd.args_usage}".strip()
            t.add_row(cmd_display, cmd.description)
        return t

    def action_close(self) -> None:
        """关闭帮助弹窗。"""
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """响应按钮点击关闭弹窗。"""
        self.action_close()

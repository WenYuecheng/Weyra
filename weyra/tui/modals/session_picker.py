"""Session picker modal for listing, resuming, and creating sessions."""

from __future__ import annotations

import datetime
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from weyra.service import SessionSummary


class SessionPickerModal(ModalScreen[str | None]):
    """Modal dialog for session selection and creation with BearCode styling."""

    BINDINGS = [
        Binding("escape", "cancel", "取消", priority=True),
        Binding("ctrl+n", "create_new", "新建会话", priority=True),
    ]

    def __init__(self, sessions: list[SessionSummary], current_session_id: str, *, no_color: bool = False, **kwargs) -> None:
        """初始化会话切换与管理弹窗。

        【架构定位】
        负责绑定持久化会话快照列表、标识活跃会话并初始化表格视图组件。

        【开源设计思想与架构血统】
        深度对齐 Pi Agent 的会话持久化与分支查看模型（Pi Session DAG Architecture）。
        Pi Agent 将每次对话视为 Session Tree 上的节点或独立分支，本弹窗呈现所有离线持久化的
        会话根/叶节点摘要（`SessionSummary`），支持开发者无缝切回历史分支或启动全新会话。

        Args:
            sessions: 持久化会话摘要列表，包含各会话的 ID、消息条数与更新时间戳。
            current_session_id: 当前正在运行的活跃会话标识。
            no_color: 是否禁用 ANSI 颜色渲染（无色彩无障碍模式）。
            **kwargs: 传递给 `ModalScreen` 基类的通用界面布局参数。
        """
        super().__init__(**kwargs)
        self.sessions = sessions
        self.current_session_id = current_session_id
        self.no_color = no_color
        self._table = DataTable(cursor_type="row")

    def compose(self) -> ComposeResult:
        """构建会话选择器弹窗的 UI 组件树。

        【架构定位】
        声明式渲染模态框主体：顶部标题横幅、中间会话列表数据表格以及底部操作按钮栏。

        Returns:
            ComposeResult: Textual 界面组件流，供引擎挂载至 DOM 树。
        """
        with Vertical(classes="modal-dialog"):
            if self.no_color:
                title = Text("[SESSION MANAGER] Sessions History")
            else:
                title = Text.from_markup(
                    "[bold black on #c084fc] ◆ SESSION MANAGER [/] [bold #c084fc]› Sessions History[/]"
                )
            yield Static(title, classes="modal-title")
            yield self._table
            with Horizontal(classes="modal-buttons"):
                yield Button("➕ 新建会话 (Ctrl+N)", id="btn-new", variant="primary")
                yield Button("关闭 (Esc)", id="btn-cancel")

    def on_mount(self) -> None:
        """界面挂载后的初始化钩子：填充会话数据并聚焦表格。

        【架构定位】
        在组件成功挂载到屏幕后执行数据装载，格式化时间戳并标示当前会话，确保键盘焦点直接落在表格上。

        【核心不变性与安全约束】
        时间戳解析异常容错：若时间格式异常则安全降级为原始字符串展示，杜绝因历史会话时间损坏引发 TUI 崩溃。
        """
        self._table.add_columns("会话 ID", "消息数", "更新时间", "当前状态")
        for s in self.sessions:
            try:
                time_str = datetime.datetime.fromtimestamp(float(s.updated)).strftime("%m-%d %H:%M")
            except (ValueError, TypeError, OverflowError, OSError):
                time_str = str(s.updated or "—")
            is_current = "★ 当前会话" if s.session_id == self.current_session_id else ""
            self._table.add_row(s.session_id, str(s.message_count), time_str, is_current, key=s.session_id)
        self._table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """处理会话数据表格的行选中（回车/双击）事件。

        【架构定位】
        将选中的行主键（即 `session_id`）作为弹窗返回值传递给父级 Screen 并关闭模态框。

        Args:
            event: 数据表格行选中事件对象，携带被选中行的 `row_key`。
        """
        if event.row_key and event.row_key.value:
            self.dismiss(str(event.row_key.value))

    def action_cancel(self) -> None:
        """执行取消动作：关闭弹窗且不切换会话。

        【架构定位】
        响应 Escape 按键或取消按钮，向父 Screen 返回 `None`，保持当前会话不变。
        """
        self.dismiss(None)

    def action_create_new(self) -> None:
        """执行新建会话动作：通知系统重置并启动新分支。

        【架构定位】
        响应 Ctrl+N 按键或新建按钮，返回预定义的特殊魔术标识 `__new__`。
        父级应用收到该值后将初始化全新的持久化会话记录，重置 Session DAG 游标。
        """
        self.dismiss("__new__")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """路由弹窗底部操作按钮的点击事件。

        【架构定位】
        根据触发事件的按钮 ID 分发至 `action_create_new` 或 `action_cancel`。

        Args:
            event: 按钮点击事件对象。
        """
        if event.button.id == "btn-new":
            self.action_create_new()
        else:
            self.action_cancel()

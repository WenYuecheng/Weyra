"""Skill viewer and version rollback modal."""

from __future__ import annotations

from typing import Any
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static


class SkillViewerModal(ModalScreen[tuple[str, str] | None]):
    """Modal displaying project skill catalog with optional version rollback."""

    BINDINGS = [
        Binding("escape", "close", "关闭", priority=True),
    ]

    def __init__(self, skills_data: list[dict[str, Any]], *, no_color: bool = False, **kwargs) -> None:
        """初始化技能仓库与版本回滚模态弹窗。

        【架构定位】
        绑定已注册技能元数据列表，初始化表格与详情面板，支持动态高亮与版本回滚操作。

        【开源设计思想与架构血统】
        结合自演化技能生命周期（`SkillEvolution` / `OnlineSkillEvolution`）设计。
        提供直观的技能清单视图与版本切换支持，使开发者可在线查看各技能的 Prompt 描述、
        来源（内置或演化自学习）及版本演进路径。

        Args:
            skills_data: 技能描述字典列表，每项包含 `name`, `version`, `description`, `source`, `path` 等字段。
            no_color: 是否禁用 ANSI 颜色渲染。
            **kwargs: 传递给 `ModalScreen` 基类的布局参数。
        """
        super().__init__(**kwargs)
        self.skills_data = skills_data
        self.no_color = no_color
        self._table = DataTable(cursor_type="row")
        self._detail_static = Static("请在上方列表中选择技能查看详情", id="skill-detail")
        self._selected_skill: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        """构建技能查看器弹窗的组件树。

        【架构定位】
        声明式渲染技能管理对话框：顶部标题、中间技能数据表、下方技能详情描述区及底部操作按钮。

        Returns:
            ComposeResult: 挂载到界面的 Textual 组件流。
        """
        with Vertical(classes="modal-dialog"):
            if self.no_color:
                title = Text("[SKILLS] Registered & Evolvable Skills Catalog")
            else:
                title = Text.from_markup(
                    "[bold black on #34d399] ◆ EVOLVABLE SKILLS REGISTRY [/] [bold #34d399]› Version & Rollback[/]"
                )
            yield Static(title, classes="modal-title")
            yield self._table
            yield self._detail_static
            with Horizontal(classes="modal-buttons"):
                yield Button("↺ 回滚到此版本", id="btn-rollback", variant="warning", disabled=True)
                yield Button("关闭 (Esc)", id="btn-close")

    def on_mount(self) -> None:
        """界面挂载后的初始化回调：填充技能列表并初始化数据表。

        【架构定位】
        将 `skills_data` 列表映射为数据表的表格行（技能名称、版本、简短描述、来源），并默认聚焦表格。
        """
        self._table.add_columns("技能名称", "版本", "描述", "来源")
        for s in self.skills_data:
            name = s.get("name", "unknown")
            ver = s.get("version", "1.0.0")
            desc = s.get("description", "")
            src = s.get("source", "builtin")
            self._table.add_row(name, ver, desc[:40], src, key=name)
        self._table.focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """响应技能数据表格的光标高亮切换事件。

        【架构定位】
        当用户在表格中移动光标时实时触发，提取选中技能的完整元数据，刷新下方详情面板并启用回滚按钮。

        Args:
            event: 数据表高亮行变动事件，携带当前高亮行的 `row_key`。
        """
        if event.row_key and event.row_key.value:
            name = str(event.row_key.value)
            matched = next((s for s in self.skills_data if s.get("name") == name), None)
            if matched:
                self._selected_skill = matched
                t = Text()
                if self.no_color:
                    t.append(f"【{matched.get('name')}】 版本: {matched.get('version')}\n")
                    t.append(f"描述: {matched.get('description')}\n")
                    t.append(f"来源: {matched.get('source')} | 路径: {matched.get('path', '内置')}\n")
                else:
                    t.append(f"【{matched.get('name')}】 ", style="bold #00f2fe")
                    t.append(f"版本: {matched.get('version')}\n", style="bold #34d399")
                    t.append(f"描述: {matched.get('description')}\n", style="#38bdf8")
                    t.append(f"来源: {matched.get('source')} | 路径: {matched.get('path', '内置')}\n", style="dim #64748b")
                self._detail_static.update(t)
                btn = self.query_one("#btn-rollback", Button)
                btn.disabled = False

    def action_close(self) -> None:
        """执行关闭弹窗动作：关闭界面并不做任何回滚。

        【架构定位】
        响应 Escape 按键或取消按钮，向父级调用者返回 `None`。
        """
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """分发底部按钮点击事件。

        【架构定位】
        若点击“回滚到此版本”且存在选中技能，向父 Screen 返回 `(skill_name, version)` 二元组；
        若点击“关闭”则委托给 `action_close`。

        Args:
            event: 按钮点击事件。
        """
        if event.button.id == "btn-rollback" and self._selected_skill:
            name = self._selected_skill.get("name", "")
            ver = self._selected_skill.get("version", "")
            self.dismiss((name, ver))
        else:
            self.action_close()

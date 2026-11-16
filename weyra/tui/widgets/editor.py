"""多行交互式提示词编辑器、斜杠命令智能补全推荐与用户草稿防丢失保护系统 (Interactive Prompt Editor & Draft Guard)。

【架构定位】
终端界面 (TUI) 的核心用户输入门面，集成了多行文本编辑、Enter/Shift+Enter 发送/换行控制、
斜杠命令（Slash Command）实时下拉筛选推荐、临时草稿快照暂存与输入历史游标回溯。

【开源设计思想与架构血统】
1. 借鉴 Pi Agent (`packages/tui/`) 的轻量级交互协议：
   - Enter 发送，Shift+Enter/Alt+Enter 换行，支持终端常见的 Ctrl+J 多行编辑。
   - 快速输入 "/" 时自动匹配推荐候选集，避免用户手动记忆全部命令语法。
2. 借鉴 DeepSeek-Harness 的终端鲁棒性原则（Input Safety & Draft Protection）：
   - 当用户正在输入长篇代码任务时，若点击快捷 "/" 触发命令，原长篇输入将被封装为 `SavedDraft`
     原子暂存。用户若按 Esc 取消，立即无损还原选区与光标，杜绝丢失长文本的用户痛点。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message as TMessage
from textual.widgets import Button, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from weyra.tui.commands import TUICommand, match_commands


@dataclass
class SavedDraft:
    """用户临时草稿快照数据类 (Prompt Draft Snapshot)。

    【架构定位】
    在用户触发临时斜杠命令时，保存当前正在编辑的提示词文本、光标位置与历史游标，
    确保在用户取消或执行完临时操作后能够无损恢复现场。
    """

    text: str
    selection: Any
    cursor_location: tuple[int, int]
    history_cursor: int


class CommandRecommendationList(OptionList):
    """斜杠命令下拉匹配推荐列表组件 (Slash Command Recommendation Dropdown)。

    【架构定位】
    展示当前匹配到的斜杠命令候选集，支持键盘上下方向键循环滚动与 Tab/Enter 键快速补全。
    """


class PromptEditor(TextArea):
    """定制化多行提示词输入文本域组件 (Customized Multi-line Prompt Editor)。

    【架构定位】
    Weyra TUI 的主力文本编辑控件，继承自 Textual TextArea，重载了快捷键绑定与光标动作，
    实现 Enter 提交、Shift+Enter 换行、Tab 补全与历史记录切换。
    """

    BINDINGS = [
        Binding("enter", "submit_prompt", "发送", priority=True),
        Binding("ctrl+s", "submit_prompt", "发送", priority=True),
        Binding("shift+enter", "insert_newline", "换行", priority=True),
        Binding("alt+enter", "insert_newline", "换行", priority=True),
        Binding("ctrl+j", "insert_newline", "换行", priority=True),
        Binding("escape", "handle_escape", "关闭推荐/恢复草稿", priority=True),
        Binding("tab", "handle_tab", "补全", priority=True),
        Binding("ctrl+c", "cancel_or_clear", "取消/清空", priority=True),
        Binding("alt+up", "history_previous", "上一条"),
        Binding("alt+down", "history_next", "下一条"),
    ]

    class Submitted(TMessage):
        """提示词或斜杠命令提交事件 (Prompt Submission Event)。

        【架构定位】
        当用户在编辑框中敲击 Enter 或 Ctrl+S 且文本非空时派发，驱动底层 AgentLoop 开始新轮次执行。
        """

        def __init__(self, text: str) -> None:
            """初始化提交消息事件。"""
            super().__init__()
            self.text = text

    class CancelRequested(TMessage):
        """用户按下 Ctrl+C 触发请求取消或清空编辑框事件。"""

    @property
    def editor_area(self) -> EditorArea | None:
        """向上追溯查找所属的父级 EditorArea 容器实例。"""
        parent = self.parent
        while parent is not None:
            if isinstance(parent, EditorArea):
                return parent
            parent = parent.parent
        return None

    def action_submit_prompt(self) -> None:
        """响应 Enter 或 Ctrl+S 快捷键，触发提示词或斜杠命令提交。"""
        text = self.text
        if text.strip():
            if self.editor_area and self.editor_area.is_submitting:
                return
            if self.editor_area:
                self.editor_area.set_submitting(True)
            self.post_message(self.Submitted(text))

    def action_insert_newline(self) -> None:
        """响应 Shift+Enter 或 Alt+Enter 快捷键，在当前光标处插入换行符。"""
        start, end = self.selection.start, self.selection.end
        self.replace("\n", start, end, maintain_selection_offset=False)

    def action_handle_escape(self) -> None:
        """响应 Escape 快捷键，关闭斜杠命令自动补全列表或恢复被暂存的草稿。"""
        if self.editor_area:
            self.editor_area.handle_escape()

    def action_handle_tab(self) -> None:
        """响应 Tab 快捷键，若补全列表打开则完成补全，否则将焦点移至下一组件。"""
        if self.editor_area and self.editor_area.is_recommendation_open:
            self.editor_area.complete_recommendation()
        else:
            self.screen.focus_next()

    def action_cancel_or_clear(self) -> None:
        """响应 Ctrl+C 快捷键，派发 CancelRequested 事件。"""
        self.post_message(self.CancelRequested())

    def action_cursor_up(self) -> None:
        """响应方向上键：若补全列表开启则上移高亮项，否则上移行光标。"""
        if self.editor_area and self.editor_area.is_recommendation_open:
            self.editor_area.recommendation_up()
        else:
            super().action_cursor_up()

    def action_cursor_down(self) -> None:
        """响应方向下键：若补全列表开启则下移高亮项，否则下移行光标。"""
        if self.editor_area and self.editor_area.is_recommendation_open:
            self.editor_area.recommendation_down()
        else:
            super().action_cursor_down()

    def action_history_previous(self) -> None:
        """响应 Alt+Up 快捷键，切换到上一条输入历史记录。"""
        if self.editor_area:
            self.editor_area.history_prev()

    def action_history_next(self) -> None:
        """响应 Alt+Down 快捷键，切换到下一条输入历史记录。"""
        if self.editor_area:
            self.editor_area.history_next()


class EditorArea(Vertical):
    """底部多功能输入区复合容器 (Composite Bottom Input Container)。

    【架构定位】
    装配多行编辑框 (PromptEditor)、快捷斜杠按钮、提示词语法标签与命令推荐列表，
    统一调度草稿保护、命令补全与输入历史栈。
    """

    DEFAULT_HINT = (
        "[bold #00f2fe]╭─[/] [bold black on #00f2fe] ⚡ PROMPT [/] "
        "[bold #64748b]‹Enter 发送 · Shift+Enter 换行 · Tab 补全›[/]"
    )

    def __init__(self, *, no_color: bool = False, **kwargs) -> None:
        """初始化输入区域容器组件。"""
        super().__init__(**kwargs)
        self.no_color = no_color
        self.editor = PromptEditor(id="editor-input", show_line_numbers=False)
        self.recommendation_list = CommandRecommendationList(id="command-recommendations")
        self.recommendation_list.display = False
        hint_text = (
            Text("输入任务或 /命令 · Enter 发送 · Shift+Enter 换行")
            if no_color
            else Text.from_markup(self.DEFAULT_HINT)
        )
        self.status_label = Static(hint_text, id="editor-status")
        self.slash_button = Button("⚡ /", id="btn-quick-slash")
        # 每次点击必须重新聚焦编辑框，支持连续快速重复点击
        self.slash_button.active_effect_duration = 0

        self._history: list[str] = []
        self._history_cursor: int = -1
        self._current_draft: str = ""
        self._saved_draft: SavedDraft | None = None
        self._current_matches: list[TUICommand] = []
        self._suppress_recommendations_once: bool = False
        self._is_submitting: bool = False

    def compose(self):
        """组合输入栏顶部控制区、自动补全列表与多行编辑框。"""
        with Horizontal(id="editor-header"):
            yield self.slash_button
            yield self.status_label
        yield self.recommendation_list
        yield self.editor

    @property
    def has_saved_draft(self) -> bool:
        """判断当前是否存在因触发临时斜杠命令而被暂存保护的用户草稿。"""
        return self._saved_draft is not None

    @property
    def is_recommendation_open(self) -> bool:
        """判断斜杠命令推荐浮层是否处于显示状态。"""
        return bool(self.recommendation_list.display and self.recommendation_list.option_count > 0)

    @property
    def is_submitting(self) -> bool:
        """判断当前是否正处于提交响应等待状态（防重复击发）。"""
        return self._is_submitting

    def set_submitting(self, value: bool) -> None:
        """设置当前输入区域的提交锁定状态。"""
        self._is_submitting = value

    def set_history(self, history: list[str]) -> None:
        """初始化输入历史记录列表。"""
        self._history = list(history)
        self._history_cursor = -1

    def append_history(self, text: str) -> None:
        """向输入历史尾部追加一条非空且去重的新记录。"""
        if text.strip() and (not self._history or self._history[-1] != text.strip()):
            self._history.append(text.strip())
        self._history_cursor = -1

    def clear(self) -> None:
        """彻底清空编辑框内容、暂存草稿、历史游标并隐藏补全列表。"""
        self.editor.text = ""
        self._history_cursor = -1
        self._saved_draft = None
        self.hide_recommendations()
        self._reset_hint()

    def _reset_hint(self) -> None:
        """重置状态标签为默认提示词语法帮助文本。"""
        if self.no_color:
            self.status_label.update(Text("输入任务或 /命令 · Enter 发送 · Shift+Enter 换行"))
        else:
            self.status_label.update(Text.from_markup(self.DEFAULT_HINT))

    def get_text(self) -> str:
        """获取当前编辑框中的完整文本内容。"""
        return self.editor.text

    def set_text(self, text: str) -> None:
        """设置编辑框文本内容并将光标自动移至末尾。"""
        self.editor.text = text
        lines = text.split("\n")
        self.editor.cursor_location = (len(lines) - 1, len(lines[-1]))

    def trigger_slash_button(self) -> None:
        """响应点击快捷 '/' 按钮，暂存当前草稿并快速呼出命令推荐菜单。"""
        current_text = self.editor.text
        if not current_text:
            self.editor.text = "/"
            self.editor.cursor_location = (0, 1)
            self.editor.focus()
            self.show_recommendations(match_commands("/"))
        elif current_text.startswith("/"):
            self.editor.focus()
            parts = current_text.split(" ", 1)
            if len(parts) == 1:
                matches = match_commands(parts[0])
                if matches:
                    self.show_recommendations(matches)
        else:
            if self._saved_draft is None:
                self._saved_draft = SavedDraft(
                    text=current_text,
                    selection=self.editor.selection,
                    cursor_location=self.editor.cursor_location,
                    history_cursor=self._history_cursor,
                )
            self.editor.text = "/"
            self.editor.cursor_location = (0, 1)
            self.editor.focus()
            self.show_recommendations(match_commands("/"))

    def restore_draft(self) -> None:
        """无损还原被暂存保护的用户草稿文本、光标位置与选区。"""
        if self._saved_draft is not None:
            draft = self._saved_draft
            self._saved_draft = None
            self.editor.text = draft.text
            self.editor.selection = draft.selection
            self.editor.cursor_location = draft.cursor_location
            self._history_cursor = draft.history_cursor
        self.hide_recommendations()

    def discard_draft(self) -> None:
        """废弃被暂存的用户草稿并关闭补全。"""
        self._saved_draft = None
        self.hide_recommendations()

    def handle_escape(self) -> bool:
        """处理 Escape 键逻辑：优先恢复暂存草稿，其次关闭推荐列表。"""
        if self.has_saved_draft:
            self.restore_draft()
            return True
        if self.is_recommendation_open:
            self.hide_recommendations()
            return True
        return False

    def show_recommendations(self, matches: list[TUICommand]) -> None:
        """渲染并展开匹配的斜杠命令下拉选项列表。"""
        self._current_matches = list(matches)
        self.recommendation_list.clear_options()
        for cmd in matches:
            t = Text()
            if self.no_color:
                t.append(f"{cmd.name} {cmd.args_usage}  {cmd.description}".strip())
            else:
                t.append(cmd.name, style="bold #00f2fe")
                if cmd.args_usage:
                    t.append(f" {cmd.args_usage}", style="#38bdf8")
                t.append("  ")
                t.append(cmd.description, style="#94a3b8")
            self.recommendation_list.add_option(Option(t, id=cmd.name))
        if matches:
            self.recommendation_list.highlighted = 0
        self.recommendation_list.display = True

    def hide_recommendations(self) -> None:
        """隐藏斜杠命令推荐列表并清空临时匹配项。"""
        self.recommendation_list.display = False
        self.recommendation_list.clear_options()
        self._current_matches = []

    def recommendation_up(self) -> None:
        """向上移动补全推荐列表的高亮选中项（支持循环滚动）。"""
        if self.recommendation_list.option_count > 0:
            if self.recommendation_list.highlighted is None:
                self.recommendation_list.highlighted = self.recommendation_list.option_count - 1
            elif self.recommendation_list.highlighted > 0:
                self.recommendation_list.highlighted -= 1
            else:
                self.recommendation_list.highlighted = self.recommendation_list.option_count - 1

    def recommendation_down(self) -> None:
        """向下移动补全推荐列表的高亮选中项（支持循环滚动）。"""
        if self.recommendation_list.option_count > 0:
            if self.recommendation_list.highlighted is None:
                self.recommendation_list.highlighted = 0
            elif self.recommendation_list.highlighted < self.recommendation_list.option_count - 1:
                self.recommendation_list.highlighted += 1
            else:
                self.recommendation_list.highlighted = 0

    def complete_recommendation(self) -> None:
        """应用当前高亮的斜杠命令，自动补全至编辑框并将光标移至末尾。"""
        if not self.is_recommendation_open or not self._current_matches:
            return
        idx = self.recommendation_list.highlighted
        if idx is None or idx < 0 or idx >= len(self._current_matches):
            idx = 0
        cmd = self._current_matches[idx]
        completed = cmd.name + (" " if cmd.args_usage else "")
        self._suppress_recommendations_once = True
        self.editor.text = completed
        self.editor.cursor_location = (0, len(completed))
        self.hide_recommendations()

    def on_paste(self, event: events.Paste) -> None:
        """拦截并阻止粘贴事件向上冒泡至 App 顶层以防止内容二次重复输入 (Deduplicated Paste Guard)。

        Args:
            event: Textual 终端粘贴事件。
        """
        event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """处理快捷按钮点击事件。"""
        if event.button.id == "btn-quick-slash":
            self.trigger_slash_button()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """处理鼠标点击或回车选中特定补全项事件。"""
        if 0 <= event.option_index < len(self._current_matches):
            cmd = self._current_matches[event.option_index]
            completed = cmd.name + (" " if cmd.args_usage else "")
            self._suppress_recommendations_once = True
            self.editor.text = completed
            self.editor.cursor_location = (0, len(completed))
            self.hide_recommendations()
            self.editor.focus()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """监听编辑框文本变化，实时过滤与展开斜杠命令推荐。"""
        text = self.editor.text
        if self._suppress_recommendations_once:
            self._suppress_recommendations_once = False
            self.hide_recommendations()
            return

        if self.has_saved_draft and not text.startswith("/"):
            self.restore_draft()
            return

        if text.startswith("/") and "\n" not in text:
            parts = text.split(" ", 1)
            if len(parts) == 1:
                matches = match_commands(parts[0])
                if matches:
                    self.show_recommendations(matches)
                    return
        self.hide_recommendations()

    def on_submission_finished(self, success: bool, is_command: bool, submitted_text: str) -> None:
        """处理提示词或命令提交完成后的输入框清理与历史记录入库。"""
        self._is_submitting = False
        if success:
            self.append_history(submitted_text)
            # 延迟响应绝不能抹除提交后用户正在输入的新内容 (Draft Protection)
            if self.editor.text == submitted_text:
                if self.has_saved_draft:
                    self.restore_draft()
                else:
                    self.clear()
        else:
            # 失败时完整保留编辑框内容与暂存草稿，方便用户修改重试或按 Esc 恢复
            pass

    def history_prev(self) -> None:
        """回溯并切换到上一条输入历史记录 (Navigate To Previous Input History)。

        【开源设计思想与架构血统】
        参考 GNU Readline / Zsh 与 Pi Agent 终端交互习惯：
        首次向上翻动时，将当前编辑中的文本存入 `_current_draft`，方便向下翻回时恢复现场。
        """
        if not self._history:
            return
        if self._history_cursor == -1:
            self._current_draft = self.editor.text
            self._history_cursor = len(self._history) - 1
        elif self._history_cursor > 0:
            self._history_cursor -= 1
        self.set_text(self._history[self._history_cursor])

    def history_next(self) -> None:
        """向前并切换到下一条输入历史记录 (Navigate To Next Input History)。

        【开源设计思想与架构血统】
        参考 GNU Readline / Zsh 终端交互习惯：
        当光标回到最新一条之后，自动恢复之前暂存的未提交输入 `_current_draft`。
        """
        if not self._history or self._history_cursor == -1:
            return
        if self._history_cursor < len(self._history) - 1:
            self._history_cursor += 1
            self.set_text(self._history[self._history_cursor])
        else:
            self._history_cursor = -1
            self.set_text(self._current_draft)

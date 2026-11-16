"""终端底部快捷键指引与交互状态提示栏 (TUI Keybinding Footer Bar)。

【架构定位】
位于 TUI 视口最底部的常驻状态提示栏，采用高对比度色彩徽章展示常用全局快捷键映射（Enter、Shift+Enter、Ctrl+C、F1-F4），
并支持在执行耗时任务或特殊模式下临时显示自定义操作提示。
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


class FooterBar(Static):
    """常驻底部快捷键徽章与交互指引栏组件 (Footer Shortcut Badge Bar)。

    【架构定位】
    以胶囊徽章样式渲染全局功能键映射，响应窗口宽度自适应截断，保证终端信息精炼可读。
    """

    def __init__(self, *, no_color: bool = False, **kwargs) -> None:
        """初始化底部快捷键与提示状态栏。"""
        super().__init__(**kwargs)
        self.no_color = no_color
        self._custom_text: str | None = None

    def set_hint(self, text: str | None) -> None:
        """设置临时自定义提示文本，覆盖常规快捷键徽章。"""
        self._custom_text = text
        self.refresh()

    def render(self) -> Text:
        """渲染底部状态栏的富文本徽章与快捷键列表。"""
        if self._custom_text:
            return Text(self._custom_text, style="bold #fbbf24" if not self.no_color else "bold")

        shortcuts = [
            ("Enter", "发送", "#00f2fe"),
            ("Shift+Enter", "换行", "#c084fc"),
            ("Ctrl+C", "取消/清空", "#f43f5e"),
            ("F2", "会话", "#fbbf24"),
            ("F3", "技能", "#34d399"),
            ("F4", "模型", "#38bdf8"),
            ("F1", "帮助", "#64748b"),
        ]

        if self.no_color:
            t = Text("  ".join(f"[{key}] {label}" for key, label, _ in shortcuts))
            t.truncate(max(1, self.content_size.width), overflow="ellipsis")
            return t

        t = Text()
        for idx, (key, label, color) in enumerate(shortcuts):
            if idx > 0:
                t.append("  ", style="dim #334155")
            t.append(f" {key} ", style=f"bold black on {color}")
            t.append(f" {label}", style="#f8fafc")
        t.truncate(max(1, self.content_size.width), overflow="ellipsis")
        return t

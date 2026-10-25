"""TUI 自定义响应式组件库（TUI Custom Widgets）。

包含卡片式消息聊天流、自适应多行代码编辑器、底部状态与快捷键导航栏、思考链卡片与工具调用卡片。
"""

from .chat_view import ChatView

from .editor import EditorArea, PromptEditor
from .footer import FooterBar
from .message import AssistantMessageWidget, SystemNoticeWidget, UserMessageWidget
from .status_bar import StatusBar
from .thinking_card import ThinkingCardWidget
from .tool_card import ToolCardWidget

__all__ = [
    "AssistantMessageWidget",
    "ChatView",
    "EditorArea",
    "FooterBar",
    "PromptEditor",
    "StatusBar",
    "SystemNoticeWidget",
    "ThinkingCardWidget",
    "ToolCardWidget",
    "UserMessageWidget",
]

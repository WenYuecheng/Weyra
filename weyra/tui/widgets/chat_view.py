"""自适应滚动消息流容器、智能贴底锁定与历史卡片重构引擎 (Chat View Container)。"""

from __future__ import annotations

from typing import Any

from rich import box
from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from weyra.tui.widgets.message import AssistantMessageWidget, SystemNoticeWidget, UserMessageWidget
from weyra.tui.widgets.thinking_card import ThinkingCardWidget
from weyra.tui.widgets.tool_card import ToolCardWidget

LOGO_WEYRA = r"""
  ██╗    ██╗███████╗██╗   ██╗██████╗  █████╗ 
  ██║    ██║██╔════╝╚██╗ ██╔╝██╔══██╗██╔══██╗
  ██║ █╗ ██║█████╗   ╚████╔╝ ██████╔╝███████║
  ██║███╗██║██╔══╝    ╚██╔╝  ██╔══██╗██╔══██║
  ╚███╔███╔╝███████╗   ██║   ██║  ██║██║  ██║
   ╚══╝╚══╝ ╚══════╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝"""

LOGO_HARNESS = r"""
  ██╗  ██╗ █████╗ ██████╗ ███╗   ██╗███████╗███████╗███████╗
  ██║  ██║██╔══██╗██╔══██╗████╗  ██║██╔════╝██╔════╝██╔════╝
  ███████║███████║██████╔╝██╔██╗ ██║█████╗  ███████╗███████╗
  ██╔══██║██╔══██║██╔══██╗██║╚██╗██║██╔══╝  ╚════██║╚════██║
  ██║  ██║██║  ██║██║  ██║██║ ╚████║███████╗███████║███████║
  ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚══════╝"""


class ChatView(VerticalScroll):
    """用于承载消息流、思考链和工具卡片的垂直可滚动容器 (Chat View Scroll Container)。"""

    class PinChanged(Message):
        """视图滚动是否贴底锁定状态变更事件。"""

        def __init__(self, pinned: bool):
            """初始化滚动锁定事件。"""
            super().__init__()
            self.pinned = pinned

    def __init__(self, *, ascii_only: bool = False, no_color: bool = False, **kwargs) -> None:
        """初始化聊天主滚动容器组件。"""
        super().__init__(**kwargs)
        self.ascii_only = ascii_only
        self.no_color = no_color
        self._auto_scroll_pinned = True
        self._active_assistant: AssistantMessageWidget | None = None
        self._active_thinking: ThinkingCardWidget | None = None
        self._active_tools: dict[str, ToolCardWidget] = {}
        self.tool_cards: dict[str, ToolCardWidget] = {}

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """监听 Y 轴滚动位置，自动计算是否锁定贴底并广播 PinChanged 事件。"""
        super().watch_scroll_y(old_value, new_value)
        dist_from_bottom = self.max_scroll_y - self.scroll_y
        self._auto_scroll_pinned = dist_from_bottom <= 2
        self.post_message(self.PinChanged(self._auto_scroll_pinned))

    def check_auto_scroll(self) -> None:
        """检查滚动位置并在贴底状态下自动锁定滚动至最底部 (Auto-scroll Pin Guard)。

        【架构定位】
        负责在界面新增流式字符或子组件后维持智能贴底体验。

        【开源设计思想与架构血统】
        参考 Pi Agent (`packages/tui/`) 的流式滚动钉子机制 (Scroll Pinning)：
        当用户未手动向上翻阅历史消息时，实时流式内容自动推拉屏幕至底端；
        一旦用户向上滚轮查看历史，自动解除钉住，防止打断阅读体验。
        """
        if self._auto_scroll_pinned:
            self.call_after_refresh(self.scroll_end, animate=False)

    def jump_to_bottom(self) -> None:
        """强制重置贴底锁定状态并瞬间滚动至视口最底部 (Jump To Bottom & Pin)。

        【架构定位】
        响应用户点击跳到底部快捷操作或新任务击发。
        """
        self._auto_scroll_pinned = True
        self.scroll_end(animate=False)
        self.post_message(self.PinChanged(True))

    async def reset(self) -> None:
        """重置视图，清空所有活跃助手/思考/工具卡片并移除所有子组件。"""
        self._active_assistant = None
        self._active_thinking = None
        self._active_tools.clear()
        self.tool_cards.clear()
        await self.remove_children()

    def show_welcome(self, project_name: str) -> None:
        """渲染带有工作区规格与快捷命令指南的赛博朋克风格欢迎面板 (Render Cyberpunk Welcome Dashboard)。

        【架构定位】
        在会话初始化且无历史上下文时展示工作区身份、底层模型与操作指南。

        Args:
            project_name: 当前加载的工作区目录名或工程标识。
        """
        if self.no_color:
            t = Table.grid()
            t.add_row(Align.center("WEYRA - EVOLVABLE CODING AGENT HARNESS"))
            t.add_row(Align.center(f"Workspace: {project_name} | Dual-SDK Engine"))
            t.add_row("")
            t.add_row("Commands: /help, /session, /skills, /memory, /models, /usage, /exit")
            panel = Panel(
                t,
                title="[ WEYRA HARNESS · READY ]",
                subtitle="Type your request or slash command below",
                box=box.ROUNDED,
                padding=(1, 2),
            )
            self.mount(Static(panel, id="welcome-banner"))
            return

        specs_table = Table.grid(expand=True, padding=(0, 2))
        specs_table.add_column(style="bold #c084fc", no_wrap=True)
        specs_table.add_column(style="white")
        specs_table.add_column(style="bold #c084fc", no_wrap=True)
        specs_table.add_column(style="white")

        specs_table.add_row(
            "ENGINE", "[bold #38bdf8]DeepSeek-V3[/] [dim](deepseek-chat)[/dim]",
            "WORKSPACE", f"[bold #34d399]{project_name}[/]",
        )
        specs_table.add_row(
            "HARNESS", "Autonomous Tools / BM25 Evolution",
            "SANDBOX", "[bold #fbbf24]Permission Guard / Microcompact[/]",
        )

        cmd_table = Table.grid(expand=True, padding=(0, 2))
        cmd_table.add_column(style="bold #00f2fe", no_wrap=True)
        cmd_table.add_column(style="#64748b")
        cmd_table.add_column(style="bold #00f2fe", no_wrap=True)
        cmd_table.add_column(style="#64748b")

        cmd_table.add_row(
            "/help", "Show all shortcuts & command guides",
            "/skills", "Explore & invoke registered skills",
        )
        cmd_table.add_row(
            "/session", "Manage, switch & create sessions",
            "/memory", "Inspect persistent project memories",
        )
        cmd_table.add_row(
            "/models", "Inspect model routes & configs",
            "/usage", "Inspect real-time token expenditure",
        )
        cmd_table.add_row(
            "/goal", "View or set current session goal",
            "/exit", "Safely exit Weyra Agent",
        )

        main_layout = Table.grid(expand=True)
        main_layout.add_row(Align.center(f"[bold #00f2fe]{LOGO_WEYRA}[/bold #00f2fe]"))
        main_layout.add_row(Align.center(f"[bold #c084fc]{LOGO_HARNESS}[/bold #c084fc]"))
        main_layout.add_row("")
        main_layout.add_row(
            Align.center("[bold #38bdf8]Autonomous Coding Agent & Self-Evolving Evaluation Harness[/bold #38bdf8]")
        )
        main_layout.add_row(
            Align.center("[dim #64748b]Creator:[/] [bold #00f2fe]温悦成 (Wen Yuecheng)[/] [dim #64748b]| v1.0.0 | Dual-SDK Engine[/dim #64748b]")
        )
        main_layout.add_row("")
        main_layout.add_row("[bold #c084fc]SYSTEM SPECIFICATIONS[/bold #c084fc]")
        main_layout.add_row(specs_table)
        main_layout.add_row("")
        main_layout.add_row("[bold #00f2fe]QUICK COMMAND NAVIGATION[/bold #00f2fe]")
        main_layout.add_row(cmd_table)

        panel = Panel(
            main_layout,
            title="[bold black on #00f2fe] WEYRA-HARNESS [/] [bold black on #c084fc] v1.0.0 [/] [bold #34d399]● READY[/]",
            title_align="left",
            border_style="#334155",
            box=box.ROUNDED,
            padding=(1, 2),
            subtitle="[dim #64748b]Type your software engineering request or slash command below[/dim #64748b]",
            subtitle_align="center",
        )
        self.mount(Static(panel, id="welcome-banner"))

    def add_user_message(self, text: str) -> UserMessageWidget:
        """在消息流中挂载新的用户提示词卡片并自动滚动 (Mount User Message Bubble)。

        【架构定位】
        将用户输入的原始 Prompt 或命令包装为气泡组件挂载至视口容器。

        Args:
            text: 用户输入的提示词正文。

        Returns:
            UserMessageWidget: 挂载完成的用户消息组件实例。
        """
        for banner in self.query("#welcome-banner"):
            banner.remove()
        widget = UserMessageWidget(text, no_color=self.no_color)
        self.mount(widget)
        self.check_auto_scroll()
        return widget

    def start_assistant_message(self, initial_text: str = "") -> AssistantMessageWidget:
        """挂载并返回一个新的活跃助手流式消息容器 (Mount Streamed Assistant Message)。

        【架构定位】
        用于承载模型回复正文的流式 Markdown 渲染。

        Args:
            initial_text: 初始文本内容（默认为空）。

        Returns:
            AssistantMessageWidget: 挂载的活跃助手消息组件。
        """
        widget = AssistantMessageWidget(initial_text, no_color=self.no_color)
        self._active_assistant = widget
        self.mount(widget)
        self.check_auto_scroll()
        return widget

    def get_or_create_assistant_message(self) -> AssistantMessageWidget:
        """获取当前活跃的助手消息组件；若不存在则自动挂载新组件。"""
        if self._active_assistant is None:
            return self.start_assistant_message()
        return self._active_assistant

    def finish_assistant_message(self, final_text: str | None = None) -> None:
        """完成当前活跃助手消息的流式输出并解除活跃绑定。"""
        if self._active_assistant is not None:
            self._active_assistant.finish(final_text)
            self._active_assistant = None
            self.check_auto_scroll()

    def start_thinking(self, initial_text: str = "") -> ThinkingCardWidget:
        """挂载模型链式思考卡片以承载实时推理 Token (Mount DeepSeek-R1 CoT Thinking Card)。

        【架构定位】
        为模型的思考过程（Thinking / Reasoning Content）提供独立的折叠展示区域。

        【开源设计思想与架构血统】
        借鉴 DeepSeek-Harness 对 DeepSeek-R1 真实推理流的呈现理念：
        将推理内容与正式回答物理解耦，默认可折叠收起，避免冗长思考链污染交互主视口。

        Args:
            initial_text: 初始思考文本。

        Returns:
            ThinkingCardWidget: 挂载的模型推理卡片组件。
        """
        widget = ThinkingCardWidget(initial_text, no_color=self.no_color)
        self._active_thinking = widget
        self.mount(widget)
        self.check_auto_scroll()
        return widget

    def get_or_create_thinking(self) -> ThinkingCardWidget:
        """获取当前活跃的模型思考卡片组件；若不存在则自动挂载新卡片。"""
        if self._active_thinking is None:
            return self.start_thinking()
        return self._active_thinking

    def finish_thinking(self, final_text: str | None = None) -> None:
        """完成当前活跃思考卡片的流式输出并解除活跃绑定。"""
        if self._active_thinking is not None:
            self._active_thinking.finish(final_text)
            self._active_thinking = None
            self.check_auto_scroll()

    def start_tool(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolCardWidget:
        """挂载一个新的运行中工具调用折叠卡片 (Mount Tool Execution Card)。

        【架构定位】
        展示工具分发状态、调用参数及运行耗时。

        【开源设计思想与架构血统】
        参考 DeepSeek-Harness (`packages/deliverables/`) 的工具展示规范，
        在执行期间实时向用户反馈正在被调用的工具名称与入参。

        Args:
            call_id: 工具调用的唯一标识 ID。
            name: 工具名称。
            arguments: 工具调用实参字典。

        Returns:
            ToolCardWidget: 挂载的工具调用卡片组件。
        """
        widget = ToolCardWidget(
            call_id,
            name,
            arguments,
            ascii_only=self.ascii_only,
            no_color=self.no_color,
        )
        self._active_tools[call_id] = widget
        self.tool_cards[call_id] = widget
        self.mount(widget)
        self.check_auto_scroll()
        return widget

    def complete_tool(
        self,
        call_id: str,
        status: str,
        result: str,
        duration: float | None = None,
        preview: dict[str, Any] | None = None,
        artifact_path: str | None = None,
    ) -> None:
        """更新已有工具卡片的执行终态或重构已完成的历史工具卡片 (Complete Tool Execution Card)。

        【架构定位】
        将工具执行的输出、耗时、状态及代码差异 (Unified Diff) 回填到卡片中。

        Args:
            call_id: 工具调用唯一标识。
            status: 执行终态（'success' / 'failed' / 'denied' 等）。
            result: 工具执行结果文本。
            duration: 工具执行耗时（秒）。
            preview: 结构化预览字典（可能包含 diff、stats 等元数据）。
            artifact_path: 大文件产物落盘路径（若输出发生截断时提供）。
        """
        widget = self._active_tools.pop(call_id, None)
        if widget is not None:
            widget.complete(status, result, duration, preview, artifact_path)
        else:
            # 若活跃卡片已被析构则根据事件快照重构工具卡片
            widget = ToolCardWidget(
                call_id,
                "tool",
                {},
                ascii_only=self.ascii_only,
                no_color=self.no_color,
            )
            widget.complete(status, result, duration, preview, artifact_path)
            self.mount(widget)
        self.tool_cards[call_id] = widget
        self.check_auto_scroll()

    def add_notice(self, title: str, message: str, kind: str = "info") -> None:
        """向消息流中挂载系统级通知横幅 (Mount System Notice Banner)。

        【架构定位】
        展示上下文压缩、重试告警或运行时错误等系统级别事件。

        Args:
            title: 通知标题。
            message: 通知正文详情。
            kind: 通知类型（'info', 'warning', 'error', 'compaction'）。
        """
        widget = SystemNoticeWidget(title, message, kind=kind, no_color=self.no_color)
        self.mount(widget)
        self.check_auto_scroll()

    def load_history_cards(self, cards: list[dict[str, Any]]) -> None:
        """消费服务层导出的 DisplayCard 领域卡片序列重构历史视口 (Reconstruct Viewport from Display Cards)。

        【架构定位】
        会话恢复或切换时，将纯领域事件或快照投影生成的 DisplayCard 数组无损映射为 TUI 组件树。

        【开源设计思想与架构血统】
        参考 DeepSeek-Harness 的纯投影架构（Presenter Purity）：
        UI 层严禁反向解析复杂的底层 Session 日志，而是消费服务层标准化的 `DisplayCard` 投影模型。

        Args:
            cards: 服务层投影生成的卡片数据字典列表。
        """
        for card in cards:
            kind = card.get("kind")
            content = card.get("content", card.get("text", ""))
            metadata = card.get("metadata") or {}
            if kind in {"user", "user_message"}:
                self.add_user_message(content)
            elif kind in {"assistant", "assistant_message"}:
                self.start_assistant_message(content)
                self.finish_assistant_message()
            elif kind == "thinking":
                self.start_thinking(content)
                self.finish_thinking()
            elif kind in {"tool", "tool_call"}:
                call_id = metadata.get("call_id", card.get("call_id", ""))
                tool = self.start_tool(call_id, metadata.get("name", card.get("name", "tool")),
                                       metadata.get("arguments", card.get("arguments", {})))
                tool.complete(card.get("status", "success"), content,
                               preview=metadata, artifact_path=metadata.get("output_ref"))
                self._active_tools.pop(call_id, None)
            else:
                self.add_notice(card.get("title", "运行记录"), content)
        self.check_auto_scroll()

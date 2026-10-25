"""Weyra Textual 全屏交互式终端应用核心驱动（WeyraTUIApp）。

架构定位与设计哲学:
    本模块基于现代化异步终端框架 Textual 构建全屏自适应交互应用。
    负责管理终端视窗生命周期、键盘快捷键映射绑定、消息卡片视图（ChatView）、
    底部多行代码编辑器（PromptEditor）、全局状态栏（StatusBar）以及各类交互模态弹窗（Modals）。
    应用严格作为观察者与命令发起方对接 `WeyraAppService`，实现了高响应度、无卡顿的富终端交互。
"""

from __future__ import annotations


import asyncio
import json
import shlex
from typing import Any
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Button

from weyra.contracts import new_id
from weyra.events import AppEvent, Subscription
from weyra.service import WeyraAppService
from weyra.presentation import truncate_preview
from weyra.tui.modals import (
    ApprovalModal,
    HelpModal,
    ModelPickerModal,
    OutputViewerModal,
    SessionPickerModal,
    SkillViewerModal,
)
from weyra.tui.state import UIState
from weyra.tui.widgets import ChatView, EditorArea, FooterBar, PromptEditor, StatusBar


class WeyraTUIApp(App[None]):
    """Textual TUI for Weyra evolvable coding agent."""

    CSS_PATH = "theme.tcss"
    TITLE = "Weyra Agent"

    BINDINGS = [
        Binding("ctrl+p", "open_sessions", "会话", priority=True),
        Binding("f4", "open_models", "模型", priority=True),
        Binding("f2", "open_sessions", "会话", priority=True),
        Binding("f3", "open_skills", "技能", priority=True),
        Binding("ctrl+k", "open_skills", "技能", priority=True),
        Binding("f1", "open_help", "帮助", priority=True),
        Binding("ctrl+c", "handle_ctrl_c", "取消/清空/退出", priority=True),
    ]

    def __init__(
        self,
        service: WeyraAppService,
        *,
        ascii_only: bool = False,
        **kwargs,
    ) -> None:
        """初始化 Weyra 终端应用。

        【架构定位】
        装配后台应用服务实例、状态管理器（UIState）以及视窗组件树（状态栏、消息卡片流、
        输入编辑区、底栏及悬浮快速跳转按钮）。

        【开源设计思想与架构血统】
        深度践行 Pi Agent 极简微内核与纯异步交互设计（Microkernel & Async Reactive UI）。
        界面层与 Agent 核心逻辑完全解耦，TUI 仅作为观察者与命令发起方，所有交互均通过统一的
        `WeyraAppService` 进行事件订阅与指令分发，保证高并发流式输出下的终端零卡顿。

        Args:
            service: Weyra 核心应用层服务门面，提供统一的生命周期与总线接口。
            ascii_only: 是否强制采用 ASCII 兼容字符集（用于兼容老旧终端仿真器）。
            **kwargs: 透传给 Textual `App` 基类的通用配置参数。
        """
        super().__init__(**kwargs)
        self.service = service
        self.ui_state = UIState.create(service.project_path, ascii_only=ascii_only)
        self.ui_state.no_color = self.ui_state.no_color or self.no_color
        self.status_bar = StatusBar()
        self.chat_view = ChatView(ascii_only=ascii_only, no_color=self.ui_state.no_color)
        self.editor_area = EditorArea(no_color=self.ui_state.no_color)
        self.footer_bar = FooterBar(no_color=self.ui_state.no_color)
        self.jump_button = Button("↓ 返回最新", id="jump-latest")
        self.jump_button.display = False

        self._subscription: Subscription | None = None
        self._event_task: asyncio.Task | None = None
        self._active_approval_modal: ApprovalModal | None = None

    def compose(self) -> ComposeResult:
        """构建全屏终端应用的顶层组件布局树。

        【架构定位】
        声明式编排主界面布局：从上至下依次为全局状态栏（StatusBar）、自适应消息流视图（ChatView）、
        动态悬浮回到底部按钮（JumpButton）、输入编辑区域（EditorArea）以及底栏快捷键提示（FooterBar）。

        Returns:
            ComposeResult: 供 Textual 框架挂载的组件生成器。
        """
        yield self.status_bar
        yield self.chat_view
        yield self.jump_button
        yield self.editor_area
        yield self.footer_bar

    async def on_mount(self) -> None:
        """应用启动挂载完成后的生命周期钩子。

        【架构定位】
        执行服务启动、事件总线订阅、历史卡片重载及默认焦点指派。

        【开源设计思想与架构血统】
        对齐 DeepSeek-Harness 的事件流订阅及 Pi Agent 的历史记录恢复机制。
        启动时先拉取当前会话的快照状态，并通过有界事件队列建立订阅通道（`subscribe("tui_main")`），
        将已持久化的历史卡片一次性载入 ChatView，并将光标默认聚焦于输入编辑器。
        """
        # Start service
        snapshot = await self.service.start()
        self.ui_state.apply_snapshot(snapshot)
        self.status_bar.update_status(self.ui_state)

        # Subscribe to event stream
        snapshot, self._subscription = await self.service.subscribe("tui_main")
        self.ui_state.apply_snapshot(snapshot)
        self._through_seq = snapshot.through_seq
        self._generation = snapshot.session_generation
        self._event_task = asyncio.create_task(self._listen_events())

        # Load session history if present, otherwise show welcome banner
        history = await self.service.get_history()
        if history.cards:
            self.chat_view.load_history_cards(history.cards)
        else:
            self.chat_view.show_welcome(self.ui_state.project_name)

        # Set initial focus on editor
        self.editor_area.editor.focus()

    async def _listen_events(self) -> None:
        """后台异步事件监听循环。

        【架构定位】
        持续消费有界事件订阅队列中的 `AppEvent`，维持界面与底层 Agent 状态强一致。

        【开源设计思想与架构血统】
        实现 DeepSeek-Harness 严格的单调序列号（`seq`）与会话代数（`session_generation`）因果事件信封体系。
        1. 乱序/陈旧过滤：当 `event.seq <= self._through_seq` 或代数落后时安全丢弃。
        2. 背压与环形缓冲区滑移处理：若检测到 `needs_resync`，立即发起 `resubscribe` 重新同步完整快照与实时增量。
        """
        if not self._subscription:
            return
        try:
            while self.is_running:
                event = await self._subscription.get()
                if self._subscription.needs_resync:
                    snapshot, history, sub, live = await self.service.resubscribe("tui_main")
                    self._subscription = sub
                    self._through_seq = snapshot.through_seq
                    self._generation = snapshot.session_generation
                    await self.chat_view.reset()
                    self.chat_view.load_history_cards(history.cards)
                    self.ui_state.apply_snapshot(snapshot)
                    for item in live:
                        if item["kind"] == "text_delta":
                            self.chat_view.get_or_create_assistant_message().append_text(item["data"]["delta"])
                        elif item["kind"] == "thinking_delta":
                            self.chat_view.get_or_create_thinking().append_delta(item["data"]["delta"])
                        else:
                            data = item["data"]
                            self.chat_view.start_tool(data["call_id"], data.get("name", "tool"), data.get("arguments", {}))
                    pending_ids = {a["request_id"] for a in snapshot.pending_approvals}
                    if self._active_approval_modal and self._active_approval_modal.request_id not in pending_ids:
                        modal = self._active_approval_modal
                        self._active_approval_modal = None
                        modal.dismiss(False)
                    for approval in snapshot.pending_approvals:
                        if not self._active_approval_modal or self._active_approval_modal.request_id != approval["request_id"]:
                            self._handle_approval_requested(approval)
                    self.status_bar.update_status(self.ui_state)
                    continue
                if event is None:
                    break
                if event.seq <= self._through_seq or event.session_generation < self._generation:
                    continue
                self._through_seq = event.seq
                self._generation = event.session_generation
                self._dispatch_event(event)
        except asyncio.CancelledError:
            pass

    def _dispatch_event(self, event: AppEvent) -> None:
        """将结构化领域事件分发至具体的 UI 组件卡片。

        【架构定位】
        TUI 事件总线的分发中心，负责解析各种事件类型并实时渲染界面组件。

        【开源设计思想与架构血统】
        深度对齐 DeepSeek-Harness 事件模型与 Pi Agent 流式卡片展示：
        - `text_delta` / `thinking_delta`: 增量推入文本/思考流卡片并触发平滑自适应滚动；
        - `tool_start` / `tool_end`: 闭合文本块并创建工具执行卡片，展示工具耗时、中间修剪预览或外置制品链接；
        - `approval_requested` / `approval_resolved`: 触发或撤销安全审批弹窗；
        - `compaction_start` / `compaction_end`: 渲染 DeepSeek-Harness 8段式压缩与检查点保存通知；
        - `attempt_reset`: 网络或模型异常重试提示；
        - `service_error`: 统一错误横幅展示。

        Args:
            event: 携带领域载荷的结构化事件对象。
        """
        self.ui_state.apply_event(event)
        kind = event.kind
        data = event.data

        if kind == "text_delta":
            delta = data.get("delta", "")
            self.chat_view.get_or_create_assistant_message().append_text(delta)
            self.chat_view.check_auto_scroll()
        elif kind == "thinking_delta":
            delta = data.get("delta", "")
            self.chat_view.get_or_create_thinking().append_delta(delta)
        elif kind == "tool_start":
            self.chat_view.finish_assistant_message()
            self.chat_view.finish_thinking()
            call_id = data.get("call_id", new_id())
            name = data.get("name", "tool")
            args = data.get("arguments", {})
            self.chat_view.start_tool(call_id, name, args)
        elif kind == "tool_end":
            call_id = data.get("call_id", "")
            status = data.get("status", "success")
            result = data.get("result", "")
            duration = data.get("duration")
            preview = data.get("preview")
            artifact_path = data.get("artifact_path")
            self.chat_view.complete_tool(call_id, status, result, duration, preview, artifact_path)
        elif kind == "turn_end":
            self.chat_view.finish_thinking()
            self.chat_view.finish_assistant_message()
        elif kind == "approval_requested":
            self._handle_approval_requested(data)
        elif kind == "approval_resolved":
            if self._active_approval_modal and self._active_approval_modal.request_id == data.get("request_id"):
                modal = self._active_approval_modal
                self._active_approval_modal = None
                modal.dismiss(data.get("approved", False))
        elif kind == "attempt_reset":
            self.chat_view.finish_thinking()
            self.chat_view.finish_assistant_message()
            self.chat_view.add_notice("请求重试", "上次临时输出未完成，正在重新尝试。", kind="warning")
        elif kind == "compaction_start":
            self.chat_view.add_notice("上下文压缩", "已达到阈值，正在提炼历史记忆...", kind="compaction")
        elif kind == "compaction_end":
            success = data.get("success", False)
            self.chat_view.add_notice("上下文压缩", "检查点已保存" if success else "未发布新摘要，保留原有上下文", kind="compaction")
        elif kind == "service_error":
            self.chat_view.add_notice("运行异常", data.get("message", "未知错误"), kind="error")

        self.status_bar.update_status(self.ui_state)

    def _handle_approval_requested(self, data: dict[str, Any]) -> None:
        """处理危险工具调用的权限审批请求。

        【架构定位】
        弹出模态审批对话框，暂停当前流程，等待用户授权或拒绝。

        【开源设计思想与架构血统】
        对齐 DeepSeek-Harness 权限与预算安全门禁（Budget & Permission Guard）。
        对于写文件、执行 Shell 脚本等越权敏感操作实行“默认拒绝（Fail-Closed）”，
        以高对比度警告弹窗展现待执行工具名称、入参和申请权限，并将审批结果异步回传后端服务。

        Args:
            data: 审批请求载荷，包含 `request_id`, `name`, `arguments`, `permission` 等。
        """
        req_id = data.get("request_id", "")
        name = data.get("name", "tool")
        arguments = data.get("arguments", {})
        permission = data.get("permission", "write")

        modal = ApprovalModal(
            req_id,
            name,
            arguments,
            permission,
            no_color=self.ui_state.no_color,
        )
        self._active_approval_modal = modal

        def _on_resolved(approved: bool | None) -> None:
            """审批弹窗关闭后的异步回调。

            【架构定位】
            清理当前激活的模态框句柄，并将决议结果（同意/拒绝）异步通知 `WeyraAppService`。

            Args:
                approved: 用户是否批准本次执行，`None` 或 `False` 均视为拒绝。
            """
            self._active_approval_modal = None
            asyncio.create_task(self.service.resolve_approval(req_id, bool(approved)))

        self.push_screen(modal, _on_resolved)

    async def on_prompt_editor_submitted(self, event: PromptEditor.Submitted) -> None:
        """响应文本编辑器提交事件（Enter 发送）。

        【架构定位】
        用户交互主入口：接收编辑器文本，区分斜杠系统命令（`/`）与业务对话 Prompt，分流执行。

        【开源设计思想与架构血统】
        深度践行 Pi Agent 输入状态机管理设计：
        1. 空输入守卫：空行直接解锁编辑器，不提交无意义请求；
        2. 斜杠指令路由：前缀以 `/` 开头的命令优先本地消费或委托至后端命令执行器；
        3. 乐观上屏与防丢失保护：普通提问成功接收（`receipt.accepted`）后立即在 ChatView 中
           渲染用户卡片，并通知 EditorArea 释放暂存草稿；若服务拒绝或抛出异常，草稿将被完好保留在输入框中。

        Args:
            event: 编辑器提交事件对象，携带用户输入的完整多行文本。
        """
        text = event.text
        if not text.strip():
            self.editor_area.set_submitting(False)
            return

        # Handle slash commands locally or route to service
        if text.startswith("/"):
            await self._handle_slash_command(text)
            return

        # Regular prompt submission
        try:
            req_id = f"sub_{new_id()}"
            receipt = await self.service.submit(text, client_request_id=req_id)
            if not receipt.accepted:
                self.chat_view.add_notice("提交失败", receipt.message, kind="warning")
                self.editor_area.on_submission_finished(success=False, is_command=False, submitted_text=text)
            else:
                self.chat_view.add_user_message(text)
                self.editor_area.on_submission_finished(success=True, is_command=False, submitted_text=text)
        except Exception as e:
            self.chat_view.add_notice("提交错误", f"输入已保留，请重试（{type(e).__name__}）", kind="error")
            self.editor_area.on_submission_finished(success=False, is_command=False, submitted_text=text)

    async def _handle_slash_command(self, line: str) -> bool:
        """解析并调度斜杠系统命令。

        【架构定位】
        负责本地快捷指令（`/help`, `/skills`, `/models`, `/sessions`, `/exit`）与
        后台领域命令（`/branch`, `/new`, `/clear`, `/skill-rollback` 等）的分流调度。

        【开源设计思想与架构血统】
        参考 Pi Agent 与 DeepSeek-Harness 的 CLI/REPL 命令协议：
        - 本地模态界面唤起：直接弹出会话选择器、模型切换器或帮助窗；
        - 会话重置/分支命令：命令执行成功后触发 `chat_view.reset()` 并拉取新会话卡片；
        - 耗时与 Token 统计：对于返回用量指标的数据，提取并格式化展示 Calls、Tokens 及折合人民币费用。

        Args:
            line: 用户输入的完整命令行字符串。

        Returns:
            bool: 命令是否成功调度并执行。
        """
        cmd = line.split()[0].lower()
        if cmd == "/help":
            self.action_open_help(submitted_cmd=line)
        elif cmd == "/skills":
            await self.action_open_skills(submitted_cmd=line)
        elif cmd in {"/models", "/routes"}:
            self.action_open_models(submitted_cmd=line)
        elif cmd in {"/session", "/sessions"}:
            await self.action_open_sessions(submitted_cmd=line)
        elif cmd in {"/exit", "/quit"}:
            await self.action_exit_app()
        else:
            # Delegate to service
            req_id = f"cmd_{new_id()}"
            res = await self.service.execute_command(line, client_request_id=req_id)
            if res.ok:
                self.ui_state.apply_snapshot(await self.service.get_snapshot())
                self.status_bar.update_status(self.ui_state)
                msg = res.message
                if isinstance(res.data, dict) and "tokens" in res.data:
                    d = res.data
                    cost = "未知" if d.get("cost_cny") is None else f"¥{d['cost_cny']}"
                    msg = f"Calls: {d.get('calls')} | Tokens: {d.get('tokens')} | 费用: {cost}"
                elif res.data is not None:
                    content = res.data if isinstance(res.data, str) else json.dumps(res.data, ensure_ascii=False, indent=2, default=str)
                    msg = truncate_preview(content)[0]
                self.chat_view.add_notice(f"命令执行: {cmd}", msg, kind="info")
                if cmd in {"/branch", "/new", "/clear"}:
                    self.ui_state.apply_snapshot(await self.service.get_snapshot())
                    await self.chat_view.reset()
                    history = await self.service.get_history()
                    self.chat_view.load_history_cards(history.cards)
                self.editor_area.on_submission_finished(success=True, is_command=True, submitted_text=line)
            else:
                self.chat_view.add_notice(f"命令失败: {cmd}", res.message, kind="error")
                self.editor_area.on_submission_finished(success=False, is_command=True, submitted_text=line)
                return False
        return True

    def action_open_help(self, *, submitted_cmd: str | None = None) -> None:
        """打开快捷键与斜杠命令帮助模态窗口。

        【架构定位】
        响应 F1 快捷键或 `/help` 指令，展示完整的操作键位说明和内置系统命令表。

        Args:
            submitted_cmd: 若通过输入框指令触发，在模态框关闭后用于清理草稿状态的原始指令字符串。
        """
        def _on_closed(_res: None = None) -> None:
            """帮助弹窗关闭后的清理回调。

            Args:
                _res: 弹窗返回值（固定为 None）。
            """
            if submitted_cmd:
                self.editor_area.on_submission_finished(success=True, is_command=True, submitted_text=submitted_cmd)

        self.push_screen(HelpModal(no_color=self.ui_state.no_color), _on_closed)

    def action_open_models(self, *, submitted_cmd: str | None = None) -> None:
        """打开模型路由选择模态窗口。

        【架构定位】
        响应 F4 快捷键或 `/models` 指令，展示当前已配置的模型路由策略与权重分布。

        Args:
            submitted_cmd: 触发本操作的原始指令行。
        """
        def _on_closed(_res: None = None) -> None:
            """模型选择弹窗关闭后的清理回调。

            Args:
                _res: 弹窗返回值。
            """
            if submitted_cmd:
                self.editor_area.on_submission_finished(success=True, is_command=True, submitted_text=submitted_cmd)

        self.push_screen(ModelPickerModal(self.ui_state.routes, no_color=self.ui_state.no_color), _on_closed)

    async def action_open_sessions(self, *, submitted_cmd: str | None = None) -> None:
        """打开历史会话选择与管理模态窗口。

        【架构定位】
        响应 Ctrl+P / F2 快捷键或 `/sessions` 指令，列出所有持久化历史会话并支持分支切换或新建。

        【开源设计思想与架构血统】
        深度践行 Pi Agent Session Tree DAG 模型：
        会话切换本质上是切换会话树上活跃的叶子节点游标（`leaf` cursor movement），
        底层历史不发生破坏性变更。选择新会话后清空并重新装载目标分支的历史卡片流。

        Args:
            submitted_cmd: 触发本操作的原始命令行字符串。
        """
        sessions = await self.service.list_sessions()
        modal = SessionPickerModal(sessions, self.ui_state.session_id, no_color=self.ui_state.no_color)

        async def _on_session_picked(target_id: str | None) -> None:
            """会话选择完成后的异步回调：执行切换或新建会话。

            Args:
                target_id: 选中的会话 ID，`__new__` 表示新建会话，`None` 表示取消。
            """
            if submitted_cmd:
                self.editor_area.on_submission_finished(success=True, is_command=True, submitted_text=submitted_cmd)
            if target_id is None or target_id == self.ui_state.session_id:
                return
            try:
                snapshot = await self.service.new_session() if target_id == "__new__" else await self.service.open_session(target_id)
                # Reload history
                self.ui_state.apply_snapshot(snapshot)
                self.status_bar.update_status(self.ui_state)
                # Clear and reload chat view
                await self.chat_view.reset()
                history = await self.service.get_history()
                if history.cards:
                    self.chat_view.load_history_cards(history.cards)
                else:
                    self.chat_view.show_welcome(self.ui_state.project_name)
            except (RuntimeError, ValueError, OSError):
                self.chat_view.add_notice("切换会话失败", "请等待活动任务结束后重试，并检查会话是否存在。", kind="warning")

        self.push_screen(modal, _on_session_picked)

    async def action_open_skills(self, *, submitted_cmd: str | None = None) -> None:
        """打开技能查看与版本回滚模态窗口。

        【架构定位】
        响应 F3 / Ctrl+K 快捷键或 `/skills` 指令，展示内置技能与自演化技能目录，并支持版本回滚。

        【开源设计思想与架构血统】
        对接自学习与在线演化技能体系（`SkillEvolution`），通过调用 `/skill-rollback <name> <ver>`
        实现针对已演化技能安全无损的版本撤销与历史溯源。

        Args:
            submitted_cmd: 触发本操作的原始命令行字符串。
        """
        res = await self.service.execute_command("/skills", client_request_id=f"cmd_{new_id()}")
        skills_data = res.data if isinstance(res.data, list) else []
        modal = SkillViewerModal(skills_data, no_color=self.ui_state.no_color)

        async def _on_rollback_picked(rollback_target: tuple[str, str] | None) -> None:
            """技能版本选择完成后的异步回调：发起回滚请求。

            Args:
                rollback_target: 待回滚的 `(skill_name, version)` 二元组，`None` 表示放弃。
            """
            if submitted_cmd:
                self.editor_area.on_submission_finished(success=True, is_command=True, submitted_text=submitted_cmd)
            if rollback_target is None:
                return
            name, ver = rollback_target
            rb_res = await self.service.execute_command(
                shlex.join(["/skill-rollback", name, ver]), client_request_id=f"cmd_{new_id()}"
            )
            if rb_res.ok:
                result = rb_res.data if isinstance(rb_res.data, dict) else {}
                self.chat_view.add_notice("技能回滚", f"已恢复 {name} 的 {ver} 正文，新版本：{result.get('version', '请查看历史')}")
            else:
                self.chat_view.add_notice("技能回滚失败", rb_res.message, kind="error")

        self.push_screen(modal, _on_rollback_picked)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """处理主界面的通用按钮点击事件。

        【架构定位】
        分发“返回最新消息”以及工具卡片上的“查看完整输出（View Output）”操作。

        【开源设计思想与架构血统】
        对接 DeepSeek-Harness 的中间修剪（Middle Pruning）与制品外置机制：
        当工具输出被中间裁剪或写入外部 Artifact 时，通过挂载 `OutputViewerModal` 并传入
        `page_loader`，实现以 200 行/页分页即时读取磁盘日志，避免千行日志爆内存卡死终端。

        Args:
            event: 按钮点击事件对象。
        """
        if event.button.id == "jump-latest":
            self.chat_view.jump_to_bottom()
            self.jump_button.display = False
        elif event.button.has_class("view-output"):
            tool_card = next((c for c in self.chat_view.tool_cards.values()
                              if c.output_button is event.button), None)
            if tool_card and (tool_card.full_content or tool_card.artifact_path):
                modal = OutputViewerModal(
                    f"{tool_card.tool_name} 完整输出",
                    tool_card.full_content or "",
                    artifact_id=tool_card.call_id,
                    page_loader=(lambda offset: self.service.read_tool_output(
                        Path(tool_card.artifact_path).name, offset=offset
                    )) if tool_card.artifact_path else None,
                    no_color=self.ui_state.no_color,
                )
                self.push_screen(modal)

    def on_chat_view_pin_changed(self, event: ChatView.PinChanged) -> None:
        """响应消息流视图滚动钉住状态改变事件。

        【架构定位】
        当用户向上翻阅历史消息解除底部自动钉住时，显示“↓ 返回最新”浮动按钮；重回底部时自动隐藏。

        Args:
            event: 钉住状态变更事件，`event.pinned` 为 True 表示当前正贴合底部。
        """
        self.jump_button.display = not event.pinned

    async def action_handle_ctrl_c(self) -> None:
        """处理 Ctrl+C 组合键的三段式优雅响应。

        【架构定位】
        终端用户最高优先级的防误触与打断中枢。

        【开源设计思想与架构血统】
        严谨遵循 Pi Agent 交互式终端的标准三段式打断逻辑（Pi Three-Phase Interrupt Model）：
        1. 运行态保护（Task Cancellation）：若当前正有模型推理或工具执行活动（`activity != idle`），
           发送取消任务信号（`cancel_turn`），立即优雅中止后端推理，绝不强制杀掉 Python 进程；
        2. 草稿清理（Draft Discard）：若系统处于空闲态且编辑器中有未发送的草稿文本，清空草稿文本并在底栏给出提示；
        3. 优雅退出（Graceful Exit）：若系统空闲且编辑器草稿已为空，执行完整的连接释放与终端退出流程。
        """
        is_running = self.ui_state.activity not in {"idle", "closing", "closed"}
        if is_running:
            self.footer_bar.set_hint("正在取消当前任务...")
            await self.service.cancel_turn()
            return

        text = self.editor_area.get_text()
        if text.strip():
            self.editor_area.clear()
            self.footer_bar.set_hint("已清空输入草稿")
            self.set_timer(2.0, lambda: self.footer_bar.set_hint(None))
            return

        # Empty draft & idle -> exit
        await self.action_exit_app()

    async def action_exit_app(self) -> None:
        """安全释放资源并退出 TUI 应用。

        【架构定位】
        负责取消事件轮询后台任务、注销事件总线订阅并关闭后端核心服务，最后退出 Textual 运行时。
        """
        if self._event_task:
            self._event_task.cancel()
        if self._subscription:
            self.service.unsubscribe(self._subscription.id)
        await self.service.close()
        self.exit()

    async def on_unmount(self) -> None:
        """界面卸载时的全局资源终结钩子。

        【架构定位】
        确保在任何异常中断或外力退出场景下，后台监听 Task 被彻底取消、总线订阅安全解绑、
        后端文件句柄与线程池被严格释放，避免发生资源泄漏或后台孤儿子进程。
        """
        if self._event_task:
            self._event_task.cancel()
            await asyncio.gather(self._event_task, return_exceptions=True)
        if self._subscription:
            self.service.unsubscribe(self._subscription.id)
        await self.service.close()

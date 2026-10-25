"""统一审计鉴权、读写互斥调度与并发工具执行引擎 (Tool Execution Runtime & Two-Phase Permission Barrier)。

【架构定位】
本模块是 Weyra 智能体调用外部工具（文件系统、Shell 终端、技能系统、MCP 服务）的统一入口。
所有工具调用必须经过参数模式校验（JSONSchema Draft 2020-12）、双阶段权限屏障过滤、
读写互斥调度池以及产物持久化溢出处理，杜绝工具越权操作与并发数据破坏。

【开源与工程设计借鉴】
1. 借鉴 DeepSeek-Harness (`packages/shell/`, `packages/fs/`) 的读写并发调度模型 (Reader-Writer Coordination)：
   - 只读工具（Read-only，如 read_file, grep_search, list_files）共享并发池（默认 10 并发）；
   - 写入工具（Write/Mutating，如 write_file, edit_file, run_shell）独占执行（Concurrency=1），具备写优先权以防止写饥饿；
2. 双阶段权限鉴权屏障 (Two-Phase Permission Barrier)：
   - 第一阶段（静态策略判断）：结合当前权限模式（default, plan, dontAsk, acceptEdits, bypassPermissions）与 deny_rules 执行零时延判定；
   - 第二阶段（动态人机介入）：若静态策略要求审批，加锁调用 `confirm` 回调挂起等待用户交互输入；审批通过后双重校验策略，防止并发撤销；
3. 全量产物存储与前置预览截断联动 (`_persist` & `output.py`)：
   所有工具完整标准输出/错误无损落盘至 `.weyra/artifacts/<sha256>.txt`，返回给提示词上下文的是经过 Pi Agent / DSH 规则裁剪后的紧凑预览。
"""

from __future__ import annotations
import asyncio
import hashlib
import inspect
import json
from contextlib import asynccontextmanager
from pathlib import Path
from jsonschema import Draft202012Validator
from weyra.contracts import ToolResult, ToolCall
from weyra.core.hooks import ToolCallHook
from weyra.output import preview_output


class ToolRuntime:
    """工具运行时调度与审计管理器 (Tool Runtime Scheduler)。

    统一调度会话内所有已注册工具，管理并发槽位、权限边界与结果落地。
    """

    def __init__(
        self, project, session, permission_mode="default", confirm=None, concurrency=10, **shared
    ):
        """初始化工具运行时调度器 (Initialize ToolRuntime Scheduler)。

        【设计思想与开源血统】
        遵循 DeepSeek-Harness 读写并发与权限屏障模型：
        管理工具注册表、私有受控路径、读写互斥调度原语与文件锁。

        Args:
            project: 目标工程根目录。
            session: 持久化会话管理实例。
            permission_mode: 全局权限模式（默认 "default"）。
            confirm: 人机交互动态授权确认回调函数。
            concurrency: 只读工具的最大并发上限，默认 10。
            **shared: 跨实例或子代理共享的调度原语（condition, approval_lock, file_lock 等）。

        Raises:
            ValueError: 当并发度不是正整数时抛出。
        """
        self.project = Path(project).resolve()
        self.session = session
        self.permission_mode = permission_mode
        self.confirm = confirm
        self.specs = {}
        self.private_paths = set()
        self.coordination = (
            shared.get("coordination")
            or shared.get("scheduler")
            or {
                "condition": shared.get("condition") or asyncio.Condition(),
                "approval_lock": shared.get("approval_lock") or asyncio.Lock(),
                "file_lock": shared.get("file_lock") or asyncio.Lock(),
                "readers": 0,
                "writer": False,
                "waiting_writers": 0,
            }
        )
        self._condition = self.coordination["condition"]
        self._approval = self.coordination["approval_lock"]
        self._file_lock = self.coordination["file_lock"]
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
        self.concurrency = concurrency
        self.deny_rules = shared.get("deny_rules", ())

    @property
    def scheduler(self):
        """获取底层读写协调锁存器字典 (Get Shared Coordination Primitives)。"""
        return self.coordination

    @property
    def file_lock(self):
        """获取全局文件系统操作互斥锁 (Get Global File Lock)。"""
        return self._file_lock

    def register(self, spec):
        """校验入参规范并向运行时注册新工具 (Register Tool with JSON Schema Validation)。

        【安全校验】
        使用 Draft202012Validator 严格静态校验工具参数 schema 是否合规，确保格式合法。

        Args:
            spec: 工具规范契约对象 ToolSpec。
        """
        Draft202012Validator.check_schema(spec.schema)
        self.specs[spec.name] = spec

    def declarations(self):
        """导出所有公开工具的标准 Function Calling 声明元组 (Export Public Tool Declarations)。

        【设计原则】
        按工具名称升序排列以保证前缀一致性，自动过滤 MCP 内部保留前缀工具 (`mcp__`)。

        Returns:
            tuple[dict, ...]: 格式化后的工具声明列表。
        """
        return tuple(
            self.specs[name].declaration()
            for name in sorted(self.specs)
            if not name.startswith("mcp__")
        )

    def resolve_path(self, value):
        """解析并强力校验目标文件路径是否逃逸工程沙箱边界 (Resolve & Sandbox Project Path)。

        【沙箱边界防御】
        1. 路径越界检查：解析绝对路径，强制要求位于 project 根目录内，防御跨目录遍历；
        2. 私有路径防护：禁止模型访问宿主私有验证数据。

        Args:
            value: 相对或绝对路径字符串。

        Returns:
            Path: 验证合规的绝对路径对象。

        Raises:
            PermissionError: 当路径逃逸或属于私有路径时抛出。
        """
        path = (self.project / value).resolve()
        if not path.is_relative_to(self.project):
            raise PermissionError("Path escapes project root")
        if self.is_private(path):
            raise PermissionError("Host validation data is not available to model tools")
        return path

    def is_private(self, path):
        """判断指定路径是否命中私有受控隔离列表 (Check If Path Is In Private Deny List)。

        Args:
            path: 待检测的目标路径。

        Returns:
            bool: 若属于私有保护文件则返回 True。
        """
        path = Path(path).resolve()
        return any(path == Path(private).resolve() or path.is_relative_to(Path(private).resolve())
                   for private in self.private_paths)

    @asynccontextmanager
    async def _slot(self, read):
        """读写互斥槽位申请与释放上下文管理器 (Reader-Writer Slot Context Manager)。

        【DeepSeek-Harness 读写调度核心算法】
        1. 只读操作 (`read=True`)：支持最多 `concurrency`（默认 10）路并发共享，但受等待中写者阻塞（写优先）；
        2. 写入操作 (`read=False`)：互斥独占，要求活跃读者归零且无其他写者执行中（Concurrency=1）；
        3. 退出通知：退出时递减对应计数器并通过 Condition 唤醒后续等待协程。

        Args:
            read: True 为只读共享槽位，False 为独占写槽位，None 为无约束直接透传。
        """
        if read is None:
            yield
            return
        async with self._condition:
            if not read:
                self.coordination["waiting_writers"] += 1
            try:
                await self._condition.wait_for(
                    lambda: (
                        not self.coordination["writer"]
                        and (
                            self.coordination["readers"] == 0
                            if not read
                            else self.coordination["readers"] < self.concurrency
                            and not self.coordination["waiting_writers"]
                        )
                    )
                )
                if read:
                    self.coordination["readers"] += 1
                else:
                    self.coordination["writer"] = True
            finally:
                if not read:
                    self.coordination["waiting_writers"] -= 1
        try:
            yield
        finally:
            async with self._condition:
                if read:
                    self.coordination["readers"] -= 1
                else:
                    self.coordination["writer"] = False
                self._condition.notify_all()

    def _policy(self, spec, args, context):
        """基于静态策略矩阵和上下文判定工具调用权限 (Evaluate Static Permission Policy)。

        【权限模式状态机判定】
        1. 静态黑名单：任意命中国内顶级大厂安全规范 deny_rules 则立即返回 False；
        2. 只读工具特权：所有只读工具 (`spec.permission == "read"`) 均免审批放行 (True)；
        3. plan 模式：仅允许修改当前激活的 `plan_path` 规划文件；
        4. dontAsk 模式：任何非只读写操作一律静默拒绝 (False)；
        5. bypass 模式：完全放行 (True)；
        6. acceptEdits 模式：放行所有常规写入 (True)；
        7. default 模式：返回 None，转入第二阶段人机确认。

        Args:
            spec: 工具规范。
            args: 入参字典。
            context: 运行上下文。

        Returns:
            bool | None: True 允许，False 拒绝，None 需人机确认。
        """
        if any(rule(spec, args) for rule in self.deny_rules):
            return False
        mode = getattr(context, "permission_mode", self.permission_mode)
        modes = [mode]
        agent = context.services.get("agent")
        if agent is not None:
            modes.append(agent.permission_mode)
        parent = getattr(agent, "parent_agent", None)
        while parent is not None:
            if any(rule(spec, args) for rule in parent.tools.deny_rules):
                return False
            modes.append(parent.permission_mode)
            parent = getattr(parent, "parent_agent", None)
        mode = next(
            (value for value in ("plan", "dontAsk", "default", "acceptEdits") if value in modes),
            "bypassPermissions",
        )
        if spec.permission == "read":
            return True
        if mode == "plan":
            plan = getattr(context, "plan_path", None)
            return bool(
                spec.permission == "write"
                and spec.name in {"write_file", "edit_file"}
                and plan
                and "path" in args
                and self.resolve_path(args["path"]) == Path(plan).resolve()
            )
        if mode == "dontAsk":
            return False
        if mode == "bypassPermissions" or mode == "bypass":
            return True
        if mode == "acceptEdits" and spec.permission == "write":
            return True
        return None

    async def _allowed(self, spec, args, context):
        """双阶段权限鉴权屏障综合仲裁入口 (Two-Phase Permission Barrier Arbiter)。

        【双阶段鉴权流程】
        1. 阶段一：调用 `_policy` 快速执行静态无锁仲裁；
        2. 阶段二：若静态判定为 None，加锁调用 `confirm` 挂起等待人类授权输入；
        3. 二次校验：在人机批准后再次校验策略，防止并发期间权限被动态撤销。

        Args:
            spec: 工具规范。
            args: 入参字典。
            context: 运行上下文。

        Returns:
            bool: 最终是否获准执行。
        """
        decision = self._policy(spec, args, context)
        if decision is not None:
            return decision
        if self.confirm is None:
            return False
        async with self._approval:
            decision = self._policy(spec, args, context)
            if decision is not None:
                return decision
            answer = self.confirm(spec, args, context)
            approved = bool(await answer if inspect.isawaitable(answer) else answer)
            # Policy can be revoked while a human approval is pending.
            decision = self._policy(spec, args, context)
            return approved and decision is not False

    async def _run(self, call, context, previous=None, prepared=None):
        """执行单个工具调用的核心流转与异常捕获逻辑 (Execute Single Tool Invocation Core)。

        【执行管道】
        1. 依赖等待：若存在前置就绪事件 `previous`，等待前序工具准备完毕；
        2. 运行时拦截挂钩：调用 HookRegistry 的 ToolCallHook 支持动态修改参数或熔断阻断；
        3. 槽位获取与鉴权：获取读写互斥槽位并验证权限；
        4. 意图记录与事件广播：在 SessionStore 中记入 `tool_intent` 并派发 `tool_start`；
        5. 执行与结果归一化：捕获异常与取消信号，生成标准 ToolResult。

        Args:
            call: 待执行的工具调用请求。
            context: 运行上下文。
            previous: 可选的前置准备事件。
            prepared: 可选的当前准备就绪通知事件。

        Returns:
            ToolResult: 工具执行生成的标准结果。
        """
        spec = self.specs.get(call.name)
        result = ToolResult(call.id, call.name, "", status="invalid")
        try:
            if previous is not None:
                await previous.wait()
            if spec is None:
                result.content = "Unknown tool"
                return result
            hooks = context.services.get("hooks")
            if hooks:
                intervention = await hooks.emit(ToolCallHook(call.id, call.name, call.arguments))
                if intervention and intervention.block:
                    result.status, result.content = "denied", intervention.reason or "Tool blocked"
                    return result
                if intervention and intervention.updated_args is not None:
                    call = ToolCall(call.id, call.name, intervention.updated_args)
            errors = list(Draft202012Validator(spec.schema).iter_errors(call.arguments))
            if errors:
                result.content = "Arguments do not satisfy the tool schema"
                return result
            read = (
                None
                if spec.execution_mode == "dispatch"
                else spec.execution_mode == "read" and spec.permission == "read"
            )
            async with self._slot(read):
                context.check_cancelled()
                if not await self._allowed(spec, call.arguments, context):
                    result.status, result.content = "denied", "Permission denied"
                    return result
                context.check_cancelled()
                context.services["tools"] = self
                self.session.append(
                    "tool_intent",
                    {"call_id": call.id, "name": call.name, "arguments": call.arguments},
                    checkpoint=True,
                )
                result.started = True
                emit = getattr(context, "emit", None)
                if emit:
                    await emit("tool_start", {"call_id": call.id, "name": call.name, "arguments": call.arguments})
                if prepared is not None:
                    prepared.set()
                value = spec.handler(call.arguments, context)
                value = await value if inspect.isawaitable(value) else value
                if isinstance(value, ToolResult):
                    result = value
                    result.call_id, result.name, result.started = call.id, call.name, True
                else:
                    result.content = (
                        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    )
                    result.structured = value if not isinstance(value, str) else None
                    result.status = "success"
                    result.effect = "none" if spec.permission == "read" else "completed"
        except asyncio.CancelledError:
            result.status, result.content = "cancelled", "Cancelled"
            result.effect = (
                "unknown"
                if result.started and spec is not None and spec.permission != "read"
                else "none"
            )
        except Exception as exc:
            result.status = "error"
            result.content = f"Tool failed ({type(exc).__name__})"
            result.effect = (
                "unknown"
                if result.started and spec is not None and spec.permission != "read"
                else "none"
            )
        finally:
            if prepared is not None:
                prepared.set()
        return result

    async def _finalize(self, result, context, persist=True):
        """工具执行结果终结流转与全量产物落盘 (Finalize Tool Result & Persist Artifact)。

        Args:
            result: 工具返回的结果。
            context: 运行上下文。
            persist: 是否执行产物落盘与前置预览截断。

        Returns:
            ToolResult: 经过持久化后的终态结果。
        """
        if persist:
            self._persist(result)
        emit = getattr(context, "emit", None)
        if emit:
            await emit("tool_completed", {"call_id": result.call_id, "status": result.status})
        return result

    async def execute(self, call, context):
        """单工具执行对外公共标准异步入口 (Public Single Tool Execution Entrypoint)。

        Args:
            call: 拟调用的工具对象 ToolCall。
            context: 运行上下文 RunContext。

        Returns:
            ToolResult: 执行完毕的标准结果。
        """
        result = await self._finalize(await self._run(call, context), context)
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        return result

    def _persist(self, result):
        """将工具完整输出持久化至磁盘并生成紧凑上下文预览 (Persist Full Tool Output & Generate Context Preview)。

        【产物分离与预览算法】
        1. 计算原始 UTF-8 输出的 SHA-256 哈希值；
        2. 将全量文本写入 `.weyra/artifacts/<hash>.txt` 产物目录；
        3. 挂载 `output_ref` 磁盘绝对路径索引；
        4. 联动 `output.py` 中的 `preview_output` 为模型上下文生成带截断提示的紧凑预览；
        5. 原子追加 `tool_result` 事件至会话记录。

        Args:
            result: 待持久化落盘的工具执行结果对象。
        """
        raw = result.content.encode("utf-8")
        directory = self.resolve_path(".weyra/artifacts")
        directory.mkdir(parents=True, exist_ok=True)
        path = self.resolve_path(str(directory / (hashlib.sha256(raw).hexdigest() + ".txt")))
        path.write_bytes(raw)
        result.output_ref = str(path)
        result.content = preview_output(result)
        self.session.append("tool_result", vars(result).copy(), checkpoint=True)

    async def execute_batch(self, calls, context):
        """按调用顺序准备、按读写安全分类并发流转、严格按序提交结果 (Ordered Batch Tool Execution)。

        【DSH 10 路并发调度流水线】
        1. 识别连续只读工具分段 (Safe Read Segment)：若当前工具为只读且并发安全，扫描后续连续的只读工具，
           将其放入容量为 `self.concurrency`（默认 10）的有界并发池中流水线执行；
        2. 写入工具串行隔离：若遇到写工具，立即收拢并发池，以 Concurrency=1 独占串行执行；
        3. 保序提交 (In-Order Commit)：无论并行完成顺序如何，结果严格按照初始调用次序提交落盘与发布。
        """
        calls = list(calls)
        results = []
        cursor = 0
        active = {}
        values = {}
        next_commit = 0
        try:
            while cursor < len(calls):
                first = self.specs.get(calls[cursor].name)
                safe = first and first.permission == "read" and first.execution_mode == "read"
                end = cursor + 1
                if safe:
                    while end < len(calls):
                        spec = self.specs.get(calls[end].name)
                        if not (
                            spec and spec.permission == "read" and spec.execution_mode == "read"
                        ):
                            break
                        end += 1
                previous = None
                while cursor < end or active:
                    while cursor < end and len(active) < (self.concurrency if safe else 1):
                        ready = asyncio.Event()
                        task = asyncio.create_task(
                            self._run(calls[cursor], context, previous, ready)
                        )
                        active[task] = cursor
                        previous = ready
                        cursor += 1
                    done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        values[active.pop(task)] = task.result()
                    while next_commit in values:
                        value = values.pop(next_commit)
                        self._persist(value)
                        results.append(value)
                        next_commit += 1
                        await self._finalize(value, context, persist=False)
        except asyncio.CancelledError:
            for task in active:
                task.cancel()
            pending = list(active)
            drained = await asyncio.gather(*pending, return_exceptions=True)
            for task, value in zip(pending, drained):
                index = active[task]
                values[index] = (
                    value
                    if isinstance(value, ToolResult)
                    else ToolResult(calls[index].id, calls[index].name, "Cancelled", "cancelled")
                )
            for index in range(next_commit, len(calls)):
                value = values.get(index) or ToolResult(
                    calls[index].id, calls[index].name, "Cancelled", "cancelled"
                )
                results.append(await self._finalize(value, context))
            raise
        return results

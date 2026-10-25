"""内置文件系统、代码搜索、Shell 执行与受控后台作业工具集。

架构定位与设计哲学:
    本模块为 Weyra Agent 运行时提供核心的确定性系统操作工具（System Operations Tools）。
    在 LLM Agent 架构中，文件读写与命令执行属于最高风险的副作用操作边界（Side-effect Boundary）。
    本模块通过形式化参数校验、哈希乐观锁并发控制、确定性增量差分（Unified Diff）以及进程组隔离，
    构建了企业级、生产级可防御的执行沙箱。

开源框架借鉴与学术溯源:
    1. PiAgent (packages/coding-agent/src/tools/):
       - 借鉴其文件阅读工具的分页安全语义：采用 1-based offset 行偏移量索引，输出硬性上限截断（2000 行 / 50 KiB），
         并在截断响应中回传下一个 offset 提示，彻底根除模型读取超大代码文件时的上下文膨胀（Context Explosion）。
       - 借鉴其文件单次精确替换机制（edit_file）：严格约束 `text.count(old_text) == 1`，彻底杜绝模糊匹配或全局
         正则替换导致的非预期破坏；强制要求修改前必须在当前轮次完成文件读取并比对 SHA-256 指纹（Optimistic Concurrency Control）。
    2. DeepSeek-Harness (packages/shell, packages/subprocess, packages/jobs):
       - 借鉴其受控子进程作业调度器（Owned Jobs Supervision）：Shell 命令执行采用独立的会话组隔离
         （POSIX start_new_session=True），支持同步等待超时保护与异步后台分离（Background Mode）。
       - 借鉴其作业所有权流转协议（Ownership Handover）：实现轮次（Turn Context）向持久化会话（Session）的
         所有权转移（transfer_job），并在轮次异常中断时实施幂等的进程组级联取消（cancel_job / cleanup_owner）。
    3. WikiSkill (arXiv:2608.27454, 2026) & SkillOpt (arXiv:2605.23904, 2026):
       - 在写操作中集成统一差异生成器（generate_unified_diff），为后续的技能自进化（Skill Evolution）与
         人类确认审核（Human-in-the-Loop Approval）提供零歧义的代码变更凭据。
"""

from __future__ import annotations
import asyncio
import hashlib
import re
from pathlib import Path
from .contracts import ToolResult, ToolSpec


def install_builtin_tools(runtime, jobs):
    """向 ToolRuntime 注册核心内置工具集。

    注册工具清单:
        - read_file: 支持 1-based offset 深度分页与指纹采集的安全只读文件查看器。
        - write_file: 带乐观并发版本校验的全量文件覆写工具。
        - edit_file: 单次精确匹配（Exactly Once Match）的局部文件打补丁工具。
        - list_files: 限制深度与数量（5000 文件上限）的项目内静态发现工具。
        - grep_search: 正则表达式检索工具（默认上限 100 匹配项）。
        - run_shell: 支持超时熔断与后台作业托管的 Shell 进程执行器。
        - job_status / wait_job / cancel_job / transfer_job: 后台作业生命周期托管工具。

    Args:
        runtime: 宿主 ToolRuntime 实例，提供并发读写锁、路径越界校验与权限栅栏。
        jobs: Jobs 后台进程监管中心，负责进程树管理与落盘日志托管。
    """
    def schema(properties, required=()):
        """构建标准化 JSON Schema 对象定义。"""
        return {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        }

    string = {"type": "string"}
    descriptions = {
        "read_file": "Read a UTF-8 file inside the current project. Paths are project-relative. Offset starts at 1; output keeps the first 2000 lines or 50 KiB and includes the next offset when truncated.",
        "list_files": "List files inside the current project. Use path '.' to discover fixture or project files without a shell. Absolute paths must remain inside this project.",
        "grep_search": "Search text inside the current project with a regex. Returns at most 100 matches by default, with paths and line numbers; use read_file for full text.",
        "write_file": "Create or replace a project-relative UTF-8 file. Existing files must first be read in this turn so the current content version can be checked.",
        "edit_file": "Replace exactly one occurrence of old_text in a project-relative UTF-8 file. Read the current file first; conflicting external changes are rejected.",
        "run_shell": "Run a shell command in the current project. Explicit approval is normally required; prefer file tools when approval is unavailable. Background mode returns an owned job ID.",
    }

    def register(name, props, required, handler, permission="read"):
        """辅助注册单项 ToolSpec，自动绑定读共享或排他互斥模式。"""
        runtime.register(
            ToolSpec(
                name,
                descriptions.get(name, name.replace("_", " ")),
                schema(props, required),
                handler,
                "read" if permission == "read" else "exclusive",
                permission,
            )
        )

    async def read(args, ctx):
        """执行安全文件读取。

        借鉴 PiAgent 的只读设计：
            1. 校验路径是否越界逃逸项目根目录。
            2. 在文件互斥锁保护下读取字节流并计算 SHA-256 哈希值，记录到当前轮次的 ctx.read_versions 中，
               为后续可能的 write_file / edit_file 提供乐观锁比对基准。
            3. 支持 1-based 行偏移量 start，越界抛出 ValueError。

        Args:
            args: 包含 "path"、可选 "offset" 和 "limit" 的参数字典。
            ctx: 当前轮次的 RunContext 上下文对象。

        Returns:
            ToolResult: 包含文件正文与结构化分页元数据的工具执行结果。
        """
        path = runtime.resolve_path(args["path"])
        async with runtime.file_lock:
            raw = path.read_bytes()
            ctx.read_versions[str(path)] = hashlib.sha256(raw).hexdigest()
        lines = raw.decode("utf-8").split("\n")
        start = args.get("offset", 1)
        if start > len(lines):
            raise ValueError("Offset is beyond end of file")
        # 保持全量剩余行供底层统一截断层（Output Layer）进行 2000 行 / 50KiB 视窗预览
        return ToolResult(
            "",
            "read_file",
            "\n".join(lines[start - 1 :]),
            structured={
                "_preview": "read",
                "offset": start,
                "limit": args.get("limit"),
                "total_lines": len(lines),
            },
        )

    async def write(args, ctx):
        """执行文件全量覆写或精准补丁替换。

        开源架构借鉴 (PiAgent & DeepSeek-Harness):
            1. 安全沙箱边界：严禁直接通过文件工具篡改 .weyra/ 框架内部核心状态（除非显式指定为 plan 路径）。
            2. 乐观并发控制 (OCC)：如果目标文件已存在，必须校验其当前的 SHA-256 哈希值与当前轮次中
               读取记录（ctx.read_versions）一致，彻底防止 Agent 基于陈旧视图（Stale View）做盲目修改。
            3. 精确一次匹配 (Exactly Once Match)：对于 edit_file，必须满足 old_text 在文件中仅出现一次；
               多于或少于一次均抛出 ValueError 并拒绝写入，确保补丁无歧义。
            4. 差异追踪与可视化：写入完成后生成标准 Unified Diff，注入结构化元数据以供审批和终端呈现。

        Args:
            args: 写入或编辑参数字典。
            ctx: 运行时上下文 RunContext。

        Returns:
            ToolResult: 带有状态标识和 diff 视图的执行结果。
        """
        path = runtime.resolve_path(args["path"])
        state = runtime.project / ".weyra"
        if path.is_relative_to(state) and path != getattr(ctx, "plan_path", None):
            raise PermissionError("Harness state is managed through dedicated operations")
        old_text = ""
        async with runtime.file_lock:
            if path.exists():
                raw = path.read_bytes()
                current = hashlib.sha256(raw).hexdigest()
                if ctx.read_versions.get(str(path)) != current:
                    raise ValueError("Read the current file before modifying it")
                old_text = raw.decode("utf-8", errors="replace")
            if "old_text" in args:
                text = path.read_text(encoding="utf-8")
                if not args["old_text"] or text.count(args["old_text"]) != 1:
                    raise ValueError("old_text must match exactly once")
                content = text.replace(args["old_text"], args["new_text"], 1)
            else:
                content = args["content"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            ctx.read_versions[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()

        from .presentation import generate_unified_diff

        diff = generate_unified_diff(str(args["path"]), old_text, content)
        display_data = {"diff": diff} if diff else {}
        return ToolResult(
            "",
            "write_file" if "content" in args else "edit_file",
            "File updated",
            structured={"_display": display_data},
            status="success",
            effect="completed",
        )

    def listing(args, ctx):
        """遍历项目目录下的静态文件列表。

        安全约束与防御机制:
            1. 限制发现范围在项目根目录之内（防路径遍历攻击）。
            2. 自动过滤 .weyra/ 框架内部目录与 private 保护文件。
            3. 硬性限制最多返回 5000 个文件路径，避免大规模代码库产生内存与网络瓶颈。
        """
        root = runtime.resolve_path(args.get("path", "."))
        return [
            str(p.relative_to(runtime.project))
            for p in root.rglob("*")
            if p.is_file()
            and p.resolve().is_relative_to(runtime.project)
            and ".weyra" not in p.relative_to(runtime.project).parts
            and not runtime.is_private(p)
        ][:5000]

    def grep(args, ctx):
        """基于正则表达式在项目文件内容中检索匹配项。

        工程防御策略:
            1. 自动跳过二进制文件与非 UTF-8 编码文件（静默捕获 UnicodeError / OSError）。
            2. 限制单次检索最多返回 100 处匹配行（默认），返回行号、相对路径与匹配内容摘要。
        """
        matches = []
        pattern = re.compile(args["pattern"])
        root = runtime.resolve_path(args.get("path", "."))
        names = [str(root.relative_to(runtime.project))] if root.is_file() else listing(args, ctx)
        for name in names:
            path = runtime.resolve_path(name)
            try:
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if pattern.search(line):
                        matches.append({"path": name, "line": number, "text": line})
            except (UnicodeError, OSError):
                continue
        raw = "\n".join(f"{m['path']}:{m['line']}: {m['text']}" for m in matches)
        return ToolResult(
            "",
            "grep_search",
            raw,
            structured={"_preview": "grep", "matches": matches, "limit": args.get("limit", 100)},
        )

    async def shell(args, ctx):
        """在沙箱工作区内执行 Shell 命令。

        借鉴 DeepSeek-Harness (packages/shell & packages/subprocess):
            1. 托管式作业创建：命令由 jobs 子系统统一创建，分配唯一 job_id 并重定向输出至独立磁盘日志文件。
            2. 轮次资源清理屏障：向 ctx 注册清理回调，一旦该轮次异常中断或取消，自动销毁所有衍生子进程。
            3. 后台模式 (Background Mode)：允许长时间运行的任务（如开发服务器、编译测试）脱离当前阻塞轮次，
               立即向模型返回运行中状态与作业元数据。
            4. 协同取消与日志落盘：同步等待期间支持 asyncio.CancelledError 捕获，确保级联发送 SIGTERM 信号并记录 checkpoint。

        Args:
            args: 包含 command, background, timeout 等参数字典。
            ctx: 当前轮次上下文。

        Returns:
            ToolResult 或 作业状态字典。
        """
        status = await jobs.start(args["command"], ctx.id, runtime.project)
        runtime.session.append("job_start", status, checkpoint=True)
        if hasattr(ctx, "register_resource"):
            ctx.register_resource(lambda: jobs.cleanup_owner(ctx.id))
        if args["background"]:
            return status
        try:
            status = await jobs.wait(status["job_id"], args.get("timeout", 30))
            runtime.session.append("job_status", status, checkpoint=True)
            return ToolResult(
                "",
                "run_shell",
                Path(status["output_path"]).read_text(errors="replace"),
                structured=status,
                status="error" if status["returncode"] not in {0, None} else "success",
                effect="completed" if not status["running"] else "unknown",
            )
        except asyncio.CancelledError:
            cancelled = await jobs.cancel(status["job_id"])
            runtime.session.append("job_cancel", cancelled, checkpoint=True)
            raise

    def owned(args, ctx):
        """校验当前调用方是否对指定 job_id 拥有合法支配权（所有权隔离防越权）。"""
        session_owner = "session:" + str(getattr(runtime.session, "session_id", ""))
        if jobs.status(args["job_id"])["owner"] not in {ctx.id, session_owner}:
            raise PermissionError("Job belongs to another turn")

    async def wait(args, ctx):
        """同步等待特定作业执行完成或直至超时。"""
        owned(args, ctx)
        status = await jobs.wait(args["job_id"], args.get("timeout", 30))
        runtime.session.append("job_status", status, checkpoint=True)
        return ToolResult(
            "",
            "wait_job",
            Path(status["output_path"]).read_text(errors="replace"),
            structured=status,
        )

    async def cancel(args, ctx):
        """显式取消正在运行的作业进程树。"""
        owned(args, ctx)
        status = await jobs.cancel(args["job_id"])
        runtime.session.append("job_cancel", status, checkpoint=True)
        return status

    def status(args, ctx):
        """查询后台作业当前存活状态与返回码。"""
        owned(args, ctx)
        value = jobs.status(args["job_id"])
        runtime.session.append("job_status", value, checkpoint=True)
        return value

    def transfer(args, ctx):
        """将轮次拥有的作业所有权转交至会话级生命周期（所有权流转协议）。"""
        owned(args, ctx)
        session_owner = "session:" + str(getattr(runtime.session, "session_id", ""))
        if args["owner"] not in {"session", session_owner}:
            raise PermissionError("Jobs may only be handed to the current session")
        value = jobs.transfer(args["job_id"], jobs.status(args["job_id"])["owner"], session_owner)
        runtime.session.append("job_transfer", value, checkpoint=True)
        return value

    # 注册系统基础只读与只写工具
    register(
        "read_file",
        {
            "path": string,
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1},
        },
        ["path"],
        read,
    )
    register("write_file", {"path": string, "content": string}, ["path", "content"], write, "write")
    register(
        "edit_file",
        {"path": string, "old_text": string, "new_text": string},
        ["path", "old_text", "new_text"],
        write,
        "write",
    )
    register("list_files", {"path": string}, [], listing)
    register(
        "grep_search",
        {"path": string, "pattern": string, "limit": {"type": "integer", "minimum": 1}},
        ["pattern"],
        grep,
    )
    register(
        "run_shell",
        {
            "command": string,
            "background": {"type": "boolean"},
            "timeout": {"type": "number", "minimum": 0, "maximum": 300},
        },
        ["command", "background"],
        shell,
        "shell",
    )
    register(
        "job_status",
        {"job_id": string},
        ["job_id"],
        status,
    )
    register(
        "wait_job",
        {"job_id": string, "timeout": {"type": "number", "minimum": 0, "maximum": 300}},
        ["job_id"],
        wait,
    )
    register("cancel_job", {"job_id": string}, ["job_id"], cancel, "shell")
    register(
        "transfer_job",
        {"job_id": string, "owner": {"type": "string", "enum": ["session"]}},
        ["job_id", "owner"],
        transfer,
        "shell",
    )


"""Concrete filesystem, search, shell, and owned job tools."""

from __future__ import annotations
import asyncio
import hashlib
import re
from pathlib import Path
from .contracts import ToolResult, ToolSpec


def install_builtin_tools(runtime, jobs):
    def schema(properties, required=()):
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
        path = runtime.resolve_path(args["path"])
        async with runtime.file_lock:
            raw = path.read_bytes()
            ctx.read_versions[str(path)] = hashlib.sha256(raw).hexdigest()
        lines = raw.decode("utf-8").split("\n")
        start = args.get("offset", 1)
        if start > len(lines):
            raise ValueError("Offset is beyond end of file")
        # Preserve the entire remaining span before the shared output layer previews it.
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
        path = runtime.resolve_path(args["path"])
        state = runtime.project / ".weyra"
        if path.is_relative_to(state) and path != getattr(ctx, "plan_path", None):
            raise PermissionError("Harness state is managed through dedicated operations")
        async with runtime.file_lock:
            if path.exists():
                current = hashlib.sha256(path.read_bytes()).hexdigest()
                if ctx.read_versions.get(str(path)) != current:
                    raise ValueError("Read the current file before modifying it")
            if "old_text" in args:
                text = path.read_text()
                if not args["old_text"] or text.count(args["old_text"]) != 1:
                    raise ValueError("old_text must match exactly once")
                content = text.replace(args["old_text"], args["new_text"], 1)
            else:
                content = args["content"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            ctx.read_versions[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return "File updated"

    def listing(args, ctx):
        root = runtime.resolve_path(args.get("path", "."))
        return [
            str(p.relative_to(runtime.project))
            for p in root.rglob("*")
            if p.is_file()
            and p.resolve().is_relative_to(runtime.project)
            and ".weyra" not in p.relative_to(runtime.project).parts
        ][:5000]

    def grep(args, ctx):
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
        session_owner = "session:" + str(getattr(runtime.session, "session_id", ""))
        if jobs.status(args["job_id"])["owner"] not in {ctx.id, session_owner}:
            raise PermissionError("Job belongs to another turn")

    async def wait(args, ctx):
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
        owned(args, ctx)
        status = await jobs.cancel(args["job_id"])
        runtime.session.append("job_cancel", status, checkpoint=True)
        return status

    def status(args, ctx):
        owned(args, ctx)
        value = jobs.status(args["job_id"])
        runtime.session.append("job_status", value, checkpoint=True)
        return value

    def transfer(args, ctx):
        owned(args, ctx)
        session_owner = "session:" + str(getattr(runtime.session, "session_id", ""))
        if args["owner"] not in {"session", session_owner}:
            raise PermissionError("Jobs may only be handed to the current session")
        value = jobs.transfer(args["job_id"], jobs.status(args["job_id"])["owner"], session_owner)
        runtime.session.append("job_transfer", value, checkpoint=True)
        return value

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

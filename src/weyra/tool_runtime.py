"""One audited policy and scheduling path for every tool invocation."""

from __future__ import annotations
import asyncio
import hashlib
import inspect
import json
from contextlib import asynccontextmanager
from pathlib import Path
from jsonschema import Draft202012Validator
from .contracts import ToolResult, ToolSpec


class ToolRuntime:
    def __init__(
        self, project, session, permission_mode="default", confirm=None, concurrency=3, **shared
    ):
        self.project = Path(project).resolve()
        self.session = session
        self.permission_mode = permission_mode
        self.confirm = confirm
        self.specs = {}
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
        self.concurrency = concurrency
        self.deny_rules = shared.get("deny_rules", ())

    @property
    def scheduler(self):
        return self.coordination

    @property
    def file_lock(self):
        return self._file_lock

    def register(self, spec):
        Draft202012Validator.check_schema(spec.schema)
        self.specs[spec.name] = spec

    def declarations(self):
        return tuple(
            self.specs[name].declaration()
            for name in sorted(self.specs)
            if not name.startswith("mcp__")
        )

    def resolve_path(self, value):
        path = (self.project / value).resolve()
        if not path.is_relative_to(self.project):
            raise PermissionError("Path escapes project root")
        return path

    @asynccontextmanager
    async def _slot(self, read):
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

    async def _allowed(self, spec, args, context):
        if any(rule(spec, args) for rule in self.deny_rules):
            return False
        mode = getattr(context, "permission_mode", self.permission_mode)
        modes = [mode]
        parent = getattr(context.services.get("agent"), "parent_agent", None)
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
        if self.confirm is None:
            return False
        async with self._approval:
            answer = self.confirm(spec, args, context)
            return bool(await answer if inspect.isawaitable(answer) else answer)

    async def _run(self, call, context):
        spec = self.specs.get(call.name)
        result = ToolResult(call.id, call.name, "", status="invalid")
        if spec is None:
            result.content = "Unknown tool"
            return result
        errors = list(Draft202012Validator(spec.schema).iter_errors(call.arguments))
        if errors:
            result.content = "Arguments do not satisfy the tool schema"
            return result
        read = (
            None
            if spec.execution_mode == "dispatch"
            else spec.execution_mode == "read" and spec.permission == "read"
        )
        try:
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
            result.effect = "unknown" if result.started and spec.permission != "read" else "none"
        except Exception as exc:
            result.status = "error"
            result.content = f"Tool failed ({type(exc).__name__})"
            result.effect = "unknown" if result.started and spec.permission != "read" else "none"
        return result

    async def _finalize(self, result, context):
        self._persist(result)
        emit = getattr(context, "emit", None)
        if emit:
            await emit("tool_completed", {"call_id": result.call_id, "status": result.status})
        return result

    async def execute(self, call, context):
        return await self._finalize(await self._run(call, context), context)

    def _persist(self, result):
        raw = result.content.encode("utf-8")
        directory = self.resolve_path(".weyra/artifacts")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (hashlib.sha256(raw).hexdigest() + ".txt")
        path.write_bytes(raw)
        result.output_ref = str(path)
        if len(result.content) > 8000:
            result.content = (
                result.content[:6000]
                + f"\n… [truncated; full output: {path}]\n"
                + result.content[-2000:]
            )
        self.session.append("tool_result", vars(result).copy(), checkpoint=True)

    async def execute_batch(self, calls, context):
        calls = list(calls)
        results = []
        index = 0
        while index < len(calls):
            segment = []
            while index < len(calls):
                call = calls[index]
                spec = self.specs.get(call.name)
                safe = spec and spec.permission == "read" and spec.execution_mode == "read"
                if segment and not safe:
                    break
                segment.append(call)
                index += 1
                if not safe:
                    break
            tasks = [asyncio.create_task(self._run(call, context)) for call in segment]
            try:
                values = await asyncio.shield(asyncio.gather(*tasks))
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                values = await asyncio.gather(*tasks)
                for call in calls[index:]:
                    values.append(ToolResult(call.id, call.name, "Cancelled", "cancelled"))
                index = len(calls)
            for value in values:
                results.append(await self._finalize(value, context))
        return results


def install_builtin_tools(runtime, jobs):
    def schema(properties, required=()):
        return {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        }

    string = {"type": "string"}
    integer = {"type": "integer", "minimum": 0}

    def register(name, props, required, handler, permission="read"):
        runtime.register(
            ToolSpec(
                name,
                name.replace("_", " "),
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
        lines = raw.decode("utf-8").splitlines(keepends=True)
        start = args.get("offset", 0)
        return "".join(lines[start : start + args.get("limit", 2000)])

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
            and ".weyra" not in p.parts
        ][:5000]

    def grep(args, ctx):
        matches = []
        for name in listing(args, ctx):
            path = runtime.resolve_path(name)
            try:
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if args["pattern"] in line:
                        matches.append({"path": name, "line": number, "text": line})
                        if len(matches) >= 1000:
                            return matches
            except (UnicodeError, OSError):
                continue
        return matches

    async def shell(args, ctx):
        status = await jobs.start(args["command"], ctx.id, runtime.project)
        if hasattr(ctx, "register_resource"):
            ctx.register_resource(lambda: jobs.cleanup_owner(ctx.id))
        if args["background"]:
            return status
        try:
            status = await jobs.wait(status["job_id"], args.get("timeout", 30))
            return {**status, "output": Path(status["output_path"]).read_text(errors="replace")}
        except asyncio.CancelledError:
            await jobs.cancel(status["job_id"])
            raise

    def owned(args, ctx):
        if jobs.status(args["job_id"])["owner"] != ctx.id:
            raise PermissionError("Job belongs to another turn")

    async def wait(args, ctx):
        owned(args, ctx)
        return await jobs.wait(args["job_id"], args.get("timeout", 30))

    async def cancel(args, ctx):
        owned(args, ctx)
        return await jobs.cancel(args["job_id"])

    def transfer(args, ctx):
        return jobs.transfer(args["job_id"], ctx.id, args["owner"])

    register("read_file", {"path": string, "offset": integer, "limit": integer}, ["path"], read)
    register("write_file", {"path": string, "content": string}, ["path", "content"], write, "write")
    register(
        "edit_file",
        {"path": string, "old_text": string, "new_text": string},
        ["path", "old_text", "new_text"],
        write,
        "write",
    )
    register("list_files", {"path": string}, [], listing)
    register("grep_search", {"path": string, "pattern": string}, ["pattern"], grep)
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
        lambda a, c: (owned(a, c), jobs.status(a["job_id"]))[1],
    )
    register(
        "wait_job",
        {"job_id": string, "timeout": {"type": "number", "minimum": 0, "maximum": 300}},
        ["job_id"],
        wait,
    )
    register("cancel_job", {"job_id": string}, ["job_id"], cancel, "shell")
    register(
        "transfer_job", {"job_id": string, "owner": string}, ["job_id", "owner"], transfer, "shell"
    )

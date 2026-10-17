"""One audited policy and scheduling path for every tool invocation."""

from __future__ import annotations
import asyncio
import hashlib
import inspect
import json
from contextlib import asynccontextmanager
from pathlib import Path
from jsonschema import Draft202012Validator
from .contracts import ToolResult
from .output import preview_output


class ToolRuntime:
    def __init__(
        self, project, session, permission_mode="default", confirm=None, concurrency=10, **shared
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
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
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

    def _policy(self, spec, args, context):
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
        spec = self.specs.get(call.name)
        result = ToolResult(call.id, call.name, "", status="invalid")
        try:
            if previous is not None:
                await previous.wait()
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
        if persist:
            self._persist(result)
        emit = getattr(context, "emit", None)
        if emit:
            await emit("tool_completed", {"call_id": result.call_id, "status": result.status})
        return result

    async def execute(self, call, context):
        result = await self._finalize(await self._run(call, context), context)
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        return result

    def _persist(self, result):
        raw = result.content.encode("utf-8")
        directory = self.resolve_path(".weyra/artifacts")
        directory.mkdir(parents=True, exist_ok=True)
        path = self.resolve_path(str(directory / (hashlib.sha256(raw).hexdigest() + ".txt")))
        path.write_bytes(raw)
        result.output_ref = str(path)
        result.content = preview_output(result)
        self.session.append("tool_result", vars(result).copy(), checkpoint=True)

    async def execute_batch(self, calls, context):
        """Prepare in call order, roll safe bodies through a bounded pool, commit in order."""
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


# Compatibility for existing callers; concrete tools live in agents.tools.
from .tools import install_builtin_tools  # noqa: E402,F401

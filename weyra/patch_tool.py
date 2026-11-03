"""Opt-in multi-file patch execution through the existing write permission boundary.

Preflight simulates every operation before touching targets. Commit is sequential,
not transactional: failed or cancelled commits stop and expose partial outcomes.
Like the existing file tools, version checks are optimistic, not an OS-level lock
against external writers between the final check and a filesystem operation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .contracts import ToolResult
from .patch import apply_hunks, parse_patch
from .presentation import generate_unified_diff


@dataclass(frozen=True)
class Change:
    name: str
    path: Path
    before: bytes | None
    after: bytes | None
    authorization: tuple


def _read(path):
    return path.read_bytes() if path.exists() else None


def _resolve(runtime, ctx, name):
    path = runtime.resolve_path(name)
    state = (runtime.project / ".weyra").resolve()
    if path.is_relative_to(state) and path != getattr(ctx, "plan_path", None):
        raise PermissionError("Harness state is managed through dedicated operations")
    return path


def _report(records, status, reason, diffs):
    groups = {
        key: [item for item in records if item["status"] == key]
        for key in ("completed", "failed", "unknown", "skipped")
    }
    effect = (
        "unknown"
        if groups["unknown"]
        else "completed"
        if groups["completed"]
        or any(step["status"] == "completed" for item in records for step in item["steps"])
        else "none"
    )
    structured = {
        "operations": records,
        **groups,
        "_display": {"diff": "\n".join(diff for diff in diffs if diff)},
    }
    content = json.dumps({"status": status, "reason": reason, **groups}, ensure_ascii=False)
    return ToolResult(
        "", "apply_patch", content, status=status, effect=effect, structured=structured
    )


async def apply_patch(runtime, args, ctx):
    """Validate and authorize the entire patch, then commit ordered primitives."""
    try:
        operations = parse_patch(args["input"])
    except ValueError as exc:
        return _report([], "error", str(exc), [])
    records = [
        {
            "index": i,
            "operation": op.kind,
            "path": op.path,
            "move_to": op.move_to,
            "status": "skipped",
            "steps": [],
        }
        for i, op in enumerate(operations)
    ]
    plans = []
    initial = {}
    simulated = {}
    diffs = []
    current = 0
    patch_spec = runtime.specs["apply_patch"]

    def check_permission(item):
        spec, tool_args = item.authorization
        if (
            runtime.specs.get("apply_patch") is not patch_spec
            or runtime.specs.get(spec.name) is not spec
        ):
            raise PermissionError("Tool registration changed during patch execution")
        if (
            runtime._policy(patch_spec, args, ctx) is False
            or runtime._policy(spec, tool_args, ctx) is False
        ):
            raise PermissionError("Patch operation permission revoked")

    def load(path):
        if path not in simulated:
            raw = _read(path)
            if (
                raw is not None
                and ctx.read_versions.get(str(path)) != hashlib.sha256(raw).hexdigest()
            ):
                raise ValueError("Read the current file before modifying it")
            initial[path] = raw
            simulated[path] = raw
        return simulated[path]

    def change(name, path, after, tool, tool_args):
        before = load(path)
        if after is not None:
            for parent in path.parents:
                if parent == runtime.project:
                    break
                if parent in simulated:
                    if simulated[parent] is not None:
                        raise ValueError("A file blocks the target parent directory")
                elif parent.exists() and not parent.is_dir():
                    raise ValueError("A file blocks the target parent directory")
            if any(
                other != path and other.is_relative_to(path) and raw is not None
                for other, raw in simulated.items()
            ):
                raise ValueError("A planned directory blocks the file target")
        simulated[path] = after
        return Change(name, path, before, after, (runtime.specs[tool], tool_args))

    try:
        async with runtime.file_lock:
            for current, op in enumerate(operations):
                ctx.check_cancelled()
                path = _resolve(runtime, ctx, op.path)
                before = load(path)
                if op.kind == "add":
                    if before is not None:
                        raise ValueError("Add File target already exists")
                    changes = [
                        change(
                            op.path,
                            path,
                            op.content.encode("utf-8"),
                            "write_file",
                            {"path": op.path, "content": op.content},
                        )
                    ]
                else:
                    if before is None:
                        raise ValueError("Patch source does not exist")
                    text = before.decode("utf-8")
                    if op.kind == "delete":
                        changes = [
                            change(
                                op.path, path, None, "write_file", {"path": op.path, "content": ""}
                            )
                        ]
                    else:
                        content = apply_hunks(text, op.hunks)
                        if op.move_to is None:
                            changes = [
                                change(
                                    op.path,
                                    path,
                                    content.encode("utf-8"),
                                    "edit_file",
                                    {"path": op.path, "old_text": text, "new_text": content},
                                )
                            ]
                        else:
                            destination = _resolve(runtime, ctx, op.move_to)
                            if destination.is_relative_to(path) or load(destination) is not None:
                                raise ValueError("Move destination must be a different absent file")
                            # Validate both endpoints before granting a move any effects.
                            source = change(
                                op.path, path, None, "write_file", {"path": op.path, "content": ""}
                            )
                            target = change(
                                op.move_to,
                                destination,
                                content.encode("utf-8"),
                                "write_file",
                                {"path": op.move_to, "content": content},
                            )
                            changes = [target, source]
                plans.append(changes)
                authorizations = reversed(changes) if op.move_to is not None else changes
                for item in authorizations:
                    if not await runtime._allowed(*item.authorization, ctx):
                        raise PermissionError("Patch operation permission denied")
                    ctx.check_cancelled()
            # Authorization can await a user and allow external edits or policy revocation.
            # Recheck every source/destination before the first target write.
            for current, changes in enumerate(plans):
                for item in changes:
                    check_permission(item)
                    if _resolve(runtime, ctx, item.name) != item.path:
                        raise PermissionError("Patch path changed during preflight")
            for path, raw in initial.items():
                if _read(path) != raw:
                    current = next(
                        i
                        for i, changes in enumerate(plans)
                        if any(item.path == path for item in changes)
                    )
                    raise ValueError("Patch target changed during preflight")
    except asyncio.CancelledError:
        return _report(records, "cancelled", "Patch cancelled before commit", diffs)
    except Exception as exc:
        records[current]["status"] = "failed"
        records[current]["error"] = str(exc)
        return _report(
            records,
            "denied" if isinstance(exc, PermissionError) else "error",
            "Patch preflight rejected",
            diffs,
        )

    expected = dict(initial)
    for current, changes in enumerate(plans):
        record = records[current]
        try:
            # A scheduling checkpoint makes task cancellation observable between operations.
            await asyncio.sleep(0)
            ctx.check_cancelled()
            async with runtime.file_lock:
                # Check both endpoints together before beginning a move.
                for item in changes:
                    check_permission(item)
                    if _resolve(runtime, ctx, item.name) != item.path:
                        raise PermissionError("Patch path changed before commit")
                    if _read(item.path) != expected[item.path]:
                        raise ValueError("Patch target changed before commit")
                for item in changes:
                    ctx.check_cancelled()
                    check_permission(item)
                    if _resolve(runtime, ctx, item.name) != item.path:
                        raise PermissionError("Patch path changed before commit")
                    if _read(item.path) != expected[item.path]:
                        raise ValueError("Patch target changed before commit")
                    capture = ctx.services.get("replay_capture")
                    if capture is not None:
                        capture.file(
                            item.path.relative_to(runtime.project).as_posix(),
                            item.before,
                            write=True,
                        )
                    step = {
                        "path": item.name,
                        "action": "delete" if item.after is None else "write",
                        "status": "unknown",
                    }
                    record["steps"].append(step)
                    if item.after is None:
                        item.path.unlink()
                        ctx.read_versions.pop(str(item.path), None)
                    else:
                        item.path.parent.mkdir(parents=True, exist_ok=True)
                        item.path.write_bytes(item.after)
                        # Only certify the version actually written, not a concurrent edit.
                        ctx.read_versions[str(item.path)] = hashlib.sha256(item.after).hexdigest()
                    step["status"] = "completed"
                    expected[item.path] = item.after
                    diffs.append(
                        generate_unified_diff(
                            item.name,
                            (item.before or b"").decode("utf-8"),
                            (item.after or b"").decode("utf-8"),
                        )
                    )
                record["status"] = "completed"
        except asyncio.CancelledError:
            if record["steps"]:
                record["status"] = "unknown"
            return _report(
                records, "cancelled", "Patch cancelled; remaining operations skipped", diffs
            )
        except Exception as exc:
            record["status"] = "unknown" if record["steps"] else "failed"
            record["error"] = str(exc)
            return _report(
                records,
                "denied" if isinstance(exc, PermissionError) else "error",
                "Patch commit stopped; remaining operations skipped",
                diffs,
            )
    return _report(records, "success", "Patch applied", diffs)

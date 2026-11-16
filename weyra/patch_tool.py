"""受管多文件补丁事务仿真与顺序提交工具 (Opt-in Multi-File Patch Execution Tool)。

【架构定位】(Architectural Role)
    位于 `weyra` 核心工具执行层，作为智能体专用的代码补丁修改门面（`apply_patch`）。
    串联 `patch.py` 纯解析器，在真实接触文件系统前执行全量预检仿真（Preflight Simulation）、
    两阶段权限校验与乐观并发版本检查，随后在文件锁保护下执行顺序原子提交。

【开源设计思想与架构血统】(Design Pedigree)
    1. 遵循 DeepSeek-Harness 的两阶段提交与安全仿真哲学（Preflight-then-Commit）：
       在修改任何物理文件前，必须在内存中完整模拟整个补丁链条并校验全部目录与权限；
    2. 继承 Pi Agent 乐观版本控制机制（Optimistic Read-Version Checking）：
       基于 SHA-256 哈希比对 `ctx.read_versions`，强制要求智能体“修改前必须先读取当前版本”，杜绝盲目覆写；
    3. 集成统一差异展示（Unified Diff Generation）：执行完毕后自动生成直观的代码 Diff 预览。

【核心不变性与安全约束】(Invariants & Safety)
    - 状态目录绝对禁写：严禁通过补丁机制修改 `.weyra` 核心状态目录文件；
    - 预检完整性拦截：若预检期间有任何一个文件冲突、权限不足或目录重叠，立即整体中止，绝不修改任何磁盘；
    - 提交期取消可观测：在顺序写入循环中注入调度让步点（`asyncio.sleep(0)`），支持用户即时优雅打断。
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
    """单个文件拟变更的物理前后快照实体 (File Change Descriptor)。

    Attributes:
        name: 逻辑相对路径名。
        path: 物理绝对文件路径。
        before: 变更前的字节内容（若文件原本不存在则为 None）。
        after: 拟写入的目标字节内容（若为删除操作则为 None）。
        authorization: 包含对应工具 Spec 与参数的鉴权元组。
    """

    name: str
    path: Path
    before: bytes | None
    after: bytes | None
    authorization: tuple


def _read(path: Path) -> bytes | None:
    """安全读取文件全部原始字节内容，若文件不存在则返回 None。

    Args:
        path: 目标文件路径。

    Returns:
        bytes | None: 文件的字节内容或 None。
    """
    return path.read_bytes() if path.exists() else None


def _resolve(runtime, ctx, name: str) -> Path:
    """解析并校验目标路径，防止逃逸项目沙箱或越权篡改内部状态。

    Args:
        runtime: 工具运行时环境。
        ctx: 轮次执行上下文。
        name: 相对路径名。

    Returns:
        Path: 解析出的规范化绝对路径。

    Raises:
        PermissionError: 当尝试篡改系统受管状态目录 `.weyra` 时抛出。
    """
    path = runtime.resolve_path(name)
    state = (runtime.project / ".weyra").resolve()
    if path.is_relative_to(state) and path != getattr(ctx, "plan_path", None):
        raise PermissionError("Harness state is managed through dedicated operations")
    return path


def _report(records: list[dict], status: str, reason: str, diffs: list[str]) -> ToolResult:
    """组装结构化工具执行报告并附加统一 Diff 视图。

    Args:
        records: 各操作项的执行状态流水记录。
        status: 全局状态（'success', 'error', 'denied', 'cancelled'）。
        reason: 状态原因描述。
        diffs: 各文件修改生成的统一差异列表。

    Returns:
        ToolResult: 标准工具执行结果对象。
    """
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


async def apply_patch(runtime, args: dict, ctx) -> ToolResult:
    """校验、鉴权并顺序执行多文件代码补丁操作。

    【执行阶段分解】
    1. 补丁解析：调用 `parse_patch` 将输入文本解析为有序原子操作；
    2. 预检仿真（Preflight Simulation）：
       - 逐项检查文件存在性与乐观读取版本匹配（`read_versions` SHA-256）；
       - 内存推演目录结构与父级冲突；
       - 执行两阶段权限审批判定（`runtime._allowed`）；
       - 在正式物理写入前，复核所有目标文件的哈希值，确保无外部并发篡改；
    3. 顺序提交（Sequential Commit）：
       - 申请 `runtime.file_lock` 文件排他锁；
       - 逐个文件执行物理创建、修改或删除；
       - 同步更新上下文读取版本并生成代码 Diff。

    Args:
        runtime: 核心工具运行时实例。
        args: 包含 'input' 补丁文本的参数字典。
        ctx: 包含取消标记、读取版本映射与服务的执行上下文。

    Returns:
        ToolResult: 包含执行状态、明细与差异文本的标准工具结果。
    """
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

    def check_permission(item: Change) -> None:
        """复核操作项的权限策略是否依然有效。"""
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

    def load(path: Path) -> bytes | None:
        """加载文件当前内容并校验前置读取版本一致性。"""
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

    def change(name: str, path: Path, after: bytes | None, tool: str, tool_args: dict) -> Change:
        """模拟文件变更并校验路径与父级目录合法性。"""
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
                            # 在授予移动任何副作用前，严格校验源与目标两端
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
            # 权限审批可能会触发用户等待并允许外部编辑或策略撤销。
            # 在首个写入动作前，重新校验所有源与目标路径及文件哈希
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
            # 显式调度检查点，使任务取消在各操作步骤间立即可感知
            await asyncio.sleep(0)
            ctx.check_cancelled()
            async with runtime.file_lock:
                # 在开始移动前统一检查源与目标端点
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
                        # 仅认证真实写入的内容版本，防止外部并发修改
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

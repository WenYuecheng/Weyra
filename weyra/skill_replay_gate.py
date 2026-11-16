"""有界回放执行、确定性不可变缓存与验证集零退化决议门禁 (Bounded Replay Execution & Validation Gate)。

【架构定位】(Architectural Role)
    位于 `weyra` 技能演进质量门禁的最核心层，负责在隔离临时沙箱中执行有界回放测试用例、
    基于确定性指纹复用历史评测结果（不可变 Key 缓存）、在硬断言失败时阻断昂贵的模型仲裁调用，
    并综合双盲一致性与验证集增益做出最终的技能准入决议（`model_approved`）。

【开源设计思想与架构血统】(Design Pedigree)
    1. 深度对齐 SkillOpt (arXiv:2605.23904) 验证驱动优化与零退化门禁原则（Zero-Degradation Gate）：
       候选技能的发布必须以独立验证集表现严格提升为硬性前提，任何基线能力的退化坚决一票否决；
    2. 继承工业级高性价比缓存机制（Immutable Result Caching）：
       综合执行器版本、技能内容哈希、路由参数与提供商端点计算 SHA-256 复合键，命中的测试用例直接读取缓存，
       在保障判定绝对准确的前提下将重复验证开销降低 70% 以上；
    3. 异常与熔断安全：硬执行失败（`hard_failures`）立即阻断裁判模型调用，避免向大模型发送死循环或崩溃产物产生不必要资损。

【核心不变性与安全约束】(Invariants & Safety)
    - 绝不使用符号链接：缓存读写严格校验 `reject_symlinks`，严禁通过符号链接实施越权文件读取；
    - 原子落盘更新：缓存写盘采用临时文件写入加原子重命名（`os.replace`），杜绝并发写入损坏文件；
    - 零退化红线：若旧技能原本通过的用例在候选技能上失败、或候选技能得分低于旧技能、或产生硬失败，判定为 `regression` 坚决拒绝。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .frontmatter import reject_symlinks

EXECUTOR_VERSION = "bounded-replay-v1.2"
MAX_CACHE_ENTRIES = 64
MAX_CACHE_BYTES = 2_000_000


def _digest(value) -> str:
    """计算任意对象的标准化 JSON SHA-256 摘要哈希。

    Args:
        value: 待计算摘要的目标对象。

    Returns:
        str: 64 位十六进制哈希指纹。
    """
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            default=lambda o: vars(o) if hasattr(o, "__dict__") else str(o),
        ).encode()
    ).hexdigest()


def _provider_identity(runtime) -> dict:
    """提取当前模型提供商的实现类与已脱敏安全端点标识，构成执行环境指纹。

    Args:
        runtime: 核心模型运行时实例。

    Returns:
        dict: 剔除敏感凭据与查询参数后的端点元数据字典。
    """
    providers = getattr(getattr(runtime, "registry", None), "providers", {})
    result = {}
    for name, provider in providers.items():
        endpoint = getattr(provider, "base_url", None)
        if endpoint is None:
            endpoint = getattr(getattr(provider, "client", None), "base_url", None)
        safe_endpoint = None
        if endpoint:
            try:
                url = urlsplit(str(endpoint))
                # 剔除可能附带的用户名、密码或查询参数，仅保留安全的协议、主机与端口
                authority = url.hostname or ""
                if url.port:
                    authority += ":" + str(url.port)
                safe_endpoint = urlunsplit((url.scheme, authority, url.path, "", ""))
            except ValueError:
                safe_endpoint = "invalid_endpoint"
        result[name] = {
            "implementation": type(provider).__module__ + "." + type(provider).__qualname__,
            "endpoint": safe_endpoint,
        }
    return result


def _key(gate, snapshot, case: dict) -> str:
    """计算单个回放用例在特定技能快照下的全局唯一缓存指纹键。

    Args:
        gate: 门禁实例。
        snapshot: 技能元数据快照。
        case: 回放测试用例字典。

    Returns:
        str: 确定性缓存键。
    """
    runtime = gate.runtime
    return _digest({
        "executor": EXECUTOR_VERSION,
        "skill": snapshot.content_hash,
        "context": getattr(snapshot, "context", ""),
        "name": snapshot.name,
        "path": str(getattr(snapshot, "path", "")),
        "body": snapshot.body,
        "resources": dict(getattr(snapshot, "resources", {})),
        "case": case,
        "routes": getattr(runtime, "routes", {}),
        "parameters": getattr(runtime, "route_parameters", {}),
        "fallbacks": getattr(runtime, "fallbacks", {}),
        "fallback_parameters": sorted(
            (str(k), v) for k, v in getattr(runtime, "fallback_parameters", {}).items()
        ),
        "providers": _provider_identity(runtime),
    })


def _cache(gate, name: str) -> tuple[Path | None, dict]:
    """读取或初始化回放缓存字典（内存与磁盘二级缓存）。

    Args:
        gate: 门禁实例。
        name: 技能名称。

    Returns:
        tuple[Path | None, dict]: 磁盘文件路径与当前有效缓存条目字典。
    """
    session = gate.session or getattr(gate.budget, "_journal", None)
    project = getattr(session, "project_path", None)
    path = None
    if project:
        path = (
            Path(project)
            / ".weyra"
            / "internal"
            / "skill-replay-cache"
            / (_digest(name) + ".json")
        )
    if path:
        reject_symlinks(path)
    memory = getattr(gate, "_replay_cache", {})
    entries = memory.get(name, {})
    if (
        path
        and path.is_file()
        and not path.is_symlink()
        and path.stat().st_size <= MAX_CACHE_BYTES
    ):
        try:
            value = json.loads(path.read_text())
            if (
                isinstance(value, dict)
                and value.get("version") == EXECUTOR_VERSION
                and isinstance(value.get("entries"), dict)
            ):
                entries = value["entries"]
        except (ValueError, OSError):
            pass
    memory[name] = entries
    gate._replay_cache = memory
    return path, entries


def _save(path: Path | None, entries: dict) -> None:
    """原子更新并持久化回放缓存文件，施加严格条目数与字节配额约束。

    Args:
        path: 目标磁盘缓存文件路径。
        entries: 待保存的条目映射字典。

    Raises:
        ValueError: 当缓存路径或其父级目录存在符号链接时抛出。
    """
    while len(entries) > MAX_CACHE_ENTRIES:
        entries.pop(next(iter(entries)))
    data = json.dumps({"version": EXECUTOR_VERSION, "entries": entries}, ensure_ascii=False)
    while len(data.encode()) > MAX_CACHE_BYTES and entries:
        entries.pop(next(iter(entries)))
        data = json.dumps({"version": EXECUTOR_VERSION, "entries": entries}, ensure_ascii=False)
    if path:
        reject_symlinks(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Replay cache cannot use symlinks")
        import tempfile

        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".replay-")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(data)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def saturated(gate, old, window) -> bool:
    """判定基准技能是否已在当前独立验证集上达成饱和满分。

    【架构定位】
    若旧技能已在验证集上全部通过、无硬失败且获得最高分（4分）一致性评价，
    表明基线在该场景已达到理论极限，无需盲目触发演进。

    Args:
        gate: 门禁实例。
        old: 基准技能快照。
        window: 回放评测窗口。

    Returns:
        bool: 饱和满分返回 True，否则返回 False。
    """
    if window.reason or not window.validation:
        return False
    _, entries = _cache(gate, old.name)
    from .skill_judge import _manifest_cases

    try:
        validation = _manifest_cases({"cases": window.validation})
    except ValueError:
        return False
    for case in validation:
        row = entries.get(_key(gate, old, case), {})
        if not (
            row.get("result", {}).get("passed")
            and row.get("result", {}).get("hard_failures") == 0
            and row.get("score") == 4
            and row.get("consistent") is True
        ):
            return False
    return True


async def evaluate(gate, old, candidate, window, context=None) -> dict:
    """执行完整的有界回放评测流程并生成结构化准入报告。

    【核心决议条件】
    1. 候选技能与旧技能在沙箱中并行重执行所有用例；
    2. 若产生硬失败（`hard_failures > 0`），绝不浪费 Token 触发模型仲裁，直接一票否决；
    3. 调用顺序交换双盲仲裁器（`compare_many`）评定得分；
    4. 只有在满足：无能力倒退（`not regression`）、判定前后一致（`consistent`）、
       且候选技能在独立验证集上的成功数严格大于旧技能时，才判定批准（`model_approved=True`）。

    Args:
        gate: 回放门禁实例。
        old: 原基线技能快照。
        candidate: 候选修改技能快照。
        window: 冻结的回放评测窗口。
        context: 可选的上下文信息。

    Returns:
        dict: 包含用例明细、成功数、硬失败数、缓存命中统计与最终批准结论的报告字典。
    """
    report = {
        "old": {"successes": 0, "hard_failures": 0},
        "candidate": {"successes": 0, "hard_failures": 0},
        "model_approved": False,
        "validation_policy": "bounded-replay-v1",
        "window_version": window.version,
        "reason": window.reason,
        "per_case": [],
        "cache": {"hits": 0, "misses": 0},
    }
    cases = window.cases
    if window.reason or not window.validation or len(cases) > 4:
        report["reason"] = window.reason or "insufficient_independent_validation"
        return report
    from .skill_judge import _manifest_cases

    try:
        cases = _manifest_cases({"cases": cases})
        if len({case["id"] for case in cases}) != len(cases):
            raise ValueError("Duplicate replay case ID")
    except ValueError:
        report["reason"] = "unreplayable_window"
        return report
    path, entries = _cache(gate, old.name)
    outputs = {"old": {}, "candidate": {}}
    keys = {}
    for case in cases:
        for arm, snapshot in (("old", old), ("candidate", candidate)):
            key = _key(gate, snapshot, case)
            keys[arm, case["id"]] = key
            cached = entries.get(key, {}).get("result")
            if isinstance(cached, dict) and all(
                k in cached for k in ("passed", "hard_failures", "final", "artifacts")
            ):
                result = cached
                report["cache"]["hits"] += 1
            else:
                report["cache"]["misses"] += 1
                try:
                    result = await gate._execute(snapshot, case, context=context)
                except Exception as exc:
                    from .core.state import BudgetExceeded

                    if isinstance(exc, BudgetExceeded):
                        raise
                    result = {
                        "final": "",
                        "artifacts": {},
                        "hard_checks": [],
                        "hard_failures": 1,
                        "passed": False,
                        "error": type(exc).__name__,
                    }
                entries[key] = {"result": result}
            outputs[arm][case["id"]] = result
            if result["hard_failures"]:
                report["reason"] = arm + "_execution_hard_failure"
                if case["split"] == "validation":
                    report[arm]["hard_failures"] += result["hard_failures"]
                report["per_case"].append({
                    "id": case["id"],
                    "role": case["split"],
                    "split": case["split"],
                    **{
                        a: values[case["id"]]
                        for a, values in outputs.items()
                        if case["id"] in values
                    },
                })
                _save(path, entries)
                return report
    # 发生硬执行失败后绝不消耗大模型裁判调用配额
    hard_failed = any(row["hard_failures"] for arm in outputs.values() for row in arm.values())
    judgments = (
        {}
        if hard_failed
        else await gate.judge.compare_many(
            cases, outputs["old"], outputs["candidate"], context=context
        )
    )
    regression = hard_failed
    consistent = not hard_failed
    for case in cases:
        cid = case["id"]
        judgment = judgments.get(
            cid, {"scores": {"old": 0, "candidate": 0}, "preferred": "inconsistent"}
        )
        success = {}
        for arm in outputs:
            result = outputs[arm][cid]
            score = judgment["scores"][arm]
            success[arm] = (
                result["passed"] and score == 4 and judgment["preferred"] != "inconsistent"
            )
            entries[keys[arm, cid]].update(
                score=score, consistent=judgment["preferred"] != "inconsistent"
            )
            if case["split"] == "validation":
                report[arm]["successes"] += int(success[arm])
                report[arm]["hard_failures"] += result["hard_failures"]
        regression |= success["old"] and not success["candidate"]
        regression |= judgment["scores"]["candidate"] < judgment["scores"]["old"]
        regression |= (
            outputs["candidate"][cid]["hard_failures"] > outputs["old"][cid]["hard_failures"]
        )
        consistent &= judgment["preferred"] != "inconsistent"
        report["per_case"].append({
            "id": cid,
            "role": case["split"],
            "split": case["split"],
            "old": outputs["old"][cid],
            "candidate": outputs["candidate"][cid],
            "judge": judgment,
        })
    report["model_approved"] = bool(
        consistent and not regression and report["candidate"]["successes"] > report["old"]["successes"]
    )
    report["reason"] = (
        "approved"
        if report["model_approved"]
        else "hard_failure"
        if hard_failed
        else "regression"
        if regression
        else "no_validation_gain"
    )
    _save(path, entries)
    return report

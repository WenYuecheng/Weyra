"""SkillOpt 有界技能演化存储与 SkillDiff 原位防坍缩引擎 (Bounded Skill Evolution Store).

【架构定位】
本模块负责 Weyra 智能体技能（`SKILL.md`）的受控版本演化、并发冲突防护（OCC）、
语义版本号递增、历史快照归档、崩溃恢复与原子回滚。

【开源框架与学术论文溯源】
1. Microsoft SkillOpt (`microsoft/SkillOpt`, NeurIPS 2026 Spotlight, arXiv:2605.23904, MIT):
   - 有界文本补丁原语（`append` / `insert_after` / `replace` / `delete`）与受保护块隔离：
     直接调用 `weyra.skillopt_vendor.skill.apply_patch_with_report`；
   - 文本学习率编辑预算调度（`constant` / `linear` / `cosine` / `autonomous`）：
     直接调用 `weyra.skillopt_vendor.scheduler.build_scheduler`；
   - 留出验证集严格提升门禁（`accept_new_best` / `accept` / `reject`）：
     经 `SkillValidationGate` 计算得分并调用 `weyra.skillopt_vendor.gate.evaluate_gate`；
   - 被拒编辑负反馈缓冲（`rejected_edits.jsonl`）：记录未通过门禁的补丁与分差，回流优化器避坑；
   - SkillOpt-Sleep 受保护学习区（`apply_learned_edits`）：直接调用
     `weyra.skillopt_vendor.memory.apply_edits_detailed`，仅维护 `LEARNED` 区块而不触碰手写正文。
2. SkillDiff (`LitaoGuo/How-Self-Evolution-Reshapes-Skills`, arXiv:2611.25534, Nov 2026, MIT):
   - 调用 `weyra.skilldiff_vendor.items.split_items` 与 `weyra.skilldiff_vendor.skilldiff.mark`
     对模型先验未覆盖的专家知识就地标注 `**[FOCUS]**`（`annotate_skilldiff`）；
   - 未经外部验证门禁通过的补丁禁止删除或破坏 `**[FOCUS]**` 专家条目，防止向模型先验坍缩。
3. 工程可靠性（继承 Weyra 事务边界）:
   - POSIX 原子写入（`atomic`）、符号链接防逃逸（`reject_symlinks`）、`base_hash` 乐观锁、
     单调 SemVer 归档快照及 `recover()` 崩溃恢复状态机（恢复期间绝不隐式发布未落盘变更）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
from types import MappingProxyType
from typing import Sequence
import uuid

from .frontmatter import atomic, parse_markdown, reject_symlinks, sha, tokens
from .runtime_state import BudgetExceeded
from .skilldiff_vendor.items import split_items
from .skilldiff_vendor.skilldiff import BODY_TYPES, MARK, mark
from .skillopt_vendor.memory import apply_edits_detailed
from .skillopt_vendor.scheduler import build_scheduler
from .skillopt_vendor.skill import apply_patch_with_report
from .skillopt_vendor.types import Edit, EditRecord, Patch

MAX_REJECTED_EDITS = 20
_VALID_OPS = frozenset({"append", "insert_after", "replace", "delete"})


def compute_lexical_prior_coverage(
    raw_skill: str,
    prior_texts: Sequence[str],
    *,
    threshold: float = 0.45,
) -> dict[str, int]:
    """基于词法覆盖率统计每个 SkillDiff 原子条目在模型先验样本中的命中次数（零 API 离线可用）。

    对齐 `LitaoGuo/How-Self-Evolution-Reshapes-Skills` 的 `coverage[item_id]` 接口格式，
    返回供 `skilldiff.mark(original, items, coverage)` 直接消费的 `{item_id: hit_count}` 字典。
    """
    items = split_items(raw_skill)
    prior_token_sets = [set(tokens(text)) for text in prior_texts if isinstance(text, str) and text.strip()]
    coverage: dict[str, int] = {}
    for item in items:
        if item["type"] not in BODY_TYPES:
            continue
        item_tokens = set(tokens(item["text"]))
        if not item_tokens:
            coverage[item["id"]] = 0
            continue
        hits = 0
        for ptokens in prior_token_sets:
            overlap = len(item_tokens & ptokens) / len(item_tokens)
            if overlap >= threshold:
                hits += 1
        coverage[item["id"]] = hits
    return coverage


def _normalize_edit(edit: Edit | dict) -> Edit:
    """将输入字典或 Edit 对象规整化为标准 `SkillOpt` `Edit`（`append`/`insert_after`/`replace`/`delete`）。"""
    if isinstance(edit, Edit):
        op = edit.op
        target = edit.target
        content = edit.content
    elif isinstance(edit, dict):
        op = edit.get("op", edit.get("type", ""))
        target = edit.get("target", "")
        content = edit.get("content", "")
    else:
        raise ValueError("Unsupported edit")
    if op not in _VALID_OPS:
        raise ValueError(f"Unsupported edit op: {op!r}")
    if not isinstance(target, str) or not isinstance(content, str):
        raise ValueError("Edit target and content must be strings")
    if op in {"append", "insert_after"} and not content.strip():
        raise ValueError("Append/insert_after edit requires non-empty content")
    if op in {"replace", "delete"} and not target:
        raise ValueError("Replace/delete edit requires target")
    if op == "replace" and not content.strip():
        raise ValueError("Replace edit requires content; use op='delete' to remove text")
    return Edit(op=op, content=content, target=target)


class EvolutionStore:
    """SkillOpt + SkillDiff 驱动的技能演化存储与维护管理器 (Evolution Store)."""

    def __init__(
        self,
        skills,
        state_dir=None,
        session=None,
        *,
        lr_scheduler: str = "constant",
        max_lr: int = 4,
        min_lr: int = 2,
        total_steps: int = 10,
        gate_metric: str = "hard",
    ):
        self.skills = skills
        self.root = Path(state_dir) if state_dir else skills.root / ".weyra" / "skill-evolution"
        reject_symlinks(self.root)
        self.root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._async_locks = {}
        self.session = session
        self._session_events = {
            self._event_key(record.get("data", {}))
            for record in getattr(session, "records", ())
            if record.get("kind") == "skill_evolution"
        }
        self.legacy_roots = [] if state_dir else [skills.root / ".weyra" / "evolution"]
        self.recovery_results = []
        self.auxiliary_errors = []
        self.scheduler = build_scheduler(
            lr_scheduler, max_lr=max_lr, min_lr=min_lr, total_steps=total_steps
        )
        self.gate_metric = gate_metric
        self._step_counts: dict[str, int] = {}
        self.recover()

    def _key(self, name: str) -> str:
        try:
            path = self.skills.load(name).path
        except (KeyError, ValueError):
            path = self.skills.root / ".weyra" / "skills" / name / "SKILL.md"
        return sha(str(path.resolve()) + "\0" + name)

    def _dir(self, name: str) -> Path:
        p = self.root / self._key(name)
        reject_symlinks(p)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _event_key(event: dict) -> tuple:
        return (
            event.get("event_id"),
            event.get("status"),
            event.get("skill_path"),
            event.get("recovered", False),
        )

    def _mirror_event(self, event: dict) -> None:
        key = self._event_key(event)
        if self.session is not None and key not in self._session_events:
            self.session.append("skill_evolution", dict(event), checkpoint=True)
            self._session_events.add(key)

    def _record_in(self, folder: Path, event: dict) -> None:
        path = folder / "provenance.jsonl"
        reject_symlinks(path)
        separator = ""
        if path.exists() and path.stat().st_size:
            with path.open("rb") as previous:
                previous.seek(-1, os.SEEK_END)
                if previous.read(1) != b"\n":
                    separator = "\n"
        with path.open("a", encoding="utf-8") as output:
            output.write(separator + json.dumps(event, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        self._mirror_event(event)

    def _record(self, name: str, event: dict) -> None:
        self._record_in(self._dir(name), event)

    def _read_records(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        reject_symlinks(path)
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    records.append(event)
            except ValueError:
                self.auxiliary_errors.append(
                    "Incomplete skill evolution audit record: " + str(path)
                )
        return records

    def history(self, name: str) -> list[dict]:
        """聚合查询指定技能的演化审计历史轨迹。"""
        key = self._key(name)
        records = []
        for root in [*self.legacy_roots, self.root]:
            records.extend(self._read_records(root / key / "provenance.jsonl"))
        return records

    def recover(self) -> list[dict]:
        """启动阶段确定性崩溃恢复：绝不隐式发布未确认落盘的 `prepared` 提议。"""
        with self._lock:
            for root in [*self.legacy_roots, self.root]:
                if not root.exists():
                    continue
                try:
                    reject_symlinks(root)
                    paths = sorted(root.glob("*/provenance.jsonl"))
                except (OSError, ValueError) as error:
                    self.auxiliary_errors.append(str(error))
                    continue
                for path in paths:
                    try:
                        records = self._read_records(path)
                        for event in records:
                            try:
                                self._mirror_event(event)
                            except Exception as error:
                                self.auxiliary_errors.append(str(error))
                        terminal = {
                            event.get("event_id")
                            for event in records
                            if event.get("status")
                            in {"updated", "rejected", "discarded", "interrupted", "conflict"}
                        }
                        pending = {
                            event.get("event_id"): event
                            for event in records
                            if event.get("status") == "prepared"
                            and event.get("event_id") not in terminal
                        }
                        for event in pending.values():
                            result = self._recover_prepared(path.parent, event)
                            self.recovery_results.append(result)
                    except (OSError, ValueError, TypeError) as error:
                        self.auxiliary_errors.append(str(error))
        return list(self.recovery_results)

    def _recover_prepared(self, folder: Path, event: dict) -> dict:
        result = {**event, "recovered": True, "auxiliary_errors": []}
        try:
            recorded_path = event.get("skill_path")
            if not isinstance(recorded_path, str) or not Path(recorded_path).is_absolute():
                result.update(
                    status="interrupted",
                    reason="Prepared audit lacks an exact skill_path; publication was not inferred",
                )
            else:
                skill_path = Path(recorded_path)
                reject_symlinks(skill_path)
                allowed = [Path(directory).resolve() for directory in self.skills.skill_dirs]
                if skill_path.name != "SKILL.md" or not any(
                    skill_path.resolve().is_relative_to(directory) for directory in allowed
                ):
                    raise ValueError("Prepared skill path is outside configured skill directories")
                expected_key = sha(str(skill_path.resolve()) + "\0" + event.get("name", ""))
                if folder.name != expected_key:
                    raise ValueError("Prepared skill path does not match audit identity")
                current = skill_path.read_text(encoding="utf-8") if skill_path.exists() else None
                current_hash = sha(current) if current is not None else None
                if current_hash == event.get("content_hash") and current is not None:
                    version = event.get("version", "")
                    if len(version.split(".")) != 3 or not all(
                        part.isdigit() for part in version.split(".")
                    ):
                        raise ValueError("Prepared version is invalid")
                    snapshot = folder / (version + ".md")
                    if (
                        event.get("snapshot_path")
                        and Path(event["snapshot_path"]).resolve() != snapshot.resolve()
                    ):
                        raise ValueError("Prepared snapshot path does not match audit identity")
                    if event.get("snapshot_hash", current_hash) != current_hash:
                        raise ValueError("Prepared snapshot hash does not match published skill")
                    result["snapshot_path"] = str(snapshot.resolve())
                    result["status"] = "updated"
                    try:
                        atomic(snapshot, current)
                    except Exception as error:
                        result["status"] = "recovery_pending"
                        result["auxiliary_errors"].append(str(error))
                elif current is None or current_hash == event.get("base_hash"):
                    result.update(
                        status="interrupted",
                        reason="Prepared edit was not published; original file was preserved",
                    )
                else:
                    result.update(
                        status="conflict",
                        reason="Skill content changed after preparation; file was preserved",
                    )
        except (OSError, ValueError, TypeError) as error:
            result.update(status="conflict", reason="Prepared edit could not be safely recovered")
            result["auxiliary_errors"].append(str(error))
        try:
            self._record_in(folder, result)
        except Exception as error:
            result["auxiliary_errors"].append(str(error))
        return result

    def _completed(self, name: str, event_id: str) -> dict | None:
        return next(
            (
                dict(event)
                for event in reversed(self.history(name))
                if event.get("event_id") == event_id and event.get("status") == "updated"
            ),
            None,
        )

    def _reject(self, name: str, event_id: str, reason: str, record: bool = True) -> dict:
        result = {
            "status": "rejected",
            "event_id": event_id,
            "reason": reason,
            "failure_kind": "failure_to_apply",
        }
        try:
            if record:
                self._record(name, result)
        except Exception as error:
            result["auxiliary_errors"] = [str(error)]
        return result

    def rejected_edits(self, name: str, limit: int = 5) -> list[dict]:
        """读取 SkillOpt 被拒编辑缓冲 (`rejected_edits.jsonl`) 中的近期记录。"""
        folder = self._dir(name)
        path = folder / "rejected_edits.jsonl"
        if not path.exists():
            return []
        reject_symlinks(path)
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    records.append(item)
            except ValueError:
                self.auxiliary_errors.append("Incomplete rejected edit record: " + str(path))
        return records[-limit:] if limit > 0 else []

    def _save_rejected_edit(self, name: str, entry: dict) -> None:
        """将未通过门禁或违反 SkillDiff 保护的候选补丁写入 `rejected_edits.jsonl`。"""
        folder = self._dir(name)
        path = folder / "rejected_edits.jsonl"
        reject_symlinks(path)
        existing = self.rejected_edits(name, limit=MAX_REJECTED_EDITS)
        sanitized = {
            "event_id": str(entry.get("event_id", ""))[:64],
            "reason": str(entry.get("reason", ""))[:240],
            "gate_action": str(entry.get("gate_action", "reject"))[:32],
            "current_score": entry.get("current_score"),
            "candidate_score": entry.get("candidate_score"),
            "edits_summary": str(entry.get("edits_summary", ""))[:800],
            "failed_cases": list(entry.get("failed_cases", ()))[:8],
            "skill_version": str(entry.get("skill_version", "unknown"))[:32],
            "time": datetime.now(timezone.utc).isoformat(),
        }
        existing.append(sanitized)
        trimmed = existing[-MAX_REJECTED_EDITS:]
        payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in trimmed)
        atomic(path, payload)

    def rejected_edits_prompt(self, name: str, limit: int = 5) -> str:
        """格式化近期被拒编辑摘要，供 SkillOpt 优化器在后续迭代中避开已知失败方向。"""
        items = self.rejected_edits(name, limit=limit)
        if not items:
            return ""
        lines = []
        for item in items:
            cases = ",".join(str(c) for c in item.get("failed_cases", ())) or "n/a"
            score_info = ""
            if item.get("current_score") is not None and item.get("candidate_score") is not None:
                score_info = f"; score={item['current_score']}->{item['candidate_score']}"
            lines.append(
                f"[rejected v{item.get('skill_version', '?')}; reason={item.get('reason', '')}{score_info}; "
                f"cases={cases}] edits={item.get('edits_summary', '')}"
            )
        return "\n".join(lines)

    def current_edit_budget(self, name: str) -> int:
        """根据 SkillOpt `LRScheduler` 计算指定技能当前步的文本学习率（允许最大编辑条数）。"""
        step = self._step_counts.get(name, 0) + 1
        return self.scheduler.get_lr(step)

    @staticmethod
    def _violated_focus_items(snap, candidate_body: str) -> list[str]:
        """校验 SkillDiff 标注的 `**[FOCUS]**` 专家条目是否在候选正文中被删除或损坏。"""
        if not getattr(snap, "focus_items", ()):
            return []
        normalized_body = re.sub(r"\s+", " ", candidate_body.replace(MARK, ""))
        return [item for item in snap.focus_items if item not in normalized_body]

    def apply(
        self,
        name: str,
        edits,
        event_id=None,
        base_hash=None,
        description=None,
        when_to_use=None,
        reason: str = "",
        permission_mode: str = "default",
        authorized: bool = False,
        max_edits: int | None = None,
        _prepare_only: bool = False,
        _validation: str = "unvalidated",
        _expected_resources=None,
    ):
        """应用 SkillOpt 有界编辑补丁（`append`/`insert_after`/`replace`/`delete`）更新技能。

        安全不变量:
            1. 权限门槛：`acceptEdits` / `bypassPermissions` 或显式 `authorized=True`；
            2. 编辑预算：由 `SkillOpt` `LRScheduler`（或 `max_edits`）限制单步编辑操作数；
            3. `base_hash` OCC 冲突检测与资源防漂移校验；
            4. SkillDiff 护栏：未经外部验证通过（`_validation != "validated"`）时，禁止删改 `**[FOCUS]**` 专家条目。
        """
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            completed = self._completed(name, event_id)
            if completed:
                return completed
            try:
                snap = self.skills.load(name)
                if _expected_resources is not None and snap.resources != _expected_resources:
                    raise ValueError("Resource drift during validation")
                if base_hash != snap.content_hash:
                    raise ValueError("Base hash conflict")
                if not isinstance(edits, (list, tuple)) or not edits:
                    raise ValueError("At least one edit is required")
                budget = max_edits if max_edits is not None else self.current_edit_budget(name)
                if len(edits) > budget:
                    raise ValueError(f"Edit count {len(edits)} exceeds SkillOpt lr budget {budget}")
                normalized_edits = [_normalize_edit(e) for e in edits]
                body, reports = apply_patch_with_report(
                    snap.body, Patch(edits=normalized_edits, reasoning=reason)
                )
                if not any(r.get("status", "").startswith("applied_") for r in reports):
                    raise ValueError("No edit matched target or produced a change")
                if _validation != "validated":
                    violated = self._violated_focus_items(snap, body)
                    if violated:
                        reason_msg = f"Unvalidated edit alters SkillDiff [FOCUS] item: {violated[0][:80]}"
                        rejection = self._reject(name, event_id, reason_msg)
                        rejection["failure_kind"] = "focus_item_violation"
                        rejection["violated_focus_items"] = violated
                        self._save_rejected_edit(
                            name,
                            {
                                "event_id": event_id,
                                "reason": "focus_item_violation",
                                "edits_summary": json.dumps([e.to_dict() for e in normalized_edits], ensure_ascii=False)[:800],
                                "failed_cases": [],
                                "skill_version": snap.version,
                            },
                        )
                        return rejection
                res = self._publish(
                    snap,
                    body,
                    event_id,
                    description,
                    when_to_use,
                    reason,
                    _prepare_only,
                    _validation,
                )
                if not _prepare_only and isinstance(res, dict) and res.get("status") == "updated":
                    self._step_counts[name] = self._step_counts.get(name, 0) + 1
                    res["edit_reports"] = reports
                return res
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
                return self._reject(name, event_id, str(error))

    def apply_learned_edits(
        self,
        name: str,
        edit_records: Sequence[EditRecord | dict],
        *,
        event_id=None,
        base_hash=None,
        reason: str = "",
        permission_mode: str = "default",
        authorized: bool = False,
        _validation: str = "unvalidated",
    ):
        """使用 `SkillOpt-Sleep` (`skillopt_vendor/memory.py`) 维护受保护的 `LEARNED` 区块而不触碰手写正文。"""
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            try:
                snap = self.skills.load(name)
                if base_hash is not None and base_hash != snap.content_hash:
                    raise ValueError("Base hash conflict")
                records = [
                    r if isinstance(r, EditRecord) else EditRecord(**r)
                    for r in edit_records
                ]
                new_body, applied, unmatched = apply_edits_detailed(snap.body, records)
                if not applied:
                    raise ValueError("No learned edit applied")
                res = self._publish(
                    snap,
                    new_body,
                    event_id,
                    None,
                    None,
                    reason or "skillopt_sleep_learned_update",
                    False,
                    _validation,
                )
                if isinstance(res, dict):
                    res["applied_learned"] = len(applied)
                    res["unmatched_learned"] = len(unmatched)
                return res
            except (ValueError, KeyError, TypeError, OSError) as error:
                return self._reject(name, event_id, str(error))

    def annotate_skilldiff(
        self,
        name: str,
        coverage: dict[str, int],
        *,
        event_id=None,
        permission_mode: str = "default",
        authorized: bool = False,
    ):
        """使用 `SkillDiff` (`skilldiff_vendor/skilldiff.py`) 对先验零覆盖条款就地标注 `**[FOCUS]**`。"""
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            try:
                snap = self.skills.load(name)
                raw = snap.path.read_text(encoding="utf-8")
                items = split_items(raw)
                marked_raw = mark(raw, items, coverage)
                _, marked_body = parse_markdown(marked_raw)
                return self._publish(
                    snap,
                    marked_body,
                    event_id,
                    None,
                    None,
                    "skilldiff_focus_annotation",
                    False,
                    "skilldiff",
                )
            except (ValueError, KeyError, TypeError, OSError) as error:
                return self._reject(name, event_id, str(error))

    def _publish(
        self,
        snap,
        body: str,
        event_id: str,
        description=None,
        when_to_use=None,
        reason: str = "",
        prepare_only: bool = False,
        validation: str = "unvalidated",
    ):
        if not body.strip() or any(line.strip() == "---" for line in body.splitlines()):
            raise ValueError("Invalid body/frontmatter delimiter")
        description = snap.description if description is None else description
        when_to_use = snap.when_to_use if when_to_use is None else when_to_use
        if (
            body == snap.body
            and description == snap.description
            and when_to_use == snap.when_to_use
        ):
            raise ValueError("No effective change")
        if len(description) > 1024 or len(when_to_use) > 500:
            raise ValueError("Metadata exceeds character limit")
        parts = snap.version.split(".")
        if len(parts) != 3 or not all(x.isdigit() for x in parts):
            raise ValueError("Invalid semantic version")
        version = ".".join(parts[:2] + [str(int(parts[2]) + 1)])
        metadata = {
            **snap.metadata,
            "name": snap.name,
            "version": version,
            "description": description,
            "when_to_use": when_to_use,
        }
        if snap.context:
            metadata["context"] = snap.context
        raw = (
            "---\n"
            + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k, v in metadata.items())
            + "\n---\n"
            + body
        )
        for relative_path in snap.resources:
            self.skills.read_resource(snap, relative_path)
        if prepare_only:
            from .skills import extract_focus_items

            return replace(
                snap,
                body=body,
                version=version,
                description=description,
                when_to_use=when_to_use,
                metadata=MappingProxyType(metadata),
                content_hash=sha(raw),
                focus_items=extract_focus_items(raw),
            )
        folder = self._dir(snap.name)
        old = snap.path.read_text(encoding="utf-8")
        if sha(old) != snap.content_hash:
            raise ValueError("Concurrent modification")
        atomic(folder / (snap.version + ".md"), old)
        event = {
            "status": "prepared",
            "validation": validation,
            "name": snap.name,
            "event_id": event_id,
            "version": version,
            "base_hash": snap.content_hash,
            "base_snapshot_path": str((folder / (snap.version + ".md")).resolve()),
            "snapshot_path": str((folder / (version + ".md")).resolve()),
            "snapshot_hash": sha(raw),
            "skill_path": str(snap.path.resolve()),
            "content_hash": sha(raw),
            "reason": reason,
        }
        self._record(snap.name, event)
        if sha(snap.path.read_text(encoding="utf-8")) != snap.content_hash:
            raise ValueError("Concurrent modification")
        atomic(snap.path, raw)
        result = {**event, "status": "updated", "auxiliary_errors": []}
        try:
            atomic(folder / (version + ".md"), raw)
            self._record(snap.name, result)
        except Exception as error:
            result["auxiliary_errors"].append(str(error))
        return result

    def rollback(self, name: str, version: str, event_id=None, permission_mode: str = "default", authorized: bool = False):
        """基于历史归档快照将技能安全回滚至指定版本并递增 SemVer 补丁号。"""
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            completed = self._completed(name, event_id)
            if completed:
                return completed
            try:
                if not version or any(c not in "0123456789." for c in version):
                    raise ValueError("Invalid version")
                metadata, body = parse_markdown((self._dir(name) / (version + ".md")).read_text(encoding="utf-8"))
                return self._publish(
                    self.skills.load(name),
                    body,
                    event_id,
                    metadata.get("description", ""),
                    metadata.get("when_to_use", ""),
                    "rollback " + version,
                )
            except (ValueError, KeyError, OSError, RuntimeError) as error:
                return self._reject(name, event_id, str(error))

    def discard(self, name: str, event_id=None, reason: str = "") -> dict:
        """显式丢弃无依据或放弃的演化提议并写入审计轨迹。"""
        result = {"status": "discarded", "event_id": event_id or uuid.uuid4().hex, "reason": reason}
        try:
            self._record(name, result)
        except Exception as error:
            result["auxiliary_errors"] = [str(error)]
        return result

    def create(
        self,
        name: str,
        body: str,
        description: str = "",
        when_to_use: str = "",
        permission_mode: str = "default",
        authorized: bool = False,
        event_id=None,
        _validation: str = "unvalidated",
    ):
        """安全创建全新技能并写入 `1.0.0` 初始版本与不可变快照。"""
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            completed = self._completed(name, event_id)
            if completed:
                return completed
            if (
                not name
                or name in (".", "..")
                or "/" in name
                or "\\" in name
                or not body.strip()
                or any(x.strip() == "---" for x in body.splitlines())
                or len(description) > 1024
                or len(when_to_use) > 500
            ):
                return self._reject(name, event_id, "Invalid new skill")
            path = self.skills.root / ".weyra" / "skills" / name / "SKILL.md"
            try:
                reject_symlinks(path)
            except ValueError as error:
                return self._reject(name, event_id, str(error))
            if path.exists() or name in self.skills.capture_catalog():
                return self._reject(name, event_id, "Skill exists")
            raw = (
                "---\n"
                + "\n".join(
                    k + ": " + json.dumps(v, ensure_ascii=False)
                    for k, v in dict(
                        name=name, version="1.0.0", description=description, when_to_use=when_to_use
                    ).items()
                )
                + "\n---\n"
                + body
            )
            event = {
                "status": "prepared",
                "validation": _validation,
                "event_id": event_id,
                "name": name,
                "version": "1.0.0",
                "content_hash": sha(raw),
                "base_hash": None,
                "skill_path": str(path.resolve()),
                "snapshot_path": str((self._dir(name) / "1.0.0.md").resolve()),
                "snapshot_hash": sha(raw),
            }
            try:
                atomic(self._dir(name) / "1.0.0.md", raw)
                self._record(name, event)
                atomic(path, raw)
            except (OSError, ValueError, RuntimeError) as error:
                return self._reject(name, event_id, str(error))
            result = {**event, "status": "updated", "auxiliary_errors": []}
            try:
                self._record(name, result)
            except Exception as error:
                result["auxiliary_errors"].append(str(error))
            return result

    async def maintain(
        self,
        window,
        model_call,
        permission_mode: str = "default",
        authorized: bool = False,
        event_id=None,
        evidence=None,
        target_snapshot=None,
        validation_gate=None,
        resolution=None,
        run_context=None,
    ):
        """SkillOpt 闭环维护：固化证据 -> 生成有界编辑提议 -> `SkillValidationGate` 评测 -> 门控合入或拒绝记录。"""
        event_id = event_id or uuid.uuid4().hex
        if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
            return {
                "status": "rejected",
                "event_id": event_id,
                "reason": "Permission required",
                "failure_kind": "failure_to_apply",
            }
        catalog = self.skills.capture_catalog() if target_snapshot is None else None
        frozen_window = json.loads(json.dumps(window, ensure_ascii=False))
        frozen_evidence = json.loads(
            json.dumps(evidence if evidence is not None else window, ensure_ascii=False)
        )
        source_record = {
            "event_id": event_id,
            "resolution": resolution,
            "evidence": frozen_evidence,
            "target": None
            if target_snapshot is None
            else {
                "name": target_snapshot.name,
                "path": str(target_snapshot.path),
                "version": target_snapshot.version,
                "content_hash": target_snapshot.content_hash,
            },
        }
        evidence_path = self.root / "evidence.jsonl"
        try:
            reject_symlinks(evidence_path)
            with self._lock, evidence_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(source_record, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
        except (OSError, ValueError) as error:
            return {
                "status": "rejected",
                "event_id": event_id,
                "reason": str(error),
                "failure_kind": "failure_to_apply",
            }

        proposal = await model_call(frozen_window)
        if not proposal:
            proposal = {"action": "discard"}
        proposal_text = json.dumps(
            {"event_id": event_id, "proposal": proposal, "target": source_record["target"]},
            ensure_ascii=False,
            sort_keys=True,
        )
        proposal_path = self.root / "proposals" / (sha(str(event_id)) + "-" + sha(proposal_text) + ".json")
        atomic(proposal_path, proposal_text)
        if self.session is not None:
            self.session.append(
                "skill_proposal",
                {
                    "event_id": event_id,
                    "proposal_ref": str(proposal_path),
                    "proposal_hash": sha(proposal_text),
                },
                checkpoint=True,
            )
        if not isinstance(proposal, dict):
            return self._reject("_maintenance", event_id, "Invalid model proposal")
        proposal = dict(proposal)
        action = proposal.pop("action", "apply")
        name = proposal.pop("name", None)
        for reserved in ("event_id", "authorized", "permission_mode", "base_hash", "content_hash", "_prepare_only", "_validation", "_expected_resources"):
            proposal.pop(reserved, None)
        reason = proposal.pop("reasoning", proposal.pop("reason", ""))
        if action == "discard":
            return self.discard(name or "_maintenance", event_id=event_id, reason=reason)
        if not isinstance(name, str) or not name:
            return self._reject("_maintenance", event_id, "A skill name is required")
        if resolution is not None and resolution.get("reason") == "ambiguous":
            return self._reject(name, event_id, "Ambiguous skill target; abstaining")
        if action == "apply":
            if target_snapshot is not None and name != target_snapshot.name:
                return self._reject(name, event_id, "Proposal target differs from pinned skill")
            target = target_snapshot if target_snapshot is not None else catalog.get(name)
            if target is None:
                return self._reject(name, event_id, "No program-pinned target")
            proposal["base_hash"] = target.content_hash
            proposal["reason"] = reason
            name = target.name
        elif action == "create":
            if name in self.skills.capture_catalog():
                return self._reject(name, event_id, "Create requires a new skill")
        else:
            return self._reject(name, event_id, "Unknown maintenance action")
        proposal.update(name=name, event_id=event_id, permission_mode=permission_mode, authorized=authorized)

        async with self._async_locks.setdefault(name, asyncio.Lock()):
            try:
                if action == "create":
                    if validation_gate is None:
                        return self.create(**proposal)
                    if not getattr(validation_gate, "supports_null_baseline", False):
                        return self._reject(name, event_id, "Creation requires a baseline-capable validation gate")
                    description_val = proposal.get("description", "")
                    when_val = proposal.get("when_to_use", "")
                    body_val = proposal.get("body", "")
                    if (
                        not name
                        or name in (".", "..")
                        or "/" in name
                        or "\\" in name
                        or not isinstance(body_val, str)
                        or not body_val.strip()
                        or any(x.strip() == "---" for x in body_val.splitlines())
                        or not isinstance(description_val, str)
                        or len(description_val) > 1024
                        or not isinstance(when_val, str)
                        or len(when_val) > 500
                    ):
                        return self._reject(name, event_id, "Invalid new skill")
                    from .skills import SkillSnapshot, extract_focus_items

                    skill_path = (self.skills.root / ".weyra" / "skills" / name / "SKILL.md").resolve()
                    raw_new = (
                        "---\n"
                        + "\n".join(
                            k + ": " + json.dumps(v, ensure_ascii=False)
                            for k, v in dict(
                                name=name,
                                version="1.0.0",
                                description=description_val,
                                when_to_use=when_val,
                            ).items()
                        )
                        + "\n---\n"
                        + body_val
                    )
                    null_baseline = SkillSnapshot(
                        name=name,
                        path=skill_path,
                        version="0.0.0",
                        description="",
                        when_to_use="",
                        body="",
                        content_hash=sha(""),
                        resources=MappingProxyType({}),
                        metadata=MappingProxyType({"__null_baseline__": True}),
                    )
                    candidate = SkillSnapshot(
                        name=name,
                        path=skill_path,
                        version="1.0.0",
                        description=description_val,
                        when_to_use=when_val,
                        body=body_val,
                        content_hash=sha(raw_new),
                        resources=MappingProxyType({}),
                        focus_items=extract_focus_items(raw_new),
                    )
                    scores, report_path = await self._run_gate(
                        validation_gate, null_baseline, candidate, event_id, run_context
                    )
                    if not scores.get("approved"):
                        reason_rej = scores.get("reason", "Validation gate rejected new skill")
                        res = self._reject(name, event_id, reason_rej)
                        res.update(validation_report=scores, validation_report_ref=str(report_path))
                        return res
                    res = self.create(**proposal, _validation="validated")
                    res.update(validation_report=scores, validation_report_ref=str(report_path))
                    return res

                # action == "apply"
                if validation_gate is None:
                    return self.apply(**proposal)
                candidate = self.apply(**proposal, _prepare_only=True, _expected_resources=target.resources)
                if isinstance(candidate, dict):
                    return candidate
                try:
                    scores, report_path = await self._run_gate(
                        validation_gate, target, candidate, event_id, run_context
                    )
                except BudgetExceeded:
                    raise
                except Exception as error:
                    return self._reject(name, event_id, "Validation failed: " + type(error).__name__)
                if not scores.get("approved"):
                    reason_rej = scores.get("reason", "Validation gate rejected candidate")
                    failed_cases = [
                        row.get("id")
                        for row in scores.get("per_case", ())
                        if isinstance(row, dict)
                        and not row.get("candidate", {}).get("passed", True)
                    ]
                    self._save_rejected_edit(
                        name,
                        {
                            "event_id": event_id,
                            "reason": reason_rej,
                            "gate_action": scores.get("gate_action", "reject"),
                            "current_score": scores.get("old", {}).get("score"),
                            "candidate_score": scores.get("candidate", {}).get("score"),
                            "edits_summary": json.dumps(proposal.get("edits", []), ensure_ascii=False)[:800],
                            "failed_cases": failed_cases,
                            "skill_version": target.version,
                        },
                    )
                    res = self._reject(name, event_id, reason_rej)
                    res.update(validation_report=scores, validation_report_ref=str(report_path))
                    return res
                if self.skills.load(name).resources != target.resources:
                    return self._reject(name, event_id, "Resource drift during validation")
                res = self.apply(**proposal, _validation="validated", _expected_resources=target.resources)
                res.update(validation_report=scores, validation_report_ref=str(report_path))
                return res
            except BudgetExceeded:
                raise
            except (TypeError, ValueError) as error:
                return self._reject(name, event_id, str(error))

    async def _run_gate(self, gate, baseline, candidate, event_id: str, run_context):
        if hasattr(gate, "evaluate"):
            scores = await gate.evaluate(baseline, candidate, context=run_context)
        else:
            scores = await gate(baseline, candidate)
        report_path = self.root / "validation" / (sha(str(event_id)) + ".json")
        atomic(report_path, json.dumps(scores, ensure_ascii=False))
        if self.session is not None:
            self.session.append(
                "skill_validation",
                {"event_id": event_id, "report_ref": str(report_path)},
                checkpoint=True,
            )
        return scores, report_path

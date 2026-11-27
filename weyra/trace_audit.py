"""零 API 消耗的 SQLite 只读会话轨迹、技能采用与运行时可靠性审计器 (Zero-API Trace Auditor).

【架构定位】
直接只读解析 Weyra 会话事件流（`SessionStore` SQLite `state.db?mode=ro` 或内存事件序列），
在不发起任何大模型调用的前提下输出客观可核查的遥测统计：
1. `ExecutionStatsReport`：轮次完成数、工具调用次数/报错数/连续重复重试、编辑后校验行为与未验证声明计数；
2. `SkillAdoptionReport`：从 `<available_skills>` 目录披露、`skill` 工具激活及用户显式 `/<skill-name>`
   `skill_invocation` 记录中统计技能暴露与激活事实；
3. `HarnessReliabilityReport`：核查未闭合工具意图、崩溃恢复状态、未知费用结算、资源清理异常与演化冲突。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence

_AVAILABLE_SKILL_LINE_RE = re.compile(r"^-\s+`([^`]+)`\s*:")


@dataclass(frozen=True)
class ExecutionStatsReport:
    """单会话客观执行过程统计摘要。"""

    turns: int
    completed_turns: int
    tool_calls: int
    tool_errors: int
    redundant_calls: int
    error_loops: int
    edits: int
    verify_after_edit: bool
    unverified_claim: bool
    completed: bool


@dataclass(frozen=True)
class SkillAdoptionReport:
    """技能目录暴露与实际激活情况统计（解析标准 `<available_skills>` 与 `skill` 工具调用）。"""

    exposed: tuple[str, ...]
    retrieved: tuple[str, ...]
    activated: tuple[str, ...]
    user_invoked: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessReliabilityReport:
    """基于不可变因果事件流的 Harness 运行时可靠性核验报告。"""

    unclosed_tool_intents: int
    recovered_interrupted: int
    budget_unknown_calls: int
    permission_denials: int
    cleanup_errors: int
    evolution_statuses: dict[str, int] = field(default_factory=dict)
    reliable: bool = True


@dataclass(frozen=True)
class TraceAuditReport:
    """聚合执行统计、技能采用与 Harness 可靠性的只读审计报告。"""

    session_id: str | None
    process: ExecutionStatsReport
    skill_funnel: SkillAdoptionReport
    reliability: HarnessReliabilityReport

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_skill_names_from_catalog(content: str) -> list[str]:
    names: list[str] = []
    for line in content.splitlines():
        match = _AVAILABLE_SKILL_LINE_RE.match(line.strip())
        if match:
            names.append(match.group(1))
    return names


def audit_records(
    records: Sequence[Mapping[str, Any]],
    *,
    session_id: str | None = None,
    task_passed: bool | None = None,
) -> TraceAuditReport:
    """基于会话事件序列执行纯函数式、零 API 消耗的只读审计。"""
    turns = 0
    completed_turns = 0
    tool_calls = 0
    tool_errors = 0
    redundant_calls = 0
    error_loops = 0
    edits = 0
    verify_after_edit = True
    seen_edit = False
    verified_since_last_edit = False
    unverified_claim = False

    pending_intents: set[str] = set()
    recovered_interrupted = 0
    budget_unknown_calls = 0
    permission_denials = 0
    cleanup_errors = 0
    evolution_statuses: dict[str, int] = {}

    exposed: list[str] = []
    retrieved: list[str] = []
    activated: list[str] = []
    user_invoked: list[str] = []

    last_call_sig: tuple[str, str] | None = None
    last_failed_sig: tuple[str, str] | None = None
    intents_by_call_id: dict[str, str] = {}

    for record in records:
        kind = record.get("kind")
        data = record.get("data") or {}
        if not isinstance(data, dict):
            continue

        if kind == "turn_start":
            turns += 1
        elif kind == "turn_end":
            if data.get("state") == "completed":
                completed_turns += 1
            if data.get("reason") == "process_interrupted":
                recovered_interrupted += 1
        elif kind == "tool_intent":
            call_id = data.get("call_id")
            name = str(data.get("name", ""))
            args_sig = json.dumps(data.get("arguments", {}), sort_keys=True, ensure_ascii=False)
            if isinstance(call_id, str) and call_id:
                pending_intents.add(call_id)
                intents_by_call_id[call_id] = args_sig
            if last_call_sig == (name, args_sig):
                redundant_calls += 1
            last_call_sig = (name, args_sig)
        elif kind == "tool_result":
            call_id = data.get("call_id")
            if isinstance(call_id, str):
                pending_intents.discard(call_id)
            name = str(data.get("name", ""))
            status = data.get("status", "success")
            tool_calls += 1

            args_sig = intents_by_call_id.get(call_id, "") if isinstance(call_id, str) else ""
            if status in {"error", "denied", "invalid", "cancelled", "unknown"}:
                tool_errors += 1
                if status == "denied":
                    permission_denials += 1
                if last_failed_sig == (name, args_sig):
                    error_loops += 1
                last_failed_sig = (name, args_sig)
            else:
                last_failed_sig = None

            if name in {"write_file", "edit_file", "apply_patch"} and status == "success":
                edits += 1
                seen_edit = True
                verified_since_last_edit = False
            elif seen_edit and status == "success" and name in {"read_file", "run_shell", "grep_search"}:
                verified_since_last_edit = True

            if name == "skill" and status == "success":
                structured = data.get("structured")
                if isinstance(structured, dict) and isinstance(structured.get("name"), str):
                    if structured["name"] not in activated:
                        activated.append(structured["name"])
        elif kind == "message":
            role = data.get("role")
            source = data.get("source")
            content = str(data.get("content", ""))
            if source == "recovery":
                recovered_interrupted += 1
            elif source == "skills":
                try:
                    start = content.index("{")
                    envelope = json.loads(content[start:])
                    for name in _extract_skill_names_from_catalog(str(envelope.get("content", ""))):
                        if name not in exposed:
                            exposed.append(name)
                except (ValueError, TypeError):
                    pass
            elif source == "skill_catalog":
                for name in _extract_skill_names_from_catalog(content):
                    if name not in retrieved:
                        retrieved.append(name)
            elif source == "skill_invocation":
                sname = (data.get("provider_data") or {}).get("skill_name")
                if isinstance(sname, str):
                    if sname not in user_invoked:
                        user_invoked.append(sname)
                    if sname not in activated:
                        activated.append(sname)
            elif role == "assistant" and not data.get("tool_calls"):
                lowered = content.casefold()
                claim_cues = ("passed", "verified", "all tests", "测试通过", "已验证")
                if seen_edit and not verified_since_last_edit and any(c in lowered for c in claim_cues):
                    unverified_claim = True
        elif kind == "budget_settlement":
            if data.get("tokens") is None:
                budget_unknown_calls += 1
        elif kind == "cleanup_errors":
            cleanup_errors += len(data.get("resources", ()) or ())
        elif kind in {"evolution_result", "skill_evolution"}:
            status = str(data.get("status", "unknown"))
            evolution_statuses[status] = evolution_statuses.get(status, 0) + 1

    if seen_edit:
        verify_after_edit = verified_since_last_edit

    completed = (completed_turns > 0) if task_passed is None else bool(task_passed)
    reliable = (
        len(pending_intents) == 0
        and cleanup_errors == 0
        and evolution_statuses.get("conflict", 0) == 0
    )

    return TraceAuditReport(
        session_id=session_id,
        process=ExecutionStatsReport(
            turns=turns,
            completed_turns=completed_turns,
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            redundant_calls=redundant_calls,
            error_loops=error_loops,
            edits=edits,
            verify_after_edit=verify_after_edit,
            unverified_claim=unverified_claim,
            completed=completed,
        ),
        skill_funnel=SkillAdoptionReport(
            exposed=tuple(exposed),
            retrieved=tuple(retrieved),
            activated=tuple(activated),
            user_invoked=tuple(user_invoked),
        ),
        reliability=HarnessReliabilityReport(
            unclosed_tool_intents=len(pending_intents),
            recovered_interrupted=recovered_interrupted,
            budget_unknown_calls=budget_unknown_calls,
            permission_denials=permission_denials,
            cleanup_errors=cleanup_errors,
            evolution_statuses=evolution_statuses,
            reliable=reliable,
        ),
    )


def audit_session_db(
    project_or_db: str | Path,
    session_id: str | None = None,
    *,
    task_passed: bool | None = None,
) -> TraceAuditReport:
    """以 SQLite 只读模式 (`?mode=ro`) 审计指定项目或数据库文件中的会话轨迹。"""
    target = Path(project_or_db).resolve()
    db_path = target if target.is_file() else target / ".weyra" / "state.db"
    if not db_path.is_file():
        raise FileNotFoundError(f"Session database not found: {db_path}")
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        if session_id is None:
            row = db.execute(
                "SELECT session_id FROM sessions ORDER BY updated DESC, rowid DESC LIMIT 1"
            ).fetchone()
            if not row:
                raise ValueError("No sessions found in database")
            session_id = row[0]
        rows = db.execute(
            "SELECT id, parent, kind, data FROM events WHERE session_id=? ORDER BY seq",
            (session_id,),
        ).fetchall()
    records = [
        {"id": rid, "parent": parent, "kind": kind, "data": json.loads(raw)}
        for rid, parent, kind, raw in rows
    ]
    return audit_records(records, session_id=session_id, task_passed=task_passed)

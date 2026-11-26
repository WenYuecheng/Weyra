"""零 API 消耗的执行轨迹过程质量、技能漏斗与 Harness 可靠性审计器 (Zero-API Trace & Reliability Auditor)。

【架构定位】
直接只读解析 Weyra 会话事件流（`SessionStore` SQLite `state.db?mode=ro` 或内存事件序列），
在不发起任何外部大模型请求的前提下，从三个正交维度输出结构化诊断报告：
1. 过程质量分档 (对标 NeurIPS 2026 AgentLens / Lucky Pass 检测):
   区分理想路径 (`ideal`)、稳健完成 (`solid`)、盲试幸运通过 (`lucky_pass`) 与失败 (`failed`)；
2. 技能四段转化漏斗 (对标 EMNLP 2026 Harness Updating != Benefit & arXiv:2611.25429 SkillPlumber):
   度量 `exposed -> retrieved -> activated -> rule_coverage` 转化率，分离检索命中与实际遵循；
3. Harness 可靠性审计 (对标 arXiv:2610.32285 Trial by Trace):
   核查未闭合工具意图、崩溃恢复状态、预算挂起/未知调用、资源清理异常与权限拒绝记录。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ProcessQualityReport:
    """单会话执行过程质量指标与 NeurIPS 2026 AgentLens (arXiv:2605.12925) 分档结论。"""

    turns: int
    completed_turns: int
    tool_calls: int
    tool_errors: int
    redundant_calls: int
    error_loops: int
    edits: int
    verify_after_edit: bool
    unverified_claim: bool
    tier: str


@dataclass(frozen=True)
class SkillFunnelReport:
    """技能暴露、检索召回、激活加载与条款覆盖四段漏斗报告。

    对标 EMNLP 2026 Main *Harness Updating Is Not Harness Benefit* (2026.emnlp-main.2326)
    与 *SkillPlumber* (arXiv:2611.25429, Nov 2026) 的指令级覆盖率分析。
    """

    exposed: tuple[str, ...]
    retrieved: tuple[str, ...]
    activated: tuple[str, ...]
    rule_coverage: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    coverage_rate: float | None = None


@dataclass(frozen=True)
class HarnessReliabilityReport:
    """基于不可变因果事件流的 Harness 运行时可靠性核验报告 (对标 Trial by Trace, arXiv:2610.32285)。"""

    unclosed_tool_intents: int
    recovered_interrupted: int
    budget_unknown_calls: int
    permission_denials: int
    cleanup_errors: int
    evolution_statuses: dict[str, int] = field(default_factory=dict)
    reliable: bool = True


@dataclass(frozen=True)
class TraceAuditReport:
    """聚合过程质量、技能转化漏斗与 Harness 可靠性的完整审计快照。"""

    session_id: str | None
    process: ProcessQualityReport
    skill_funnel: SkillFunnelReport
    reliability: HarnessReliabilityReport

    def to_dict(self) -> dict[str, Any]:
        """将审计报告转换为可 JSON 序列化的纯字典结构。"""
        return asdict(self)


def _extract_skill_names_from_snapshot(content: str) -> list[str]:
    names = []
    for line in content.splitlines()[1:]:
        head = line.split(":", 1)[0].strip()
        if head:
            names.append(head)
    return names


def _extract_retrieved_skills(content: str) -> list[str]:
    names = []
    for line in content.splitlines()[1:]:
        prefix = line.split(":", 1)[0].strip()
        if "[" in prefix:
            prefix = prefix.split("[", 1)[0].strip()
        if prefix:
            names.append(prefix)
    return names


def audit_records(
    records: Sequence[Mapping[str, Any]],
    *,
    session_id: str | None = None,
    skill_rules: Mapping[str, Sequence[str]] | None = None,
    task_passed: bool | None = None,
) -> TraceAuditReport:
    """基于会话事件序列执行纯函数式、零 API 消耗的过程与可靠性审计。

    Args:
        records: 按因果序排列的会话事件字典序列。
        session_id: 可选的会话标识。
        skill_rules: 可选的 `{skill_name: [clause_or_keyword, ...]}` 映射，用于估算条款覆盖率。
        task_passed: 可选的外部客观判分布尔值；若未提供则以是否存在已完成轮次近似。

    Returns:
        TraceAuditReport: 包含过程质量、技能漏斗与 Harness 可靠性的完整审计结果。
    """
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

    last_call_sig: tuple[str, str] | None = None
    last_failed_sig: tuple[str, str] | None = None
    intents_by_call_id: dict[str, str] = {}
    observed_corpus_parts: list[str] = []

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
            observed_corpus_parts.append(f"{name} {status} {data.get('content', '')}")

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
                    for name in _extract_skill_names_from_snapshot(str(envelope.get("content", ""))):
                        if name not in exposed:
                            exposed.append(name)
                except (ValueError, TypeError):
                    pass
            elif source == "skill_catalog":
                for name in _extract_retrieved_skills(content):
                    if name not in retrieved:
                        retrieved.append(name)
            elif role == "assistant" and not data.get("tool_calls"):
                observed_corpus_parts.append(content)
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

    passed = (completed_turns > 0) if task_passed is None else bool(task_passed)
    if not passed:
        tier = "failed"
    elif (
        error_loops > 0
        or redundant_calls >= 2
        or tool_errors >= 2
        or unverified_claim
        or (seen_edit and not verify_after_edit)
    ):
        tier = "lucky_pass"
    elif tool_errors == 0 and redundant_calls == 0:
        tier = "ideal"
    else:
        tier = "solid"

    corpus = "\n".join(observed_corpus_parts).casefold()
    rule_coverage: dict[str, dict[str, list[str]]] = {}
    total_rules = 0
    followed_rules = 0
    if skill_rules:
        for skill_name, clauses in skill_rules.items():
            followed: list[str] = []
            uncovered: list[str] = []
            for clause in clauses:
                total_rules += 1
                token = str(clause).strip()
                if token and token.casefold() in corpus:
                    followed.append(token)
                    followed_rules += 1
                else:
                    uncovered.append(token)
            rule_coverage[skill_name] = {"followed": followed, "uncovered": uncovered}
    coverage_rate = (followed_rules / total_rules) if total_rules else None

    reliable = (
        len(pending_intents) == 0
        and cleanup_errors == 0
        and evolution_statuses.get("conflict", 0) == 0
    )

    return TraceAuditReport(
        session_id=session_id,
        process=ProcessQualityReport(
            turns=turns,
            completed_turns=completed_turns,
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            redundant_calls=redundant_calls,
            error_loops=error_loops,
            edits=edits,
            verify_after_edit=verify_after_edit,
            unverified_claim=unverified_claim,
            tier=tier,
        ),
        skill_funnel=SkillFunnelReport(
            exposed=tuple(exposed),
            retrieved=tuple(retrieved),
            activated=tuple(activated),
            rule_coverage=rule_coverage,
            coverage_rate=coverage_rate,
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
    skill_rules: Mapping[str, Sequence[str]] | None = None,
    task_passed: bool | None = None,
) -> TraceAuditReport:
    """以 SQLite 只读模式 (`?mode=ro`) 审计指定项目或数据库文件中的会话轨迹。

    Args:
        project_or_db: 项目根目录或 `.weyra/state.db` 文件路径。
        session_id: 可选的目标会话 ID；缺省时选取最近更新的会话。
        skill_rules: 可选的技能条款映射，用于计算指令覆盖率。
        task_passed: 可选的外部任务通过判定。

    Returns:
        TraceAuditReport: 会话级多维度审计报告。

    Raises:
        FileNotFoundError: 数据库文件不存在。
        ValueError: 数据库中不存在目标会话。
    """
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
    return audit_records(
        records, session_id=session_id, skill_rules=skill_rules, task_passed=task_passed
    )

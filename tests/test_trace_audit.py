"""Zero-API tests for Weyra's read-only Trace & Reliability Auditor (weyra/trace_audit.py)."""

from __future__ import annotations

import sqlite3

import pytest

from weyra.session import SessionStore
from weyra.trace_audit import audit_records, audit_session_db


def test_audit_records_distinguishes_ideal_solid_lucky_pass_and_failed():
    """AgentLens (arXiv:2605.12925) & Harness Updating != Benefit: 4-tier classification & funnel."""
    ideal = audit_records(
        [
            {"kind": "turn_start", "data": {"turn_id": "t1"}},
            {"kind": "message", "data": {"role": "user", "source": "skills", "content": 'ctx\n{"content":"Skill catalog snapshot:\\ncode-hygiene: clean [1.0.0]"}'}},
            {"kind": "message", "data": {"role": "user", "source": "skill_catalog", "content": "Relevant available skills:\ncode-hygiene [1.0.0]: clean"}},
            {"kind": "tool_intent", "data": {"call_id": "c1", "name": "skill", "arguments": {"name": "code-hygiene"}}},
            {"kind": "tool_result", "data": {"call_id": "c1", "name": "skill", "status": "success", "content": "ok", "structured": {"name": "code-hygiene"}}},
            {"kind": "tool_intent", "data": {"call_id": "c2", "name": "edit_file", "arguments": {"path": "x.py"}}},
            {"kind": "tool_result", "data": {"call_id": "c2", "name": "edit_file", "status": "success", "content": "edited"}},
            {"kind": "tool_intent", "data": {"call_id": "c3", "name": "read_file", "arguments": {"path": "x.py"}}},
            {"kind": "tool_result", "data": {"call_id": "c3", "name": "read_file", "status": "success", "content": "verified x.py"}},
            {"kind": "message", "data": {"role": "assistant", "content": "Verified edit on x.py."}},
            {"kind": "turn_end", "data": {"turn_id": "t1", "state": "completed"}},
        ],
        skill_rules={"code-hygiene": ["edit_file", "read_file", "missing_clause"]},
        task_passed=True,
    )
    assert ideal.process.tier == "ideal"
    assert ideal.process.verify_after_edit is True
    assert ideal.process.unverified_claim is False
    assert ideal.skill_funnel.exposed == ("code-hygiene",)
    assert ideal.skill_funnel.retrieved == ("code-hygiene",)
    assert ideal.skill_funnel.activated == ("code-hygiene",)
    assert ideal.skill_funnel.rule_coverage["code-hygiene"]["followed"] == ["edit_file", "read_file"]
    assert ideal.skill_funnel.rule_coverage["code-hygiene"]["uncovered"] == ["missing_clause"]
    assert ideal.skill_funnel.coverage_rate == pytest.approx(2 / 3)
    assert ideal.reliability.reliable is True

    solid = audit_records(
        [
            {"kind": "turn_start", "data": {"turn_id": "t1"}},
            {"kind": "tool_intent", "data": {"call_id": "c1", "name": "read_file", "arguments": {"path": "a"}}},
            {"kind": "tool_result", "data": {"call_id": "c1", "name": "read_file", "status": "error", "content": "enoent"}},
            {"kind": "tool_intent", "data": {"call_id": "c2", "name": "read_file", "arguments": {"path": "b"}}},
            {"kind": "tool_result", "data": {"call_id": "c2", "name": "read_file", "status": "success", "content": "ok"}},
            {"kind": "message", "data": {"role": "assistant", "content": "Found b."}},
            {"kind": "turn_end", "data": {"turn_id": "t1", "state": "completed"}},
        ],
        task_passed=True,
    )
    assert solid.process.tier == "solid"

    lucky = audit_records(
        [
            {"kind": "turn_start", "data": {"turn_id": "t1"}},
            {"kind": "tool_intent", "data": {"call_id": "c1", "name": "edit_file", "arguments": {"path": "a"}}},
            {"kind": "tool_result", "data": {"call_id": "c1", "name": "edit_file", "status": "error", "content": "bad"}},
            {"kind": "tool_intent", "data": {"call_id": "c2", "name": "edit_file", "arguments": {"path": "a"}}},
            {"kind": "tool_result", "data": {"call_id": "c2", "name": "edit_file", "status": "success", "content": "ok"}},
            {"kind": "message", "data": {"role": "assistant", "content": "All tests passed."}},
            {"kind": "turn_end", "data": {"turn_id": "t1", "state": "completed"}},
        ],
        task_passed=True,
    )
    assert lucky.process.tier == "lucky_pass"
    assert lucky.process.verify_after_edit is False
    assert lucky.process.unverified_claim is True

    failed = audit_records([], task_passed=False)
    assert failed.process.tier == "failed"


def test_audit_records_reliability_flags_unclosed_cleanup_and_conflict():
    """Trial by Trace (arXiv:2610.32285): detects unclosed intents, denials, unknown costs, conflicts."""
    rep = audit_records(
        [
            {"kind": "tool_intent", "data": {"call_id": "dangling", "name": "write_file", "arguments": {}}},
            {"kind": "tool_result", "data": {"call_id": "denied-1", "name": "run_shell", "status": "denied", "content": ""}},
            {"kind": "budget_settlement", "data": {"tokens": None}},
            {"kind": "cleanup_errors", "data": {"resources": ["proc-1", "lock-2"]}},
            {"kind": "evolution_result", "data": {"status": "conflict"}},
            {"kind": "turn_end", "data": {"state": "completed", "reason": "process_interrupted"}},
        ],
        task_passed=False,
    )
    assert rep.reliability.unclosed_tool_intents == 1
    assert rep.reliability.permission_denials == 1
    assert rep.reliability.budget_unknown_calls == 1
    assert rep.reliability.cleanup_errors == 2
    assert rep.reliability.recovered_interrupted == 1
    assert rep.reliability.evolution_statuses == {"conflict": 1}
    assert rep.reliability.reliable is False


def test_audit_session_db_readonly_and_errors(tmp_path):
    """Verify `?mode=ro` read-only SQLite access and error handling."""
    with pytest.raises(FileNotFoundError):
        audit_session_db(tmp_path / "missing-project")

    empty_dir = tmp_path / "empty"
    s_empty = SessionStore(project=empty_dir, session_id="to-delete")
    s_empty.close()
    with sqlite3.connect(empty_dir / ".weyra" / "state.db") as conn:
        conn.execute("DELETE FROM sessions")
        conn.commit()
    with pytest.raises(ValueError, match="No sessions"):
        audit_session_db(empty_dir)

    proj = tmp_path / "valid"
    s = SessionStore(project=proj, session_id="s-audit")
    s.append("turn_start", {"turn_id": "t1"}, True)
    s.append("turn_end", {"turn_id": "t1", "state": "completed"}, True)
    s.close()

    db_path = proj / ".weyra" / "state.db"
    before_bytes = db_path.read_bytes()
    rep = audit_session_db(proj)
    assert rep.session_id == "s-audit"
    assert rep.process.tier == "ideal"
    assert db_path.read_bytes() == before_bytes

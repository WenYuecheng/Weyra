"""Zero-API regression tests for Weyra's 2026 paper-aligned skill evolution stack."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from types import MappingProxyType

from weyra.agent import Agent
from weyra.contracts import ModelInfo, ModelResponse, ToolCall, Usage
from weyra.core.state import Budget
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.session import SessionStore
from weyra.skill_evolution import (
    EvolutionStore,
    KNOWLEDGE_KINDS,
    KNOWLEDGE_QUOTAS,
    MAX_NOTES_PER_KIND,
    MAX_REJECTED_EDITS,
)
from weyra.skill_judge import ConfiguredSkillGate, ReviewedCallbackGate
from weyra.skill_replay_gate import (
    _can_short_circuit_judge,
    _deterministic_judgment,
    _is_deterministic_case,
)
from weyra.skills import SkillStore, _parse_anchors


def _write_skill(root, name: str, *, description: str, when_to_use: str = "", aliases=(), body: str, anchors=()):
    skill_dir = root / ".weyra" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": name,
        "version": "1.0.0",
        "description": description,
        "when_to_use": when_to_use,
    }
    if aliases:
        meta["aliases"] = list(aliases)
    if anchors:
        meta["anchors"] = list(anchors)
    header = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()) + "\n---\n"
    (skill_dir / "SKILL.md").write_text(header + body, encoding="utf-8")


def _offline_runtime(main_responses=(), judge_responses=()):
    registry = ProviderRegistry()
    registry.register("offline", ScriptedProvider(list(main_responses)))
    registry.register("judge_offline", ScriptedProvider(list(judge_responses)))
    runtime = ModelRuntime(
        registry,
        {
            "main": ModelInfo("offline", "scripted-main", 200000, 1024),
            "skill_judge": ModelInfo("judge_offline", "scripted-judge", 200000, 512, supports_tools=False),
        },
    )
    runtime.context_policy = {"headroom_tokens": 1024, "max_tokens": 1024, "retain_tokens": 256}
    return runtime


class _FailIfCalledJudge(ScriptedProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def stream(self, request, context):
        self.calls += 1
        raise AssertionError("Judge LLM should not be called on deterministic cases")
        yield  # pragma: no cover


def test_skill_pro_activation_first_bm25_and_stub_tolerance(tmp_path):
    """Skill-Pro (ICML 2026) & SkillAlign (EMNLP 2026): C-first retrieval + when_to_use exposure."""
    _write_skill(
        tmp_path,
        "ledger-sop",
        description="Ledger JSON validator",
        when_to_use="When extracting audit_report.json ledger records",
        aliases=("audit-ledger",),
        body="Use strict schema output.",
    )
    _write_skill(
        tmp_path,
        "prose-writer",
        description="Narrative formatting",
        when_to_use="When drafting release blog articles",
        body=("audit_report.json ledger records " * 12).strip(),
    )
    store = SkillStore(tmp_path)
    hits_top1 = store.search("audit_report.json ledger", limit=1)
    assert [s.name for s in hits_top1] == ["ledger-sop"]

    # When C stage yields < limit matches, fallback searches body pi as well.
    hits_fallback = store.search("Narrative", limit=2)
    assert hits_fallback[0].name == "prose-writer"
    assert len(store.search("audit_report.json ledger", limit=2)) <= 2

    # ResourceLoader & CodingLifecycle._knowledge tolerate stub skill objects lacking `when_to_use`.
    @dataclass
    class _MinimalSkill:
        name: str
        version: str
        description: str

    rt = _offline_runtime([ModelResponse("done", usage=Usage(10, 4))])
    agent = Agent(tmp_path, rt, enable_memory=False, enable_evolution=False)
    try:
        agent.skills.capture_catalog = lambda: MappingProxyType(
            {"minimal": _MinimalSkill("minimal", "1.0.0", "Minimal stub")}
        )
        agent.resources.refresh_project_rules()
        msgs = [m for m in agent.session.project() if m.source == "skills"]
        assert len(msgs) >= 1
        assert "minimal: Minimal stub [1.0.0]" in msgs[-1].content

        # Real snapshot exposes `| when:` and `(when: ...)`
        real_store = SkillStore(tmp_path)
        agent.skills.capture_catalog = real_store.capture_catalog
        agent.skills.search = real_store.search
        agent.resources.refresh_project_rules()
        msgs_real = [m for m in agent.session.project() if m.source == "skills"]
        assert "| when: When extracting audit_report.json ledger records" in msgs_real[-1].content
        asyncio.run(agent.run_once("audit_report.json ledger"))
        cat_msgs = [m for m in agent.session.project() if m.source == "skill_catalog"]
        assert any("(when: When extracting audit_report.json ledger records)" in m.content for m in cat_msgs)
    finally:
        asyncio.run(agent.close())


def test_skill_diff_anchor_parsing_and_guard(tmp_path):
    """SkillDiff (arXiv:2611.25534): parse frontmatter/body anchors and block unapproved deletion."""
    anchors = _parse_anchors(
        {"anchors": '["Preserve inputs", "Check schema"]'},
        "- [ANCHOR] Preserve inputs\n- [ANCHOR] Verify outputs\n- Extra <!-- anchor -->\n",
    )
    assert anchors == ("Preserve inputs", "Check schema", "Verify outputs", "Extra")

    _write_skill(
        tmp_path,
        "anchored-skill",
        description="Anchored SOP",
        when_to_use="When modifying critical files",
        body="- [ANCHOR] Never touch raw inputs.\n- Optional detail line.\n",
    )
    skills = SkillStore(tmp_path)
    evo = EvolutionStore(skills)
    snap = skills.load("anchored-skill")
    assert snap.anchors == ("Never touch raw inputs.",)

    blocked = evo.apply(
        "anchored-skill",
        [{"op": "replace", "old": "- [ANCHOR] Never touch raw inputs.", "new": ""}],
        base_hash=snap.content_hash,
        permission_mode="acceptEdits",
    )
    assert blocked["status"] == "rejected"
    assert blocked["failure_kind"] == "anchor_violation"
    assert skills.load("anchored-skill").version == "1.0.0"
    assert len(evo.rejected_edits("anchored-skill", limit=10)) == 1

    allowed = evo.apply(
        "anchored-skill",
        [{"op": "replace", "old": "- [ANCHOR] Never touch raw inputs.", "new": "- Normal line."}],
        base_hash=snap.content_hash,
        permission_mode="acceptEdits",
        allow_anchor_override=True,
    )
    assert allowed["status"] == "updated"
    assert skills.load("anchored-skill").version == "1.0.1"


def test_wikiskill_typed_quotas_and_legacy_string_retention(tmp_path):
    """WikiSkill (arXiv:2608.27454): independent per-kind quotas preserve boundary & legacy notes."""
    assert set(KNOWLEDGE_QUOTAS) == set(KNOWLEDGE_KINDS)
    assert MAX_NOTES_PER_KIND == 10

    _write_skill(tmp_path, "wiki-skill", description="Wiki test", body="Follow rules.")
    skills = SkillStore(tmp_path)
    evo = EvolutionStore(skills)

    # Plain-string notes map to observation and retain up to MAX_NOTES_PER_KIND=10 distinct notes.
    for i in range(12):
        evo._save_note("wiki-skill", "dup" if i < 2 else f"obs-{i}", f"ev-{i}", "1.0.0")
    assert len(evo.knowledge("wiki-skill")) == 10

    # Flood observation notes; seeded boundary/verified_pattern notes must remain 100% intact.
    evo._save_note("wiki-skill", {"kind": "boundary", "text": "Do not overwrite config."}, "b-1", "1.0.0")
    evo._save_note("wiki-skill", {"kind": "verified_pattern", "text": "Sort IDs ascending."}, "v-1", "1.0.0")
    for i in range(30):
        evo._save_note("wiki-skill", {"kind": "observation", "text": f"noise-{i}"}, f"n-{i}", "1.0.0")
    kept = evo.knowledge("wiki-skill")
    assert any(item["kind"] == "boundary" and item["text"] == "Do not overwrite config." for item in kept)
    assert any(item["kind"] == "verified_pattern" and item["text"] == "Sort IDs ascending." for item in kept)
    assert sum(1 for item in kept if item["kind"] == "observation") == MAX_NOTES_PER_KIND

    prompt = evo.knowledge_prompt("wiki-skill")
    assert len(prompt) <= 2000
    assert prompt.index("[boundary;") < prompt.index("[verified_pattern;") < prompt.index("[observation;")


def test_skill_opt_rejected_edit_buffer_and_online_payload(tmp_path):
    """SkillOpt (NeurIPS 2026 Spotlight): rejected edits persist cleanly and feed optimizer."""
    from weyra.online_skill_evolution import MAINTENANCE_INSTRUCTION

    for allowed_kind in ("boundary", "verified_pattern", "failure_mode", "observation"):
        assert allowed_kind in MAINTENANCE_INSTRUCTION

    _write_skill(tmp_path, "opt-skill", description="Opt", body="Initial clause.")
    skills = SkillStore(tmp_path)
    evo = EvolutionStore(skills)
    for i in range(MAX_REJECTED_EDITS + 5):
        evo._save_rejected_edit(
            "opt-skill",
            {
                "event_id": f"e-{i}",
                "reason": "no_validation_gain",
                "edits_summary": f"edit #{i}",
                "failed_cases": ["case-1"],
                "evidence": "score tie",
                "skill_version": "1.0.0",
            },
        )
    recent = evo.rejected_edits("opt-skill", limit=50)
    assert len(recent) == MAX_REJECTED_EDITS
    assert recent[0]["event_id"] == "e-5"
    prompt_slice = evo.rejected_edits_prompt("opt-skill", limit=5)
    assert len(prompt_slice) <= 1500
    assert "no_validation_gain" in prompt_slice


def test_skillgen_null_baseline_create_and_legacy_gate_abstention(tmp_path):
    """SkillGen (arXiv:2605.10999): null-baseline gating for creation; non-baseline gate abstains."""
    # 1. Arbitrary callable / ReviewedCallbackGate lacks supports_null_baseline -> safely abstains.
    skills_legacy = SkillStore(tmp_path / "legacy")
    (tmp_path / "legacy").mkdir()
    evo_legacy = EvolutionStore(skills_legacy)

    async def _propose_new(_):
        return {"action": "create", "name": "new-sop", "body": "Always write out.txt"}

    async def _plain_gate(old, cand):
        raise AssertionError("should not run without supports_null_baseline")

    res_plain = asyncio.run(evo_legacy.maintain({}, _propose_new, authorized=True, validation_gate=_plain_gate))
    assert res_plain["status"] == "rejected"
    assert "baseline" in res_plain["reason"].lower()

    rt_cb = _offline_runtime()
    cb_gate = ReviewedCallbackGate(rt_cb, Budget(), lambda old, cand: {})
    assert not getattr(cb_gate, "supports_null_baseline", False)
    res_cb = asyncio.run(evo_legacy.maintain({}, _propose_new, authorized=True, validation_gate=cb_gate))
    assert res_cb["status"] == "rejected"
    assert "baseline" in res_cb["reason"].lower()

    # 2. ConfiguredSkillGate supports __null_baseline__ and short-circuits deterministic case with 0 Judge calls.
    proj = tmp_path / "gated_create"
    proj.mkdir()
    judge_spy = _FailIfCalledJudge()

    class _NullVsNewProvider(ScriptedProvider):
        async def stream(self, request, context):
            if not self.responses:
                if not any(m.role == "tool" for m in request.messages):
                    has_skill_in_sys = "installed skill named" in (request.system or "")
                    if has_skill_in_sys:
                        self.responses.append(
                            ModelResponse("", tool_calls=(ToolCall("s1", "skill", {"name": "new-sop"}),), usage=Usage(10, 4))
                        )
                    else:
                        # Null baseline writes stale content on validation (0 hard failures, semantic mismatch)
                        self.responses.append(
                            ModelResponse(
                                "",
                                tool_calls=(ToolCall("w0", "write_file", {"path": "out.txt", "content": "WRONG\n"}),),
                                usage=Usage(10, 4),
                            )
                        )
                        self.responses.append(ModelResponse("Completed without skill.", usage=Usage(10, 4)))
                else:
                    self.responses.append(
                        ModelResponse(
                            "",
                            tool_calls=(ToolCall("w1", "write_file", {"path": "out.txt", "content": "OK\n"}),),
                            usage=Usage(10, 4),
                        )
                    )
                    self.responses.append(ModelResponse("Completed with new-sop.", usage=Usage(10, 4)))
            async for ev in super().stream(request, context):
                yield ev

    registry = ProviderRegistry()
    registry.register("offline", _NullVsNewProvider())
    registry.register("judge_offline", judge_spy)
    runtime = ModelRuntime(
        registry,
        {
            "main": ModelInfo("offline", "scripted-main", 200000, 1024),
            "skill_judge": ModelInfo("judge_offline", "scripted-judge", 200000, 512, supports_tools=False),
        },
    )
    runtime.context_policy = {"headroom_tokens": 1024, "max_tokens": 1024, "retain_tokens": 256}
    session = SessionStore(project=proj, session_id="skillgen-create")
    budget = Budget()
    budget.attach(session)
    manifest = {
        "cases": [
            {
                "id": "det-create-1",
                "prompt": "Create out.txt with OK.",
                "files": {"in.txt": "seed"},
                "expected_artifacts": {"out.txt": "OK\n"},
                "deterministic_only": True,
            }
        ]
    }
    gate = ConfiguredSkillGate(runtime, budget, manifest, session=session)
    assert gate.supports_null_baseline is True
    evo = EvolutionStore(SkillStore(proj), session=session)
    evidence = {
        "user": "Create out.txt",
        "assistant": "Failed",
        "feedback": "Remember new-sop",
        "replay": {
            "turn_id": "t-create",
            "complete": True,
            "reason": "",
            "files": {"in.txt": "seed"},
            "written_paths": ["out.txt"],
            "context": [],
            "system_instructions": "",
        },
    }
    vctx = gate.freeze(evidence)

    async def _run():
        res = await evo.maintain(
            evidence,
            _propose_new,
            permission_mode="acceptEdits",
            validation_gate=gate,
            validation_context=vctx,
        )
        session.close()
        return res

    res = asyncio.run(_run())
    assert res["status"] == "updated"
    assert res["validation_report"]["judge_mode"] == "deterministic_short_circuit"
    assert judge_spy.calls == 0


def test_skillsbench_sopbench_deterministic_predicate_and_short_circuit():
    """SkillsBench & SOPBench: deterministic detection vs rubric-requiring cases."""
    det_case = {"id": "c1", "split": "validation", "deterministic_only": True, "expected_artifacts": {"a.json": "{}"}}
    rubric_case = {"id": "c2", "split": "validation", "expected_artifacts": {"a.json": "{}"}, "rubric": "Check tone"}
    assert _is_deterministic_case(det_case) is True
    assert _is_deterministic_case(rubric_case) is False
    assert _can_short_circuit_judge([det_case]) is True
    assert _can_short_circuit_judge([det_case, rubric_case]) is False

    j_gain = _deterministic_judgment({"passed": False, "hard_failures": 0}, {"passed": True, "hard_failures": 0})
    assert j_gain["preferred"] == "candidate" and j_gain["model_approved"] is True
    j_reg = _deterministic_judgment({"passed": True, "hard_failures": 0}, {"passed": False, "hard_failures": 1})
    assert j_reg["preferred"] == "old" and j_reg["model_approved"] is False


def test_trace_summary_extracted_on_tools_and_omitted_on_clean_disabled_turn(tmp_path):
    """Rethinking (arXiv:2608.02636) & AutoSaddler: trace_summary capture with backward compat."""
    rt_plain = _offline_runtime([ModelResponse("plain answer", usage=Usage(8, 3))])
    agent_plain = Agent(tmp_path / "plain", rt_plain, enable_memory=False, enable_evolution=False)
    try:
        asyncio.run(agent_plain.run_once("hi"))
        assert agent_plain._pending_window == {"user": "hi", "assistant": "plain answer"}
    finally:
        asyncio.run(agent_plain.close())

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "a.txt").write_text("hello\n", encoding="utf-8")
    rt_tools = _offline_runtime([
        ModelResponse("", tool_calls=(ToolCall("t1", "read_file", {"path": "missing.txt"}),), usage=Usage(10, 4)),
        ModelResponse("", tool_calls=(ToolCall("t2", "read_file", {"path": "missing.txt"}),), usage=Usage(10, 4)),
        ModelResponse("handled error", usage=Usage(10, 4)),
    ])
    agent_tools = Agent(tmp_path / "tools", rt_tools, enable_memory=False, enable_evolution=False)
    try:
        asyncio.run(agent_tools.run_once("read missing"))
        ts = agent_tools._pending_window["trace_summary"]
        assert ts["tool_calls"] == ["read_file", "read_file"]
        assert len(ts["error_tools"]) == 2
        assert ts["retry_count"] == 1
        assert ts["turn_state"] == "completed"
    finally:
        asyncio.run(agent_tools.close())

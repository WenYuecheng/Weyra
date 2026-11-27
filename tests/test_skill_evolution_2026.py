"""Zero-API unit and integration tests for Weyra's open-source-aligned skill subsystem."""

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
    MAX_REJECTED_EDITS,
    compute_lexical_prior_coverage,
)
from weyra.skill_judge import SkillJudge, SkillValidationGate, _is_deterministic_case
from weyra.skilldiff_vendor import MARK as SKILLDIFF_MARK, mark as skilldiff_mark, split_items
from weyra.skillopt_vendor import (
    Edit,
    EditRecord,
    Patch,
    apply_edits_detailed,
    apply_patch_with_report,
    build_scheduler,
    evaluate_gate,
)
from weyra.skills import (
    SkillStore,
    extract_user_invoked_skills,
    format_available_skills,
    render_skill_content,
)


def _write_skill(
    root,
    name: str,
    *,
    description: str,
    when_to_use: str = "",
    aliases=(),
    body: str,
    subdir: str = ".weyra/skills",
    disable_model_invocation: bool = False,
    user_invocable: bool = True,
    resources: dict[str, str] | None = None,
):
    skill_dir = root / subdir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": name,
        "version": "1.0.0",
        "description": description,
        "when_to_use": when_to_use,
    }
    if aliases:
        meta["aliases"] = list(aliases)
    if disable_model_invocation:
        meta["disable-model-invocation"] = True
    if not user_invocable:
        meta["user-invocable"] = False
    header = (
        "---\n"
        + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items())
        + "\n---\n"
    )
    (skill_dir / "SKILL.md").write_text(header + body, encoding="utf-8")
    for rel, content in (resources or {}).items():
        rpath = skill_dir / rel
        rpath.parent.mkdir(parents=True, exist_ok=True)
        rpath.write_text(content, encoding="utf-8")


def _offline_runtime(main_responses=(), judge_responses=()):
    registry = ProviderRegistry()
    registry.register("offline", ScriptedProvider(list(main_responses)))
    registry.register("judge_offline", ScriptedProvider(list(judge_responses)))
    runtime = ModelRuntime(
        registry,
        {
            "main": ModelInfo("offline", "scripted-main", 200000, 1024),
            "skill_judge": ModelInfo(
                "judge_offline", "scripted-judge", 200000, 512, supports_tools=False
            ),
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


def test_opensource_skill_discovery_disclosure_and_progressive_loading(tmp_path):
    """DSH + Pi (agentskills.io) + Hermes: multi-root, XML catalog, file_path resource & /gesture."""
    _write_skill(
        tmp_path,
        "ledger-sop",
        description="Ledger JSON validator",
        when_to_use="When extracting audit_report.json ledger records",
        body="Read references/schema.txt before writing audit_report.json.",
        resources={"references/schema.txt": '{"schema": "sop-v2"}\n'},
    )
    # Nested category under .agents/skills + excluded references/SKILL.md should not collide or leak
    _write_skill(
        tmp_path,
        "team-rule",
        subdir=".agents/skills/backend",
        description="Shared .agents skill",
        body="Follow shared repo rules.",
    )
    nested_ref_skill = tmp_path / ".agents" / "skills" / "backend" / "team-rule" / "references" / "ignored"
    nested_ref_skill.mkdir(parents=True)
    (nested_ref_skill / "SKILL.md").write_text("---\nname: should-ignore\n---\nIgnored.", encoding="utf-8")

    _write_skill(
        tmp_path,
        "manual-only",
        description="Manual deploy checklist",
        body="Run manual release verification steps.",
        disable_model_invocation=True,
    )

    store = SkillStore(tmp_path, skill_dirs=[])
    catalog = store.capture_catalog()
    assert set(catalog) == {"ledger-sop", "team-rule", "manual-only"}
    assert "should-ignore" not in catalog
    assert catalog["manual-only"].model_invocable is False
    assert catalog["manual-only"].user_invocable is True

    # Model search excludes disable-model-invocation skills by default
    hits = store.search("manual deploy", limit=5, catalog=catalog)
    assert all(s.name != "manual-only" for s in hits)

    # <available_skills> XML catalog & <skill_content> rendering
    xml_catalog = format_available_skills(list(catalog.values()))
    assert "<available_skills>" in xml_catalog
    assert "`ledger-sop`: Ledger JSON validator" in xml_catalog
    assert "manual-only" not in xml_catalog

    rendered = render_skill_content(catalog["ledger-sop"])
    assert '<skill_content name="ledger-sop">' in rendered
    assert "references/schema.txt" in rendered

    # Gesture parser ignores URLs/paths and extracts /manual-only
    assert extract_user_invoked_skills("Check /usr/bin and run /manual-only now") == ["manual-only"]

    # Runtime integration: user /manual-only injects <skill_content>, model cannot load manual-only via tool,
    # and model loads ledger-sop + progressive file_path resource cleanly.
    rt = _offline_runtime([
        ModelResponse("", tool_calls=(ToolCall("s0", "skill", {"name": "manual-only"}),), usage=Usage(10, 4)),
        ModelResponse("", tool_calls=(ToolCall("s1", "skill", {"name": "ledger-sop"}),), usage=Usage(10, 4)),
        ModelResponse("", tool_calls=(ToolCall("s2", "skill", {"name": "ledger-sop", "file_path": "references/schema.txt"}),), usage=Usage(10, 4)),
        ModelResponse("Loaded resource and followed /manual-only.", usage=Usage(10, 4)),
    ])
    agent = Agent(tmp_path, rt, permission_mode="bypassPermissions", enable_memory=False, enable_evolution=False)
    try:
        agent.skills = store
        agent.resources.refresh_project_rules()
        out = asyncio.run(agent.run_once("Please run /manual-only and inspect ledger"))
        assert "Loaded resource" in out
        invocations = [m for m in agent.session.project() if m.source == "skill_invocation"]
        assert len(invocations) == 1
        assert '<skill_content name="manual-only">' in invocations[0].content

        tool_results = [r["data"] for r in agent.session.records if r.get("kind") == "tool_result"]
        assert tool_results[0]["status"] == "error"  # disable-model-invocation blocked
        assert tool_results[1]["status"] == "success"
        assert '<skill_content name="ledger-sop">' in tool_results[1]["content"]
        assert tool_results[2]["status"] == "success"
        assert '{"schema": "sop-v2"}' in tool_results[2]["content"]
    finally:
        asyncio.run(agent.close())

    # Minimal stub object compatibility in ResourceLoader
    @dataclass
    class _StubSkill:
        name: str
        version: str
        description: str

    rt2 = _offline_runtime([ModelResponse("ok", usage=Usage(5, 2))])
    agent2 = Agent(tmp_path / "stub", rt2, enable_memory=False, enable_evolution=False)
    try:
        agent2.skills.capture_catalog = lambda: MappingProxyType({"stub": _StubSkill("stub", "1.0.0", "Stub desc")})
        agent2.resources.refresh_project_rules()
        msgs = [m for m in agent2.session.project() if m.source == "skills"]
        assert "`stub`: Stub desc [1.0.0]" in msgs[-1].content
    finally:
        asyncio.run(agent2.close())


def test_vendored_skillopt_primitives_and_schedulers():
    """microsoft/SkillOpt (MIT): apply_patch_with_report, schedulers, evaluate_gate, and LEARNED block."""
    doc = "Rule 1: inspect files.\n\n<!-- SLOW_UPDATE_START -->\nPinned meta.\n<!-- SLOW_UPDATE_END -->\n"
    updated, reports = apply_patch_with_report(
        doc,
        Patch(
            edits=[
                Edit(op="replace", target="Rule 1: inspect files.", content="Rule 1: read all files."),
                Edit(op="insert_after", target="Rule 1: read all files.", content="Rule 2: verify output."),
                Edit(op="append", content="Rule 3: preserve inputs."),
                Edit(op="delete", target="Pinned meta."),  # inside protected region -> skipped
            ]
        ),
    )
    assert "Rule 1: read all files." in updated
    assert "Rule 2: verify output." in updated
    assert "Rule 3: preserve inputs." in updated
    assert "Pinned meta." in updated
    assert reports[3]["status"] == "skipped_protected_region"

    # Schedulers
    assert build_scheduler("constant", max_lr=4, min_lr=1, total_steps=4).get_lr(3) == 4
    lin = build_scheduler("linear", max_lr=6, min_lr=2, total_steps=4)
    assert [lin.step() for _ in range(4)] == [5, 4, 3, 2]
    cos = build_scheduler("cosine", max_lr=6, min_lr=2, total_steps=4)
    assert cos.get_lr(4) == 2

    # Gate
    g_best = evaluate_gate("cand", 1.0, "curr", 0.5, "best", 0.5, 0, 1)
    assert g_best.action == "accept_new_best" and g_best.best_score == 1.0
    g_rej = evaluate_gate("cand", 0.5, "curr", 0.5, "best", 1.0, 1, 2)
    assert g_rej.action == "reject"

    # SkillOpt-Sleep LEARNED block
    learned_doc, applied, unmatched = apply_edits_detailed(
        "Core expert workflow.",
        [
            EditRecord(target="skill", op="add", content="Always sort items by ID."),
            EditRecord(target="skill", op="delete", anchor="nonexistent"),
        ],
    )
    assert len(applied) == 1 and len(unmatched) == 1
    assert "Core expert workflow." in learned_doc
    assert "<!-- SKILLOPT-SLEEP:LEARNED START -->" in learned_doc
    assert "- Always sort items by ID." in learned_doc


def test_vendored_skilldiff_marking_reversibility_and_unvalidated_guard(tmp_path):
    """SkillDiff (arXiv:2611.25534, MIT): atomic split, in-place **[FOCUS]** mark & guard."""
    raw = (
        "---\nname: diff-sop\nversion: 1.0.0\ndescription: Diff test\n---\n"
        "- Read every input file carefully.\n"
        "- Always emit schema_version=\"sop-v2\" and sort ascending.\n"
    )
    prior = "Read every input file carefully and write clear code."
    coverage = compute_lexical_prior_coverage(raw, [prior])
    items = split_items(raw)
    marked = skilldiff_mark(raw, items, coverage)
    assert SKILLDIFF_MARK in marked
    assert marked.count(SKILLDIFF_MARK) == 1  # only the non-prior sop-v2 clause

    # Removing SkillDiff note and **[FOCUS]** recovers original expert clauses
    _write_skill(
        tmp_path,
        "diff-sop",
        description="Diff test",
        body="- Read every input file carefully.\n- Always emit schema_version=\"sop-v2\" and sort ascending.\n",
    )
    skills = SkillStore(tmp_path, skill_dirs=[])
    evo = EvolutionStore(skills)
    res_mark = evo.annotate_skilldiff("diff-sop", coverage, permission_mode="acceptEdits")
    assert res_mark["status"] == "updated"
    snap = skills.load("diff-sop")
    assert len(snap.focus_items) == 1
    assert 'schema_version="sop-v2"' in snap.focus_items[0]

    # Unvalidated deletion of **[FOCUS]** item is blocked & logged to rejected_edits.jsonl
    blocked = evo.apply(
        "diff-sop",
        [{"op": "delete", "target": '- **[FOCUS]** Always emit schema_version="sop-v2" and sort ascending.'}],
        base_hash=snap.content_hash,
        permission_mode="acceptEdits",
    )
    assert blocked["status"] == "rejected"
    assert blocked["failure_kind"] == "focus_item_violation"
    assert skills.load("diff-sop").version == "1.0.1"
    assert len(evo.rejected_edits("diff-sop", limit=10)) == 1

    # Validated deletion (e.g. after D_sel gate approval) is permitted
    allowed = evo.apply(
        "diff-sop",
        [{"op": "replace", "target": '- **[FOCUS]** Always emit schema_version="sop-v2" and sort ascending.', "content": "- Emit sop-v3."}],
        base_hash=snap.content_hash,
        permission_mode="acceptEdits",
        _validation="validated",
    )
    assert allowed["status"] == "updated"
    assert skills.load("diff-sop").version == "1.0.2"


def test_skillopt_rejected_edit_buffer_and_validation_gate_create_and_patch(tmp_path):
    """SkillOpt rejected buffer + SkillValidationGate deterministic 0-judge gating + null_baseline create."""
    _write_skill(tmp_path, "opt-skill", description="Opt", body="Initial clause.")
    skills = SkillStore(tmp_path, skill_dirs=[])
    evo = EvolutionStore(skills, lr_scheduler="linear", max_lr=2, min_lr=1, total_steps=4)
    for i in range(MAX_REJECTED_EDITS + 3):
        evo._save_rejected_edit(
            "opt-skill",
            {
                "event_id": f"e-{i}",
                "reason": "no_validation_gain",
                "gate_action": "reject",
                "current_score": 0.5,
                "candidate_score": 0.5,
                "edits_summary": f"edit #{i}",
                "failed_cases": ["case-1"],
                "skill_version": "1.0.0",
            },
        )
    recent = evo.rejected_edits("opt-skill", limit=50)
    assert len(recent) == MAX_REJECTED_EDITS
    assert recent[0]["event_id"] == "e-3"
    assert "no_validation_gain" in evo.rejected_edits_prompt("opt-skill", limit=5)

    # Enforced SkillOpt LR edit budget
    snap_opt = skills.load("opt-skill")
    too_many = evo.apply(
        "opt-skill",
        [{"op": "append", "content": "A"}, {"op": "append", "content": "B"}, {"op": "append", "content": "C"}],
        base_hash=snap_opt.content_hash,
        permission_mode="acceptEdits",
    )
    assert too_many["status"] == "rejected"
    assert "budget" in too_many["reason"]

    # Deterministic SkillValidationGate with __null_baseline__ creation & 0 Judge calls
    proj = tmp_path / "gated_create"
    proj.mkdir()
    judge_spy = _FailIfCalledJudge()

    class _NullVsNewProvider(ScriptedProvider):
        async def stream(self, request, context):
            if not self.responses:
                if not any(m.role == "tool" for m in request.messages):
                    has_catalog = "new-sop" in "\n".join(m.content for m in request.messages)
                    if has_catalog:
                        self.responses.append(
                            ModelResponse("", tool_calls=(ToolCall("s1", "skill", {"name": "new-sop"}),), usage=Usage(10, 4))
                        )
                    else:
                        self.responses.append(
                            ModelResponse("", tool_calls=(ToolCall("w0", "write_file", {"path": "out.txt", "content": "WRONG\n"}),), usage=Usage(10, 4))
                        )
                        self.responses.append(ModelResponse("Completed without skill.", usage=Usage(10, 4)))
                else:
                    self.responses.append(
                        ModelResponse("", tool_calls=(ToolCall("w1", "write_file", {"path": "out.txt", "content": "OK\n"}),), usage=Usage(10, 4))
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
    session = SessionStore(project=proj, session_id="skillopt-create")
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
    gate = SkillValidationGate(runtime, budget, manifest, session=session)
    assert gate.judge is None
    evo2 = EvolutionStore(SkillStore(proj, skill_dirs=[]), session=session)

    async def _propose_new(_):
        return {"action": "create", "name": "new-sop", "description": "Write OK", "body": "Always write OK to out.txt."}

    async def _run():
        res = await evo2.maintain(
            {"user": "Create out.txt", "assistant": "Failed"},
            _propose_new,
            permission_mode="acceptEdits",
            validation_gate=gate,
        )
        session.close()
        return res

    res = asyncio.run(_run())
    assert res["status"] == "updated"
    assert res["validation_report"]["judge_mode"] == "deterministic"
    assert res["validation_report"]["gate_action"] == "accept_new_best"
    assert judge_spy.calls == 0


def test_deterministic_predicate_and_order_swapped_pairwise_judge(tmp_path):
    """Deterministic short-circuit predicate & Zheng et al. order-swapped pairwise consistency."""
    det = {"id": "c1", "deterministic_only": True, "expected_artifacts": {"a.json": "{}"}}
    rub = {"id": "c2", "expected_artifacts": {"a.json": "{}"}, "rubric": "Check tone"}
    assert _is_deterministic_case(det) is True
    assert _is_deterministic_case(rub) is False

    # Consistent pairwise winner -> candidate approved (soft 1.0)
    rt_ok = _offline_runtime(
        judge_responses=[
            ModelResponse('{"winner": "B", "rationale": "B avoids preamble."}', usage=Usage(10, 5)),
            ModelResponse('{"winner": "A", "rationale": "A avoids preamble."}', usage=Usage(10, 5)),
        ]
    )
    judge_ok = SkillJudge(rt_ok, Budget())
    out_ok = asyncio.run(judge_ok.compare({"prompt": "story"}, {"final": "Summary: x"}, {"final": "Rain fell."}))
    assert out_ok["preferred"] == "candidate"
    assert out_ok["consistent"] is True
    assert out_ok["soft"] == {"old": 0.0, "candidate": 1.0}

    # Position bias conflict -> inconsistent abstention
    rt_bias = _offline_runtime(
        judge_responses=[
            ModelResponse('{"winner": "A", "rationale": "First is better."}', usage=Usage(10, 5)),
            ModelResponse('{"winner": "A", "rationale": "First is better."}', usage=Usage(10, 5)),
        ]
    )
    judge_bias = SkillJudge(rt_bias, Budget())
    out_bias = asyncio.run(judge_bias.compare({"prompt": "story"}, {"final": "A"}, {"final": "B"}))
    assert out_bias["preferred"] == "inconsistent"
    assert out_bias["consistent"] is False
    assert out_bias["model_approved"] is False


def test_trace_summary_extracted_on_tools_and_omitted_on_clean_disabled_turn(tmp_path):
    """Execution trace_summary extraction with clean backward compatibility."""
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

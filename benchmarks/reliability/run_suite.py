"""零 API 成本的 Harness 可靠性与 SkillOpt + SkillDiff 30 轮防退化确定性基准套件。

【评测设计对齐】
1. Suite A — Harness 故障注入矩阵 (6 项):
   在真实 SessionStore / ToolRuntime / Budget / EvolutionStore 上注入崩溃、篡改、TOCTOU 与预算耗尽故障。
2. Suite B — 30 轮连续演化防退化压测 (对齐 SkillOpt `arXiv:2605.23904` & SkillDiff `arXiv:2611.25534`):
   在真实 EvolutionStore + SkillValidationGate + 确定性断言模式下，对比无反馈自由改写与
   SkillDiff `**[FOCUS]**` 标注 + SkillOpt 门控/被拒编辑缓冲的长期稳定性。
3. Suite C — 只读会话轨迹与技能采用审计:
   通过全真 CodingAgent + ScriptedProvider 驱动干净轨迹与错误重试轨迹，调用 `audit_session_db` 校验统计。

运行方式（0 元 API 消耗）:
    python benchmarks/reliability/run_suite.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weyra.agent import Agent  # noqa: E402
from weyra.contracts import ModelInfo, ModelResponse, ToolCall, ToolSpec, Usage  # noqa: E402
from weyra.core.state import Budget, BudgetExceeded, RunContext  # noqa: E402
from weyra.frontmatter import atomic, sha  # noqa: E402
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider  # noqa: E402
from weyra.session import SessionStore  # noqa: E402
from weyra.skill_evolution import EvolutionStore, compute_lexical_prior_coverage  # noqa: E402
from weyra.skill_judge import SkillValidationGate  # noqa: E402
from weyra.skills import SkillStore  # noqa: E402
from weyra.trace_audit import audit_session_db  # noqa: E402

DEFAULT_OUTPUT = Path(__file__).resolve().with_name("results.json")


def _resp(text: str = "", calls=()) -> ModelResponse:
    return ModelResponse(text, tool_calls=tuple(calls), usage=Usage(20, 8))


def _make_runtime(main_responses, *, judge_responses=()):
    registry = ProviderRegistry()
    registry.register("offline", ScriptedProvider(list(main_responses)))
    registry.register("judge_offline", ScriptedProvider(list(judge_responses)))
    routes = {
        "main": ModelInfo("offline", "scripted-main", 200000, 1024),
        "skill_judge": ModelInfo("judge_offline", "scripted-judge", 200000, 512, supports_tools=False),
    }
    runtime = ModelRuntime(registry, routes)
    runtime.context_policy = {"headroom_tokens": 1024, "max_tokens": 1024, "retain_tokens": 256}
    return runtime


async def _run_suite_a() -> dict:
    """Suite A: 6 项确定性 Harness 故障注入与可靠性验证。"""
    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="weyra-rel-a-") as tmp:
        base = Path(tmp)

        # 1. prepared 状态演化崩溃恢复：绝不隐式发布，必须收敛为 interrupted
        p1 = base / "case1"
        p1.mkdir()
        skills1 = SkillStore(p1, skill_dirs=[])
        evo1 = EvolutionStore(skills1)
        evo1.create("safe-rule", "Always preserve inputs.", description="Safe", permission_mode="acceptEdits")
        snap1 = skills1.load("safe-rule")
        evo1._dir("safe-rule")
        evo1._record(
            "safe-rule",
            {
                "status": "prepared",
                "event_id": "crash-evt-1",
                "name": "safe-rule",
                "version": "1.0.1",
                "base_hash": snap1.content_hash,
                "content_hash": sha("never-written"),
                "skill_path": str(snap1.path.resolve()),
            },
        )
        recovered_evo = EvolutionStore(SkillStore(p1, skill_dirs=[]))
        statuses1 = [r.get("status") for r in recovered_evo.recovery_results]
        ok1 = (
            statuses1 == ["interrupted"]
            and SkillStore(p1, skill_dirs=[]).load("safe-rule").version == "1.0.0"
        )
        results["prepared_evolution_crash_recovery"] = {"passed": ok1, "statuses": statuses1}

        # 2. 工具执行中途断电恢复：合成 unknown outcome，闭合悬垂 tool_calls 防 400
        p2 = base / "case2"
        p2.mkdir()
        s2 = SessionStore(project=p2, session_id="crash-tool")
        s2.append("turn_start", {"turn_id": "t1", "prompt": "edit"}, True)
        s2.append(
            "message",
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-crash-1", "name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}],
            },
            True,
        )
        s2.append("tool_intent", {"call_id": "call-crash-1", "name": "write_file", "arguments": {"path": "a.txt"}}, True)
        s2.close()
        s2_reopened = SessionStore(project=p2, session_id="crash-tool")
        s2_reopened.recover_interrupted()
        view2 = s2_reopened.project()
        tool_msgs = [m for m in view2 if m.role == "tool" and m.tool_call_id == "call-crash-1"]
        latest_end = s2_reopened.latest("turn_end") or {}
        s2_reopened.close()
        ok2 = (
            len(tool_msgs) == 1
            and "Outcome unknown" in tool_msgs[0].content
            and latest_end.get("reason") == "process_interrupted"
        )
        results["tool_crash_interrupted_recovery"] = {"passed": ok2, "turn_end_reason": latest_end.get("reason")}

        # 3. 外部并发篡改：base_hash 冲突 + 文件 OCC 冲突拦截
        p3 = base / "case3"
        p3.mkdir()
        (p3 / "note.txt").write_text("v1", encoding="utf-8")
        skills3 = SkillStore(p3, skill_dirs=[])
        evo3 = EvolutionStore(skills3)
        evo3.create("occ-skill", "Keep v1.", description="OCC", permission_mode="acceptEdits")
        stale_snap = skills3.load("occ-skill")
        atomic(stale_snap.path, stale_snap.path.read_text(encoding="utf-8") + "\nExternal edit.")
        rejected3 = evo3.apply(
            "occ-skill",
            [{"op": "append", "content": "New clause"}],
            base_hash=stale_snap.content_hash,
            permission_mode="acceptEdits",
        )
        rt3 = _make_runtime([])
        agent3 = Agent(p3, rt3, permission_mode="bypassPermissions", enable_memory=False, enable_evolution=False)
        occ_ctx = RunContext(p3, agent3.session, agent3.budget, permission_mode="bypassPermissions")
        read_res = await agent3.tools.execute(ToolCall("r1", "read_file", {"path": "note.txt"}), occ_ctx)
        (p3 / "note.txt").write_text("external-v2", encoding="utf-8")
        write_res = await agent3.tools.execute(
            ToolCall("w1", "write_file", {"path": "note.txt", "content": "agent-write"}), occ_ctx
        )
        await occ_ctx.close()
        await agent3.close()
        ok3 = (
            rejected3.get("status") == "rejected"
            and "Base hash conflict" in rejected3.get("reason", "")
            and read_res.status == "success"
            and write_res.status == "error"
            and (p3 / "note.txt").read_text(encoding="utf-8") == "external-v2"
        )
        results["external_file_occ_and_base_hash_conflict"] = {
            "passed": ok3,
            "skill_reject_reason": rejected3.get("reason"),
            "tool_status": write_res.status,
        }

        # 4. 技能静态资源 SHA-256 漂移拦截
        p4 = base / "case4"
        p4.mkdir()
        skill_dir4 = p4 / ".weyra" / "skills" / "res-skill"
        skill_dir4.mkdir(parents=True)
        (skill_dir4 / "SKILL.md").write_text("---\nname: res-skill\nversion: 1.0.0\n---\nUse helper.sh\n", encoding="utf-8")
        (skill_dir4 / "helper.sh").write_text("echo safe\n", encoding="utf-8")
        store4 = SkillStore(p4, skill_dirs=[])
        snap4 = store4.load("res-skill")
        (skill_dir4 / "helper.sh").write_text("echo tampered\n", encoding="utf-8")
        drift_caught = False
        try:
            store4.read_resource(snap4, "helper.sh")
        except ValueError as err:
            drift_caught = "Resource drift detected" in str(err)
        results["skill_resource_sha256_drift"] = {"passed": drift_caught}

        # 5. 审批后权限撤销 TOCTOU 二次重检阻断
        p5 = base / "case5"
        p5.mkdir()
        s5 = SessionStore(project=p5, session_id="toctou")
        from weyra.core.tools import ToolRuntime
        tools5 = ToolRuntime(p5, s5, permission_mode="default")
        ctx5 = RunContext(p5, s5, Budget(), permission_mode="default")

        def confirm_and_revoke(spec, args, ctx):
            ctx.permission_mode = "plan"
            tools5.permission_mode = "plan"
            return True

        tools5.confirm = confirm_and_revoke
        spec5 = ToolSpec("run_shell", "run", {"type": "object", "properties": {}}, lambda a, c: "ran", "exclusive", "shell")
        allowed5 = await tools5._allowed(spec5, {}, ctx5)
        s5.close()
        results["post_approval_permission_revocation_toctou"] = {"passed": allowed5 is False, "allowed": allowed5}

        # 6. Fail-Closed 预算熔断与重启挂起预扣额重放防绕过
        p6 = base / "case6"
        p6.mkdir()
        s6 = SessionStore(project=p6, session_id="budget-guard")
        b6 = Budget(token_limit=100, cost_limit_cny="0.05")
        b6.attach(s6)
        res_id = await b6.reserve(70, purpose="main", estimated_cost_cny="0.03")
        exceeded_hit = False
        try:
            await b6.reserve(50, purpose="main", estimated_cost_cny="0.03")
        except BudgetExceeded:
            exceeded_hit = True
        s6.close()
        s6_restart = SessionStore(project=p6, session_id="budget-guard")
        b6_restart = Budget(token_limit=100, cost_limit_cny="0.05")
        b6_restart.attach(s6_restart)
        bypass_blocked = False
        try:
            await b6_restart.reserve(40, purpose="main", estimated_cost_cny="0.01")
        except BudgetExceeded:
            bypass_blocked = True
        s6_restart.close()
        ok6 = exceeded_hit and bypass_blocked and b6_restart.total_tokens == 70 and bool(res_id)
        results["fail_closed_budget_and_restart_antibypass"] = {
            "passed": ok6,
            "replayed_tokens": b6_restart.total_tokens,
        }

    passed_count = sum(1 for row in results.values() if row["passed"])
    return {"passed": passed_count, "total": len(results), "cases": results}


async def _run_suite_b() -> dict:
    """Suite B: 30 轮连续演化防退化压测 (Baseline vs SkillOpt + SkillDiff Guarded, 0 LLM Judge)."""
    expert_clauses = [
        "Always check source IDs against input files before writing report.json",
        "Never modify protected input paths in workspace",
        "Report exact JSON schema with summary and items keys",
        "Abstain and set status=incomplete when evidence is missing",
    ]
    initial_body = "\n".join(
        [f"- {clause}." for clause in expert_clauses]
        + ["- Keep diagnostic reasoning concise."]
    )

    # 1. 无护栏自由改写基线（向模型先验坍缩，逐轮丢失非先验专家规则）
    baseline_body = initial_body
    for r in range(1, 31):
        if r % 6 == 0:
            victim = expert_clauses[(r // 6 - 1) % len(expert_clauses)]
            baseline_body = baseline_body.replace(f"- {victim}.\n", "")
    baseline_focus_retention = sum(c in baseline_body for c in expert_clauses) / len(expert_clauses)

    # 2. Weyra: SkillDiff (**[FOCUS]**) + SkillOpt (evaluate_gate + rejected_edits.jsonl + SHA-256 缓存)
    rounds = 30
    with tempfile.TemporaryDirectory(prefix="weyra-rel-b-") as tmp:
        root = Path(tmp)
        skills = SkillStore(root, skill_dirs=[])
        session = SessionStore(project=root, session_id="suite-b")
        evo = EvolutionStore(skills, session=session, lr_scheduler="constant", max_lr=2, min_lr=1)
        evo.create(
            "strict-audit",
            initial_body,
            description="Auditable extraction protocol",
            when_to_use="When extracting structured JSON reports from project files",
            permission_mode="acceptEdits",
        )
        raw_text = skills.load("strict-audit").path.read_text(encoding="utf-8")
        prior_text = "Keep diagnostic reasoning concise and write valid output."
        coverage = compute_lexical_prior_coverage(raw_text, [prior_text])
        evo.annotate_skilldiff("strict-audit", coverage, permission_mode="acceptEdits")

        manifest = {
            "cases": [
                {
                    "id": "det-case-1",
                    "prompt": "Extract ID-42 findings to report.json without modifying data.txt.",
                    "files": {"data.txt": "ID-42 verified"},
                    "deterministic_only": True,
                    "hard_constraints": {
                        "required_output_paths": ["report.json"],
                        "allowed_output_paths": ["report.json"],
                        "expected_semantic_artifacts": {
                            "report.json": {"summary": "ok", "items": [{"source": "ID-42"}]}
                        },
                    },
                },
                {
                    "id": "det-case-2",
                    "prompt": "Extract ID-99 second record to report.json.",
                    "files": {"data.txt": "ID-99 verified"},
                    "deterministic_only": True,
                    "hard_constraints": {
                        "required_output_paths": ["report.json"],
                        "allowed_output_paths": ["report.json"],
                        "expected_semantic_artifacts": {
                            "report.json": {"summary": "ok", "items": [{"source": "ID-99"}]}
                        },
                    },
                },
            ]
        }

        class _DynamicGateProvider(ScriptedProvider):
            async def stream(self, request, context):
                user_msg = "\n".join(m.content for m in request.messages if m.role == "user")
                if not self.responses:
                    if not any(m.role == "tool" for m in request.messages):
                        self.responses.append(_resp(calls=[ToolCall("sk", "skill", {"name": "strict-audit"})]))
                    else:
                        skill_text = "\n".join(m.content for m in request.messages if m.role == "tool")
                        if "REGRESS_BREAK" in skill_text:
                            payload = json.dumps({"summary": "bad", "items": []})
                            self.responses.append(_resp(calls=[ToolCall("wf", "write_file", {"path": "report.json", "content": payload})]))
                            self.responses.append(_resp("Regressive output."))
                        elif "GOOD_SCHEMA_HINT" in skill_text:
                            src_id = "ID-99" if "ID-99" in user_msg else "ID-42"
                            payload = json.dumps({"summary": "ok", "items": [{"source": src_id}]})
                            self.responses.append(_resp(calls=[ToolCall("wf", "write_file", {"path": "report.json", "content": payload})]))
                            self.responses.append(_resp("Completed report.json."))
                        else:
                            if "ID-42" in user_msg:
                                payload = json.dumps({"summary": "ok", "items": [{"source": "ID-42"}]})
                            else:
                                payload = json.dumps({"summary": "missing", "items": []})
                            self.responses.append(_resp(calls=[ToolCall("wf", "write_file", {"path": "report.json", "content": payload})]))
                            self.responses.append(_resp("Completed baseline attempt."))
                async for ev in super().stream(request, context):
                    yield ev

        class _FailIfCalledJudge(ScriptedProvider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def stream(self, request, context):
                self.calls += 1
                raise AssertionError("Judge LLM should not be called for deterministic cases")
                yield  # pragma: no cover

        judge_spy = _FailIfCalledJudge()
        registry = ProviderRegistry()
        registry.register("offline", _DynamicGateProvider())
        registry.register("judge_offline", judge_spy)
        runtime = ModelRuntime(
            registry,
            {
                "main": ModelInfo("offline", "scripted-main", 200000, 1024),
                "skill_judge": ModelInfo("judge_offline", "scripted-judge", 200000, 512, supports_tools=False),
            },
        )
        runtime.context_policy = {"headroom_tokens": 1024, "max_tokens": 1024, "retain_tokens": 256}
        budget_b = Budget()
        budget_b.attach(session)
        gate = SkillValidationGate(runtime, budget_b, manifest, session=session)

        focus_violations_blocked = 0
        regressions_or_ties_blocked = 0
        promotions = 0
        cache_hits = 0
        cache_misses = 0

        for r in range(1, rounds + 1):
            snap = skills.load("strict-audit")
            if r % 5 == 1:
                victim = expert_clauses[(r // 5) % len(expert_clauses)]
                proposal = {
                    "action": "apply",
                    "name": "strict-audit",
                    "edits": [{"op": "delete", "target": f"- **[FOCUS]** {victim}."}],
                    "reasoning": f"Round {r} prior collapse deletion",
                }
            elif r % 5 in {2, 3}:
                proposal = {
                    "action": "apply",
                    "name": "strict-audit",
                    "edits": [{"op": "append", "content": f"- REGRESS_BREAK round {r}"}],
                    "reasoning": f"Round {r} regressive edit",
                }
            elif r == 5:
                proposal = {
                    "action": "apply",
                    "name": "strict-audit",
                    "edits": [{"op": "append", "content": "- GOOD_SCHEMA_HINT: always write report.json for all IDs."}],
                    "reasoning": "Fix case 2 missing report.json",
                }
            else:
                proposal = {
                    "action": "apply",
                    "name": "strict-audit",
                    "edits": [{"op": "append", "content": f"- Cosmetic hint #{r}."}],
                    "reasoning": f"Round {r} cosmetic edit",
                }

            async def _propose(_, p=proposal):
                return p

            res = await evo.maintain(
                {"user": f"Run task round {r}", "assistant": "Done"},
                _propose,
                permission_mode="acceptEdits",
                event_id=f"suite-b-{r}",
                target_snapshot=snap,
                validation_gate=gate,
            )
            if res.get("failure_kind") == "focus_item_violation":
                focus_violations_blocked += 1
            elif res.get("status") == "rejected":
                regressions_or_ties_blocked += 1
            elif res.get("status") == "updated":
                promotions += 1
            vrep = res.get("validation_report") or {}
            cache_hits += vrep.get("cache", {}).get("hits", 0)
            cache_misses += vrep.get("cache", {}).get("misses", 0)

        final_snap = skills.load("strict-audit")
        weyra_focus_retention = sum(c in final_snap.body for c in expert_clauses) / len(expert_clauses)
        rejected_count = len(evo.rejected_edits("strict-audit", limit=50))
        session.close()

    ok = (
        weyra_focus_retention == 1.0
        and baseline_focus_retention < 1.0
        and focus_violations_blocked == 6
        and promotions == 1
        and regressions_or_ties_blocked == 23
        and rejected_count > 0
        and judge_spy.calls == 0
        and cache_hits > 0
    )
    return {
        "passed": ok,
        "rounds": rounds,
        "baseline": {"focus_retention": baseline_focus_retention},
        "weyra_guarded": {
            "focus_retention": weyra_focus_retention,
            "promotions": promotions,
            "focus_violations_blocked": focus_violations_blocked,
            "regressions_or_ties_blocked": regressions_or_ties_blocked,
            "rejected_edit_buffer_entries": rejected_count,
            "judge_llm_calls": judge_spy.calls,
            "validation_cache_hits": cache_hits,
            "validation_cache_misses": cache_misses,
        },
    }


async def _run_suite_c() -> dict:
    """Suite C: 全真运行时 SQLite 只读审计 (Clean vs Error-Loop Traces) 与技能激活跟踪。"""
    with tempfile.TemporaryDirectory(prefix="weyra-rel-c-") as tmp:
        base = Path(tmp)

        p_clean = base / "clean"
        p_clean.mkdir()
        (p_clean / "target.py").write_text("x = 1\n", encoding="utf-8")
        sd1 = p_clean / ".weyra" / "skills" / "code-hygiene"
        sd1.mkdir(parents=True)
        (sd1 / "SKILL.md").write_text(
            "---\nname: code-hygiene\nversion: 1.0.0\ndescription: Safe editing\n---\nInspect before edit and verify after.\n",
            encoding="utf-8",
        )
        rt1 = _make_runtime([
            _resp(calls=[ToolCall("c1", "skill", {"name": "code-hygiene"})]),
            _resp(calls=[ToolCall("c2", "read_file", {"path": "target.py"})]),
            _resp(calls=[ToolCall("c3", "edit_file", {"path": "target.py", "old_text": "x = 1", "new_text": "x = 2"})]),
            _resp(calls=[ToolCall("c4", "read_file", {"path": "target.py"})]),
            _resp("Updated target.py and verified."),
        ])
        a1 = Agent(p_clean, rt1, permission_mode="bypassPermissions", enable_memory=False, enable_evolution=False)
        await a1.run_once("Update target.py safely.")
        sid1 = a1.session.session_id
        await a1.close()
        rep_clean = audit_session_db(p_clean, sid1, task_passed=True)

        p_noisy = base / "noisy"
        p_noisy.mkdir()
        (p_noisy / "target.py").write_text("x = 1\n", encoding="utf-8")
        sd2 = p_noisy / ".weyra" / "skills" / "code-hygiene"
        sd2.mkdir(parents=True)
        (sd2 / "SKILL.md").write_text(
            "---\nname: code-hygiene\nversion: 1.0.0\ndescription: Safe editing\n---\nInspect before edit.\n",
            encoding="utf-8",
        )
        rt2 = _make_runtime([
            _resp(calls=[ToolCall("l1", "edit_file", {"path": "target.py", "old_text": "missing", "new_text": "x = 2"})]),
            _resp(calls=[ToolCall("l2", "edit_file", {"path": "target.py", "old_text": "missing", "new_text": "x = 2"})]),
            _resp(calls=[ToolCall("l3", "read_file", {"path": "target.py"})]),
            _resp(calls=[ToolCall("l4", "edit_file", {"path": "target.py", "old_text": "x = 1", "new_text": "x = 2"})]),
            _resp("All tests passed and verified."),
        ])
        a2 = Agent(p_noisy, rt2, permission_mode="bypassPermissions", enable_memory=False, enable_evolution=False)
        await a2.run_once("Update target.py safely.")
        sid2 = a2.session.session_id
        await a2.close()
        rep_noisy = audit_session_db(p_noisy, sid2, task_passed=True)

    ok = (
        rep_clean.process.completed is True
        and rep_clean.process.error_loops == 0
        and rep_clean.process.verify_after_edit is True
        and rep_clean.skill_funnel.activated == ("code-hygiene",)
        and rep_clean.reliability.reliable is True
        and rep_noisy.process.error_loops >= 1
        and rep_noisy.process.unverified_claim is True
        and rep_noisy.skill_funnel.activated == ()
    )
    return {
        "passed": ok,
        "clean": rep_clean.to_dict(),
        "noisy": rep_noisy.to_dict(),
    }


async def run_all(output_path: Path | None = DEFAULT_OUTPUT) -> dict:
    suite_a = await _run_suite_a()
    suite_b = await _run_suite_b()
    suite_c = await _run_suite_c()
    summary = {
        "mode": "offline_deterministic_zero_api",
        "paid_api_calls": 0,
        "references": {
            "suite_b_continual_30_round": [
                "Microsoft SkillOpt (NeurIPS 2026 Spotlight, arXiv:2605.23904, MIT)",
                "SkillDiff (arXiv:2611.25534, MIT)",
            ],
        },
        "suite_a_fault_injection": suite_a,
        "suite_b_continual_30_round": suite_b,
        "suite_c_trace_audit": suite_c,
        "all_passed": bool(suite_a["passed"] == suite_a["total"] and suite_b["passed"] and suite_c["passed"]),
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Weyra zero-API reliability and skill anti-degradation suite.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="JSON output path")
    args = parser.parse_args()
    report = asyncio.run(run_all(args.out))
    sa = report["suite_a_fault_injection"]
    sb = report["suite_b_continual_30_round"]
    sc = report["suite_c_trace_audit"]
    print("| Suite | Key Metric | Result |")
    print("| --- | --- | --- |")
    print(f"| Suite A: Fault Injection (6 cases) | Passed | {sa['passed']}/{sa['total']} |")
    print(
        f"| Suite B: 30-Round Continual Evo | SkillDiff Focus Retention (Base vs Weyra) | "
        f"{sb['baseline']['focus_retention']:.0%} vs {sb['weyra_guarded']['focus_retention']:.0%} |"
    )
    print(
        f"| Suite B: SkillOpt Gate | Judge LLM Calls / Cache Hits | "
        f"{sb['weyra_guarded']['judge_llm_calls']} / {sb['weyra_guarded']['validation_cache_hits']} |"
    )
    print(
        f"| Suite C: TraceAuditor | Clean vs Noisy Activation | "
        f"clean({sc['clean']['skill_funnel']['activated']}) vs noisy({sc['noisy']['skill_funnel']['activated']}) |"
    )
    if not report["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

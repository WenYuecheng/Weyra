"""零 API 成本的双层 Harness 可靠性与 30 轮技能防退化确定性基准套件。

【评测设计对齐】
1. Suite A — Harness 故障注入矩阵 (6 项, 对标 Trial by Trace `arXiv:2610.32285`):
   在真实 SessionStore / ToolRuntime / Budget / EvolutionStore 上注入崩溃、篡改、TOCTOU 与预算耗尽故障。
2. Suite B — 30 轮连续演化防退化压测 (对标 SkillOpt / WikiSkill / SkillDiff `arXiv:2611.25534`):
   在真实 EvolutionStore + ConfiguredSkillGate + 确定性验证短路模式下，对比无护栏自由重写与 Weyra 护栏演化。
3. Suite C — 过程质量与技能转化漏斗审计 (对标 AgentLens `arXiv:2605.12925` & SkillPlumber `arXiv:2611.25429`):
   通过全真 CodingAgent + ScriptedProvider 驱动 Ideal 与 Lucky Pass 两条轨迹，调用 `audit_session_db` 校验分档。

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
from weyra.skill_evolution import EvolutionStore  # noqa: E402
from weyra.skill_judge import ConfiguredSkillGate  # noqa: E402
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
        skills1 = SkillStore(p1)
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
        recovered_evo = EvolutionStore(SkillStore(p1))
        statuses1 = [r.get("status") for r in recovered_evo.recovery_results]
        ok1 = (
            statuses1 == ["interrupted"]
            and SkillStore(p1).load("safe-rule").version == "1.0.0"
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
        skills3 = SkillStore(p3)
        evo3 = EvolutionStore(skills3)
        evo3.create("occ-skill", "Keep v1.", description="OCC", permission_mode="acceptEdits")
        stale_snap = skills3.load("occ-skill")
        # 外部篡改技能文件
        atomic(stale_snap.path, stale_snap.path.read_text(encoding="utf-8") + "\nExternal edit.")
        rejected3 = evo3.apply(
            "occ-skill",
            [{"op": "append", "text": "New clause"}],
            base_hash=stale_snap.content_hash,
            permission_mode="acceptEdits",
        )
        # 文件工具 OCC 测试：在同一 RunContext 下先 read_file 取得读版本，随后外部覆写文件，再尝试 write_file 必须被拦截
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
        store4 = SkillStore(p4)
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
            # 模拟审批等待期间策略被切回只读 plan 模式
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
        # 模拟重启并重新挂载：未核销的 70 tokens 必须被重放计入总账，阻止绕过
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
    """Suite B: 30 轮连续演化防退化压测 (Baseline vs Weyra Guarded, 0 LLM Judge)."""
    anchors = [
        "Always check source IDs against input files",
        "Never modify protected input paths",
        "Report exact JSON schema with summary and items keys",
        "Abstain and set status=incomplete when evidence is missing",
    ]
    initial_body = "\n".join(
        [f"- [ANCHOR] {clause}" for clause in anchors]
        + ["- Keep diagnostic reasoning concise and verifiable."]
    )

    # 1. 无护栏基线模拟：自由改写丢锚点 + 全局 10 条 FIFO 知识滑窗
    baseline_body = initial_body
    baseline_knowledge = [
        {"kind": "boundary", "text": f"Core boundary #{i}"} for i in range(1, 4)
    ]
    for r in range(1, 31):
        # 每 5 轮模型向自身先验坍缩，重写删掉一条专家锚点；同时每轮写入临时 observation
        if r % 6 == 0 and anchors[(r // 6 - 1) % len(anchors)] in baseline_body:
            target_anchor = anchors[(r // 6 - 1) % len(anchors)]
            baseline_body = baseline_body.replace(f"- [ANCHOR] {target_anchor}\n", "")
        baseline_knowledge.append({"kind": "observation", "text": f"Round {r} transient note"})
        baseline_knowledge = baseline_knowledge[-10:]
    baseline_anchor_retention = sum(a in baseline_body for a in anchors) / len(anchors)
    baseline_boundary_retention = sum(
        1 for item in baseline_knowledge if item["kind"] == "boundary"
    ) / 3

    # 2. Weyra Guarded：真实 EvolutionStore + ConfiguredSkillGate + 确定性验证短路 (0 Judge calls)
    rounds = 30
    with tempfile.TemporaryDirectory(prefix="weyra-rel-b-") as tmp:
        root = Path(tmp)
        skills = SkillStore(root)
        session = SessionStore(project=root, session_id="suite-b")
        evo = EvolutionStore(skills, session=session)
        evo.create(
            "strict-audit",
            initial_body,
            description="Auditable extraction protocol",
            when_to_use="When extracting structured JSON reports from project files",
            permission_mode="acceptEdits",
        )
        for i in range(1, 4):
            evo._save_note("strict-audit", {"kind": "boundary", "text": f"Core boundary #{i}"}, f"seed-{i}", "1.0.0")

        # 构建固定确定性验证清单 (deterministic_only=True -> 跳过 LLM Judge，0 裁判 API 调用)
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

        # 构造共享脚本化响应队列：
        # - development 用例正常写出 report.json（hard_failures=0），使门禁进入 validation 集对比
        # - validation 用例: v1.0.0 仅在 det-case-1 通过(1/2)；含 GOOD_SCHEMA_HINT 的候选 2/2 通过；REGRESS_BREAK 0/2 失败
        class _DynamicGateProvider(ScriptedProvider):
            async def stream(self, request, context):
                user_msg = "\n".join(m.content for m in request.messages if m.role == "user")
                if not self.responses:
                    if not any(m.role == "tool" for m in request.messages):
                        self.responses.append(_resp(calls=[ToolCall("sk", "skill", {"name": "strict-audit"})]))
                    else:
                        skill_text = "\n".join(m.content for m in request.messages if m.role == "tool")
                        if "ID-42" not in user_msg and "ID-99" not in user_msg:
                            payload = json.dumps({"summary": "ok", "items": []})
                            self.responses.append(_resp(calls=[ToolCall("wf", "write_file", {"path": "report.json", "content": payload})]))
                            self.responses.append(_resp("Completed dev task."))
                        elif "REGRESS_BREAK" in skill_text:
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
                raise AssertionError("Judge LLM should be short-circuited for deterministic cases")
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
        gate = ConfiguredSkillGate(runtime, budget_b, manifest, session=session)

        anchor_violations_blocked = 0
        regressions_blocked = 0
        promotions = 0
        cache_hits = 0
        cache_misses = 0

        for r in range(1, rounds + 1):
            snap = skills.load("strict-audit")
            # 模拟 4 类混杂演进提案：
            # A) 尝试删除受保护锚点 (r % 5 == 1)
            # B) 尝试引入退化规则 REGRESS_BREAK (r % 5 == 2 or 3)
            # C) 仅在第 5 轮提出真正有效补丁 GOOD_SCHEMA_HINT (r == 5)
            # D) 其余轮次提出无净增益的冗余修饰并附带瞬态 observation 笔记
            if r % 5 == 1:
                victim = anchors[(r // 5) % len(anchors)]
                proposal = {
                    "action": "apply",
                    "name": "strict-audit",
                    "edits": [{"op": "replace", "old": f"- [ANCHOR] {victim}", "new": ""}],
                    "reason": f"Round {r} prior collapse deletion",
                }
            elif r % 5 in {2, 3}:
                proposal = {
                    "action": "apply",
                    "name": "strict-audit",
                    "edits": [{"op": "append", "text": f"- REGRESS_BREAK round {r}"}],
                    "reason": f"Round {r} regressive edit",
                    "knowledge_note": {"kind": "observation", "text": f"Transient obs {r}"},
                }
            elif r == 5:
                proposal = {
                    "action": "apply",
                    "name": "strict-audit",
                    "edits": [{"op": "append", "text": "- GOOD_SCHEMA_HINT: always write report.json for all IDs."}],
                    "reason": "Fix case 2 missing report.json",
                    "knowledge_note": {"kind": "verified_pattern", "text": "Always emit report.json even on multi-ID batches."},
                }
            else:
                proposal = {
                    "action": "apply",
                    "name": "strict-audit",
                    "edits": [{"op": "append", "text": f"- Cosmetic hint #{r}."}],
                    "reason": f"Round {r} cosmetic edit",
                    "knowledge_note": {"kind": "observation", "text": f"Transient obs {r}"},
                }

            window_evidence = {
                "user": f"Run task round {r}",
                "assistant": "Done",
                "feedback": "",
                "replay": {
                    "turn_id": f"r-{r}",
                    "complete": True,
                    "reason": "",
                    "files": {"input.txt": f"round {r}"},
                    "written_paths": ["report.json"],
                    "context": [],
                    "system_instructions": "",
                },
            }
            vctx = gate.freeze(window_evidence)
            # 用固定验证集直接评测（跳过 saturated 早退以便完整压测 30 轮拦截力）
            async def _propose(_, p=proposal):
                return p

            # 临时绕过 saturated 以便把 30 轮攻击与噪音全部送到门禁与锚点守卫验证
            orig_sat = gate.saturated
            gate.saturated = lambda _old, _win: False
            try:
                res = await evo.maintain(
                    window_evidence,
                    _propose,
                    permission_mode="acceptEdits",
                    event_id=f"suite-b-{r}",
                    target_snapshot=snap,
                    validation_gate=gate,
                    validation_context=vctx,
                )
            finally:
                gate.saturated = orig_sat

            if res.get("failure_kind") == "anchor_violation":
                anchor_violations_blocked += 1
            elif res.get("status") == "rejected":
                regressions_blocked += 1
            elif res.get("status") == "updated":
                promotions += 1
            vrep = res.get("validation_report") or {}
            cache_hits += vrep.get("cache", {}).get("hits", 0)
            cache_misses += vrep.get("cache", {}).get("misses", 0)

        final_snap = skills.load("strict-audit")
        weyra_anchor_retention = sum(a in final_snap.body for a in anchors) / len(anchors)
        weyra_knowledge = evo.knowledge("strict-audit")
        weyra_boundary_retention = sum(
            1 for item in weyra_knowledge if item["kind"] == "boundary"
        ) / 3
        rejected_count = len(evo.rejected_edits("strict-audit", limit=50))
        session.close()

    ok = (
        weyra_anchor_retention == 1.0
        and baseline_anchor_retention < 1.0
        and weyra_boundary_retention == 1.0
        and baseline_boundary_retention == 0.0
        and anchor_violations_blocked == 6
        and promotions == 1
        and regressions_blocked == 23
        and rejected_count > 0
        and judge_spy.calls == 0
        and cache_hits > 0
    )
    return {
        "passed": ok,
        "rounds": rounds,
        "baseline": {
            "anchor_retention": baseline_anchor_retention,
            "boundary_knowledge_retention": baseline_boundary_retention,
        },
        "weyra_guarded": {
            "anchor_retention": weyra_anchor_retention,
            "boundary_knowledge_retention": weyra_boundary_retention,
            "promotions": promotions,
            "anchor_violations_blocked": anchor_violations_blocked,
            "regressions_or_no_gain_blocked": regressions_blocked,
            "rejected_edit_buffer_entries": rejected_count,
            "judge_llm_calls": judge_spy.calls,
            "replay_cache_hits": cache_hits,
            "replay_cache_misses": cache_misses,
        },
    }


async def _run_suite_c() -> dict:
    """Suite C: 全真运行时轨迹过程质量分档 (Ideal vs Lucky Pass) 与技能转化漏斗审计。"""
    with tempfile.TemporaryDirectory(prefix="weyra-rel-c-") as tmp:
        base = Path(tmp)
        rules = {"code-hygiene": ["read_file", "edit_file", "report.json"]}

        # 1. Ideal 轨迹：加载技能 -> 读取 -> 编辑 -> 复读校验 -> 报告
        p_ideal = base / "ideal"
        p_ideal.mkdir()
        (p_ideal / "target.py").write_text("x = 1\n", encoding="utf-8")
        sd1 = p_ideal / ".weyra" / "skills" / "code-hygiene"
        sd1.mkdir(parents=True)
        (sd1 / "SKILL.md").write_text("---\nname: code-hygiene\nversion: 1.0.0\ndescription: Safe editing\n---\nInspect before edit and verify after.\n", encoding="utf-8")
        rt1 = _make_runtime([
            _resp(calls=[ToolCall("c1", "skill", {"name": "code-hygiene"})]),
            _resp(calls=[ToolCall("c2", "read_file", {"path": "target.py"})]),
            _resp(calls=[ToolCall("c3", "edit_file", {"path": "target.py", "old_text": "x = 1", "new_text": "x = 2"})]),
            _resp(calls=[ToolCall("c4", "read_file", {"path": "target.py"})]),
            _resp("Updated target.py and verified report.json constraint."),
        ])
        a1 = Agent(p_ideal, rt1, permission_mode="bypassPermissions", enable_memory=False, enable_evolution=False)
        await a1.run_once("Update target.py safely.")
        sid1 = a1.session.session_id
        await a1.close()
        rep_ideal = audit_session_db(p_ideal, sid1, skill_rules=rules, task_passed=True)

        # 2. Lucky Pass 轨迹：不加载技能 -> 连续重复相同失败调用 -> 未校验即宣称已验证通过
        p_lucky = base / "lucky"
        p_lucky.mkdir()
        (p_lucky / "target.py").write_text("x = 1\n", encoding="utf-8")
        sd2 = p_lucky / ".weyra" / "skills" / "code-hygiene"
        sd2.mkdir(parents=True)
        (sd2 / "SKILL.md").write_text("---\nname: code-hygiene\nversion: 1.0.0\ndescription: Safe editing\n---\nInspect before edit.\n", encoding="utf-8")
        rt2 = _make_runtime([
            _resp(calls=[ToolCall("l1", "edit_file", {"path": "target.py", "old_text": "missing", "new_text": "x = 2"})]),
            _resp(calls=[ToolCall("l2", "edit_file", {"path": "target.py", "old_text": "missing", "new_text": "x = 2"})]),
            _resp(calls=[ToolCall("l3", "read_file", {"path": "target.py"})]),
            _resp(calls=[ToolCall("l4", "edit_file", {"path": "target.py", "old_text": "x = 1", "new_text": "x = 2"})]),
            _resp("All tests passed and verified."),
        ])
        a2 = Agent(p_lucky, rt2, permission_mode="bypassPermissions", enable_memory=False, enable_evolution=False)
        await a2.run_once("Update target.py safely.")
        sid2 = a2.session.session_id
        await a2.close()
        rep_lucky = audit_session_db(p_lucky, sid2, skill_rules=rules, task_passed=True)

    ok = (
        rep_ideal.process.tier == "ideal"
        and rep_ideal.skill_funnel.activated == ("code-hygiene",)
        and rep_ideal.skill_funnel.coverage_rate == 1.0
        and rep_ideal.reliability.reliable is True
        and rep_lucky.process.tier == "lucky_pass"
        and rep_lucky.process.error_loops >= 1
        and rep_lucky.process.unverified_claim is True
        and rep_lucky.skill_funnel.activated == ()
    )
    return {
        "passed": ok,
        "ideal": rep_ideal.to_dict(),
        "lucky_pass": rep_lucky.to_dict(),
    }


async def run_all(output_path: Path | None = DEFAULT_OUTPUT) -> dict:
    suite_a = await _run_suite_a()
    suite_b = await _run_suite_b()
    suite_c = await _run_suite_c()
    summary = {
        "mode": "offline_deterministic_zero_api",
        "paid_api_calls": 0,
        "paper_references": {
            "suite_a_fault_injection": ["Trial by Trace (arXiv:2610.32285)"],
            "suite_b_continual_30_round": [
                "SkillOpt (NeurIPS 2026 Spotlight, arXiv:2605.23904)",
                "WikiSkill (arXiv:2608.27454)",
                "SkillDiff (arXiv:2611.25534)",
                "SkillsBench (NeurIPS 2026, arXiv:2602.12670)",
                "SOPBench (EMNLP 2026 Main, 2026.emnlp-main.936)",
            ],
            "suite_c_trace_audit": [
                "AgentLens (NeurIPS 2026, arXiv:2605.12925)",
                "SkillPlumber (arXiv:2611.25429)",
                "Harness Updating Is Not Harness Benefit (EMNLP 2026 Main, 2026.emnlp-main.2326)",
                "Trial by Trace (arXiv:2610.32285)",
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
        f"| Suite B: 30-Round Continual Evo | Anchor Retention (Base vs Weyra) | "
        f"{sb['baseline']['anchor_retention']:.0%} vs {sb['weyra_guarded']['anchor_retention']:.0%} |"
    )
    print(
        f"| Suite B: 30-Round Continual Evo | Boundary Wiki Retention (Base vs Weyra) | "
        f"{sb['baseline']['boundary_knowledge_retention']:.0%} vs {sb['weyra_guarded']['boundary_knowledge_retention']:.0%} |"
    )
    print(
        f"| Suite B: Deterministic Gate | Judge LLM Calls / Cache Hits | "
        f"{sb['weyra_guarded']['judge_llm_calls']} / {sb['weyra_guarded']['replay_cache_hits']} |"
    )
    print(
        f"| Suite C: TraceAuditor | Tiers & Activation | "
        f"ideal({sc['ideal']['skill_funnel']['activated']}) vs lucky_pass({sc['lucky_pass']['skill_funnel']['activated']}) |"
    )
    if not report["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

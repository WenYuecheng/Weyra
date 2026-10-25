"""基于形式化用例组与单因素消融对比的可执行系统评测框架（Evaluation & Ablation Suite）。

架构定位与设计哲学:
    本模块为 Weyra 核心能力提供基于控制变量法（One-factor-at-a-time, OFAT）的工业级回归评测引擎。
    在现代 Agent 系统的性能与质量评估中，单纯依靠大模型打分或非受控的自然语言生成存在极高的方差与偏见。
    本模块通过 20 个标准测试用例（data/scenarios.json），对文件系统事实操作、受保护文件完整性、
    工具调用证据链以及物理产物（Artifact JSON）进行端到端硬断言验证。
    回答的自然语言文本（Answer Prose）不可作为通关依据，必须且仅基于实际工具调用产生的物理副作用判定。

开源框架借鉴与学术溯源:
    1. DeepSeek-Harness (benchmarks/ & packages/compaction):
       - 借鉴其单因素对比实验架构（Single-factor Groups）：
         * `cache`: 评估 Pi Agent / Anthropic 4-breakpoint 严格前缀对齐缓存命中（stable）对比无序重构（rebuild）对延迟与成本的影响。
         * `compression`: 深度对比三种上下文治理策略：简单无序修剪（plain）、DeepSeek-Harness
           八段式检查点摘要压缩（dsh），以及三层递进压缩架构（three_layer）。
         * `subtasks`: 评估独立派生子任务（spawn）、快照分叉子任务（fork）与单代理（single）的性能与隔离边界。
    2. Hermes Agent (agent/memory/):
       - 评测长期记忆检索（`memory`）：对比无记忆（absent）、基础基线（baseline）与确定性固化记忆（fixed）
         在多轮会话中的召回有效性。
    3. SkillOpt (arXiv:2605.23904, 2026) & WikiSkill (arXiv:2608.27454, 2026):
       - 评测技能自进化收敛性（`evolution`）：实证对比全文肆意盲目覆写（full_merge）与 SkillOpt 提出的
         有界差分演化（experience_bounded），验证在零退化门禁保护下的规则稳定性。
"""

from __future__ import annotations

import inspect
import asyncio
import math
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import sysconfig
import time

from .agent import Agent
from .online_skill_evolution import MAINTENANCE_INSTRUCTION
from .contracts import (
    Message,
    ModelInfo,
    ModelResponse,
    PreparedRequest,
    ToolCall,
    Usage,
    canonical,
    digest,
    new_id,
)
from .demos import OFFLINE_POLICY, OLD_RULE, NEW_RULE, scripted_runtime
from .model_runtime import ScriptedProvider
from .runtime_state import Budget, BudgetExceeded, RunContext
from .session import SessionStore
from .frontmatter import reject_symlinks

# 单因素对比评测分组定义（用于衡量各架构特性的独立边际收益）
GROUPS = {
    "cache": {"variants": ("stable", "rebuild"), "cases": ("P01", "P02")},
    "compression": {"variants": ("plain", "dsh", "three_layer"), "cases": ("P05", "W09")},
    "subtasks": {"variants": ("single", "spawn", "fork"), "cases": ("P07", "P08")},
    "memory": {"variants": ("absent", "baseline", "fixed"), "cases": ("W07", "W09")},
    "evolution": {
        "variants": ("full_merge", "experience_bounded"),
        "cases": ("W03", "W04", "W05", "W08"),
    },
}
STATUSES = ("pass", "fail", "error", "budget_stopped")


def load_cases():
    """加载标准的 20 个端到端回归评测基准用例。"""
    path = Path(__file__).resolve().parents[1] / "data" / "scenarios.json"
    if not path.exists():
        path = Path(sysconfig.get_path("data")) / "share" / "weyra" / "data" / "scenarios.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    if len(cases) != 20 or len({c["id"] for c in cases}) != 20:
        raise ValueError("Evaluation requires exactly twenty distinct scenarios")
    return cases



def check_case(case, project, answer, records):
    """Validate saved artifacts and actual tool facts; answer prose cannot pass."""
    project = Path(project).resolve()
    try:
        actual = json.loads((project / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        actual = None
    intents = {r["data"].get("call_id"): r["data"] for r in records if r["kind"] == "tool_intent"}
    succeeded = {
        r["data"].get("call_id")
        for r in records
        if r["kind"] == "tool_result" and r["data"].get("status") == "success"
    }
    read_paths = {
        str(data.get("arguments", {}).get("path"))
        for cid, data in intents.items()
        if cid in succeeded and data.get("name") == "read_file"
    }
    # Old tool logs use args; normalize without accepting merely announced reads.
    read_paths |= {
        str(data.get("args", {}).get("path"))
        for cid, data in intents.items()
        if cid in succeeded and data.get("name") == "read_file"
    }
    normalized_reads = set()
    for value in read_paths:
        path = (project / value).resolve()
        if path.is_relative_to(project):
            normalized_reads.add(str(path.relative_to(project)))
    result = {
        "artifact_json": actual == case["expected"],
        "source_evidence_read": set(case["reads"]) <= normalized_reads,
    }
    if "protected.txt" in case["fixtures"]:
        result["protected_file_unchanged"] = (
            _file_text(project / "protected.txt") == case["fixtures"]["protected.txt"]
        )
    for edit in case.get("edits", []):
        result["edit_" + edit["path"]] = edit["new_text"] in (
            _file_text(project / edit["path"]) or ""
        )
    return result


def _file_text(path):
    """安全读取文件 UTF-8 文本内容，若文件不存在或读取异常返回 None。"""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _reply(text="", calls=()):
    """构造评测模拟响应对象 (Mock ModelResponse)。"""
    return ModelResponse(text, tool_calls=tuple(calls), usage=Usage(20, 8))


def _task_script(case, suffix="", reread=False):
    """基于确定性测试 Case 构造完整的评测工具调用预设响应序列。"""
    reads = list(case["reads"]) + (["result.json"] if reread else [])
    responses = [
        _reply(
            calls=[
                ToolCall(f"read-{suffix}-{n}", "read_file", {"path": path})
                for n, path in enumerate(reads)
            ]
        )
    ]
    for index, edit in enumerate(case.get("edits", [])):
        if not reread:
            responses.append(_reply(calls=[ToolCall(f"edit-{suffix}-{index}", "edit_file", edit)]))
    responses.append(
        _reply(
            calls=[
                ToolCall(
                    f"write-{suffix}",
                    "write_file",
                    {"path": "result.json", "content": canonical(case["expected"])},
                )
            ]
        )
    )
    responses.append(_reply("Evidence: " + ", ".join(case["reads"])))
    return responses


def _offline_runtime(case, group, variant):
    """为特定单因素对比测试用例构建纯离线确定性模拟运行时。"""
    responses = _task_script(case)
    if group == "evolution":
        responses.insert(
            0, _reply(calls=[ToolCall("load-evolved-skill", "skill", {"name": "report-writing"})])
        )
    if group == "cache":
        responses += _task_script(case, "second", reread=True)
    if group == "subtasks" and variant != "single":
        responses = [
            _reply(calls=[ToolCall("child-read", "read_file", {"path": "source.json"})]),
            _reply(canonical(case["expected"])),
        ] + responses
    if group == "compression" and variant == "dsh":
        responses = [
            _reply("## Current Work\nPreserve protected.txt. Formal reports use conclusion first.")
        ] + responses
    runtime = scripted_runtime(responses)
    auxiliaries = {
        "compression": [
            _reply(
                canonical(
                    {"constraint": "preserve protected.txt", "preference": "conclusion first"}
                )
            )
            for _ in range(3)
        ]
        if variant == "three_layer"
        else [
            _reply(
                "## Current Work\n- Preserve protected.txt. Formal reports use conclusion first.\n## Next Step\n- Read fixture inputs and write result.json."
            )
        ],
        "memory": [_reply('["preference"]') for _ in range(5)],
        "evolution": [
            _reply(
                NEW_RULE
                if variant == "full_merge"
                else canonical(
                    {
                        "action": "apply",
                        "name": "report-writing",
                        "edits": [{"op": "replace", "old": OLD_RULE, "new": NEW_RULE}],
                        "knowledge_note": {
                            "kind": "boundary",
                            "text": "Stories follow narrative, based on user correction.",
                        },
                    }
                )
            )
        ],
    }
    if group == "compression" and variant == "plain":
        auxiliaries["compression"] = [
            _reply(
                canonical(
                    {
                        "episode_memory": {"constraint": "preserve protected.txt"},
                        "working_memory": {"preference": "conclusion first"},
                        "tool_memory": {},
                    }
                )
            )
        ]
    for purpose, script in auxiliaries.items():
        runtime.registry.register(purpose, ScriptedProvider(script))
        runtime.routes[purpose] = ModelInfo(purpose, "scripted", 200000, 1024, supports_cache=False)
    runtime.pricing = {
        f"{m.provider_id}/{m.model_id}": {
            "currency": "CNY",
            "input_per_million": 0,
            "output_per_million": 0,
        }
        for m in runtime.routes.values()
    }
    return runtime


async def _prepare_mechanism(agent, case, group, variant, runtime):
    """为消融对比实验组前置注入特定机制状态（如压缩历史、子任务派生或记忆经验）。"""
    metrics = {"branch": f"{group}/{variant}"}
    ctx = RunContext(agent.project, agent.session, agent.budget, "acceptEdits")
    if group == "compression":
        agent.session.add_message(
            Message("user", "Keep protected.txt unchanged. Formal reports use conclusion first.")
        )
        for _ in range(20):
            agent.session.add_message(Message("user", "Old discussion. " * 100))
            agent.session.add_message(Message("assistant", "Recorded this earlier discussion."))
        agent.session.add_message(Message("assistant", "Continue the fixture task."))
        before = len(canonical([m.to_dict() for m in agent.session.project()]))
        policy = dict(getattr(runtime, "context_policy", {}) or {})
        policy["summary_mode"] = {
            "plain": "single_json",
            "dsh": "dsh_checkpoint",
            "three_layer": "three_parallel",
        }[variant]
        from .context import ContextManager

        agent.context_manager = ContextManager(
            agent.session, runtime.resolve("main"), agent.context_manager.system, policy=policy
        )
        agent.context_manager.request(agent.tools.declarations())
        # A balanced minimal suffix gives every summarizer the same source history.
        metrics["compacted"] = await agent.context_manager.compact(runtime, ctx, trigger="overflow")
        metrics["summary_mode"] = policy["summary_mode"]
        metrics["context_policy"] = asdict(agent.context_manager.policy)
        metrics["before_chars"] = before
        metrics["after_chars"] = len(canonical([m.to_dict() for m in agent.session.project()]))
    elif group == "subtasks" and variant != "single":
        agent.session.add_message(Message("user", "Completed background: project Aurora."))
        agent.session.append("turn_end", {"id": "fixture-background", "state": "completed"})
        result = await agent.children.spawn(
            "Read source.json and return its factual fields; do not write files.", mode=variant
        )
        metrics["child_state"] = result["state"]
        metrics["child_usage"] = result.get("usage", 0)
        child = next(iter(agent.children._tasks.values()))["child"]
        metrics["seeded_messages"] = (
            sum(r["kind"] == "seed" for r in child.session.records) if child else 0
        )
    elif group == "memory":
        agent.enable_memory = variant == "fixed"
        agent.memory.create("preference", "正式汇报结论前置；技术记录保留30天。")
        if variant == "baseline":
            # Historical whitespace matching, against the same current facts.
            query = case["objective"]
            selected = [
                item
                for item in agent.memory.list()
                if any(term.lower() in item["content"].lower() for term in query.split())
            ]
            for item in selected[:5]:
                agent.session.add_message(
                    Message("user", "<memory>" + item["content"] + "</memory>", source="memory")
                )
            metrics["legacy_recall_injections"] = len(selected[:5])
        metrics["recall_mode"] = variant
    elif group == "evolution":
        agent.evolution.create(
            "report-writing",
            OLD_RULE,
            description="Formal reports and narrative boundaries",
            permission_mode="acceptEdits",
        )
        original = agent.skills.load("report-writing")
        if variant == "experience_bounded":
            agent.evolution._save_note(
                "report-writing",
                {
                    "kind": "boundary",
                    "text": "Prior user correction: formal reports give conclusions first; stories follow narrative.",
                },
                "fixture-prior-correction",
                original.version,
            )
        knowledge = agent.evolution.knowledge_prompt("report-writing")
        metrics["experience_given_to_maintenance"] = bool(knowledge)
        evidence = {
            "user": "For formal reports give the conclusion first. Stories should follow narrative; narrow report-writing.",
            "feedback": "The existing universal rule is too broad.",
        }

        async def propose(_):
            """演化模型提议调用闭包。"""
            instruction = (
                "Return only the complete revised skill body."
                if variant == "full_merge"
                else MAINTENANCE_INSTRUCTION
            )
            response = await runtime.complete(
                PreparedRequest(
                    runtime.resolve("evolution"),
                    instruction,
                    (
                        Message(
                            "user",
                            canonical(
                                {
                                    "body": original.body,
                                    "target": {
                                        "name": original.name,
                                        "content_hash": original.content_hash,
                                        "body": original.body,
                                    },
                                    "evidence": evidence,
                                    "knowledge": knowledge,
                                }
                            ),
                            source="evidence",
                        ),
                    ),
                    purpose="evolution",
                    parameters=runtime.parameters_for("evolution"),
                ),
                ctx,
            )
            if response.stop_reason != "stop" or response.tool_calls:
                raise ValueError("Maintenance requires a complete text-only response")
            return response.content if variant == "full_merge" else json.loads(response.content)

        if variant == "full_merge":
            # Evaluation-only complete merge in this fresh sandbox. Production maintenance
            # continues to use EvolutionStore's bounded publication path.
            body = await propose(evidence)
            if not isinstance(body, str) or not body.strip() or "---" in body:
                raise ValueError("Invalid isolated full-merge proposal")
            text = original.path.read_text(encoding="utf-8")
            original.path.write_text(text.replace(original.body, body), encoding="utf-8")
            metrics["proposal_applied"] = True
        else:
            result = await agent.evolution.maintain(
                evidence, propose, permission_mode="acceptEdits", target_snapshot=original
            )
            metrics["proposal_applied"] = result.get("status") == "updated"
        updated = agent.skills.load("report-writing")
        metrics.update(
            body_before_chars=len(original.body),
            body_after_chars=len(updated.body),
            boundary_saved=bool(agent.evolution.knowledge("report-writing")),
            revised_body=updated.body,
        )
    return metrics


async def _run_case(case, *, group, variant, root, runtime, budget, offline, timeout_seconds=180):
    """在隔离临时沙箱中执行单个基准用例并校验其物理产物事实。"""
    started = time.monotonic()
    folder = root / (case["id"] + "-" + new_id()[:10])
    reject_symlinks(folder)
    folder.mkdir(parents=True)
    for filename, content in case["fixtures"].items():
        path = folder / filename
        if not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError("Fixture path escapes case directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    case_runtime = _offline_runtime(case, group, variant) if offline else runtime
    agent = None
    metrics = {}
    calls_before, tokens_before = budget.calls, budget.total_tokens
    cost_before = getattr(budget, "total_cost_cny", 0)
    result = {
        "id": case["id"],
        "group": group,
        "variant": variant,
        "status": "error",
        "checks": {},
        "fixture_project": str(folder),
    }
    try:
        agent = Agent(
            folder,
            case_runtime,
            budget=budget,
            permission_mode="acceptEdits",
            enable_memory=False,
            enable_evolution=False,
            owns_runtime=False,
            max_steps=12,
        )
        async with asyncio.timeout(timeout_seconds):
            metrics = await _prepare_mechanism(agent, case, group, variant, case_runtime)
            objective = case["objective"]
            if group == "evolution":
                objective = (
                    "First load report-writing with the skill tool and apply its appropriate scope.\n"
                    + objective
                )
            answer = await agent.run_once(objective)
            if group == "cache":
                initial = digest(
                    {"system": agent.context_manager.system, "tools": agent.tools.declarations()}
                )
                if variant == "rebuild":
                    agent.context_manager.refresh(
                        agent.context_manager.system
                        + "\nDynamic runtime history fingerprint: "
                        + digest([message.to_dict() for message in agent.session.project()])
                    )
                await agent.run_once(
                    "Recheck the same evidence and result.json, then rewrite result.json with the same factual fields."
                )
                metrics["prefix_stable"] = initial == digest(
                    {"system": agent.context_manager.system, "tools": agent.tools.declarations()}
                )
        records = agent.session.records
        checks = check_case(case, folder, answer, records)
        if group == "compression":
            checks["compression_branch"] = (
                metrics["compacted"] and metrics["after_chars"] < metrics["before_chars"]
            )
        if group == "subtasks" and variant != "single":
            checks["child_completed"] = metrics["child_state"] == "completed"
            checks["fork_seed_boundary"] = metrics["seeded_messages"] == int(variant == "fork")
        if group == "cache":
            checks["cache_branch"] = metrics["prefix_stable"] == (variant == "stable")
        if group == "memory":
            metrics["recall_injections"] = sum(
                r["data"].get("injected", 0) for r in records if r["kind"] == "memory_recall"
            )
            checks["memory_branch"] = (
                metrics["recall_injections"] > 0
                if variant == "fixed"
                else metrics["recall_injections"] == 0
            )
        if group == "evolution":
            checks["updated_skill_loaded"] = any(
                record["kind"] == "tool_result"
                and record["data"].get("name") == "skill"
                and record["data"].get("status") == "success"
                and isinstance(record["data"].get("structured"), dict)
                and record["data"]["structured"].get("instructions") == metrics["revised_body"]
                for record in records
            )
            checks["proposal_applied"] = metrics["proposal_applied"]
            checks["narrative_exception"] = (
                any(
                    term in metrics["revised_body"].lower()
                    for term in ("故事", "小说", "叙事", "stor", "narrative", "fiction")
                )
            )
            if variant == "experience_bounded":
                checks["experience_saved"] = metrics["boundary_saved"]
        result.update(
            status="pass" if all(checks.values()) else "fail",
            checks=checks,
            metrics=metrics,
            answer=answer,
            session_id=agent.session.session_id,
        )
    except BudgetExceeded as error:
        result.update(status="budget_stopped", reason=str(error))
    except Exception as error:
        result.update(
            status="error",
            error=type(error).__name__,
            reason=f"Case exceeded {timeout_seconds}s"
            if isinstance(error, TimeoutError)
            else str(error)[:300],
            timed_out=isinstance(error, TimeoutError),
        )
    finally:
        if agent is not None:
            usage = [r["data"] for r in agent.session.records if r["kind"] == "model_usage"]
            # Child sessions share the root budget but keep their own usage journals.
            for entry in agent.children._tasks.values():
                child = entry.get("child")
                if child:
                    usage += [
                        r["data"] for r in child.session.records if r["kind"] == "model_usage"
                    ]
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            ):
                values = [item.get(field) for item in usage]
                metrics[field] = (
                    sum(values) if values and all(value is not None for value in values) else None
                )
            metrics["usage_samples"] = len(usage)
            metrics["cost_known"] = bool(usage) and all(
                item.get("cost_known") is True for item in usage
            )
            metrics["usage_by_purpose"] = {
                purpose: {
                    "calls": len(samples),
                    **{
                        field: sum(item[field] for item in samples)
                        if all(item.get(field) is not None for item in samples)
                        else None
                        for field in (
                            "input_tokens",
                            "output_tokens",
                            "cache_read_tokens",
                            "cache_write_tokens",
                        )
                    },
                }
                for purpose in sorted({item.get("purpose", "main") for item in usage})
                for samples in [[item for item in usage if item.get("purpose", "main") == purpose]]
            }
            latencies = [
                item["first_text_seconds"]
                for item in usage
                if item.get("first_text_seconds") is not None
            ]
            metrics["first_token_seconds"] = (
                sum(latencies) / len(latencies) if latencies and not offline else None
            )
            metrics["request_latencies"] = (
                [
                    {
                        "attempt_id": item.get("attempt_id"),
                        "purpose": item.get("purpose"),
                        "state": item.get("state"),
                        "first_text_seconds": item.get("first_text_seconds"),
                        "duration_seconds": item.get("request_duration_seconds"),
                    }
                    for item in usage
                ]
                if not offline
                else []
            )
            metrics["parent_context_chars"] = len(
                canonical([m.to_dict() for m in agent.session.project()])
            )
            result["metrics"] = metrics
            if not result["checks"]:
                result["checks"] = check_case(case, folder, "", agent.session.records)
        try:
            if agent is not None:
                await agent.close(close_runtime=False)
        finally:
            if offline:
                await case_runtime.close()
    result.update(
        model_calls=budget.calls - calls_before,
        accounted_tokens=budget.total_tokens - tokens_before,
        cost_cny=str(getattr(budget, "total_cost_cny", 0) - cost_before),
        elapsed_seconds=round(time.monotonic() - started, 4),
    )
    return result


def _summary(cases, *, eligible=20):
    """汇总一组测试用例的执行状态计数、成功率与成本。"""
    counts = {status: sum(c["status"] == status for c in cases) for status in STATUSES}
    return {
        "denominator": 20,
        "eligible_denominator": eligible,
        "executed": len(cases),
        "counts": counts,
        "success_rate": counts["pass"] / 20,
        "eligible_success_rate": counts["pass"] / eligible if eligible else 0,
        "cases": cases,
        "cost_cny": str(sum((Decimal(str(c.get("cost_cny", 0))) for c in cases), Decimal(0))),
    }


async def run_evaluation(
    *,
    offline=True,
    runtime=None,
    budget=None,
    project=None,
    groups=None,
    on_group_complete=None,
    resume=True,
    owns_runtime=True,
    run_id=None,
    case_timeout_seconds=180,
):
    """Run baseline then five independent mechanism comparisons, with no product grid.

    The caller supplies live runtime/credentials and a shared root Budget. Every
    outcome commits to the controller session before the group callback. Return
    False from an async/sync callback to stop before the next group.
    """
    if (
        isinstance(case_timeout_seconds, bool)
        or not isinstance(case_timeout_seconds, (int, float))
        or not math.isfinite(case_timeout_seconds)
        or case_timeout_seconds <= 0
    ):
        raise ValueError("Case timeout must be positive finite seconds")
    if not offline and runtime is None:
        raise ValueError("Live evaluation requires a constructed runtime")
    if not offline and budget is None:
        raise ValueError("Live evaluation requires the shared root budget")
    if offline and runtime is not None:
        raise ValueError("Offline evaluation does not accept a live runtime")
    chosen = list(GROUPS) if groups is None else list(groups)
    if any(name not in GROUPS for name in chosen):
        raise ValueError("Unknown mechanism group")
    cases = load_cases()
    root = Path(project or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    budget = budget if budget is not None else Budget()
    route = (
        "offline"
        if offline
        else canonical({key: vars(value) for key, value in runtime.routes.items()})
    )
    identity = digest(
        {
            "version": 3,
            "case_timeout_seconds": case_timeout_seconds,
            "implementation": digest(
                {
                    path.name: digest(path.read_text(encoding="utf-8"))
                    for path in sorted(Path(__file__).resolve().parent.glob("*.py"))
                }
            ),
            "manifest": cases,
            "mode": route,
            "groups": {name: GROUPS[name] for name in chosen},
            "parameters": None
            if offline
            else {key: runtime.parameters_for(key) for key in runtime.routes},
            "policy": OFFLINE_POLICY if offline else getattr(runtime, "context_policy", None),
            "pricing": None if offline else getattr(runtime, "pricing", None),
        }
    )
    run_id = (run_id + "-" + identity[:12]) if run_id else identity[:20]
    controller = SessionStore(project=root, session_id="evaluation-" + run_id)
    attached_here = getattr(budget, "_journal", None) is None
    calls_before = budget.calls
    report = {
        "run_id": run_id,
        "mode": "offline" if offline else "live",
        "quality_claim": False,
        "scope": "deterministic fixture checks; offline validates behavior only",
        "groups": {},
        "resumed_cases": 0,
        "session_id": controller.session_id,
    }
    try:
        budget.attach(controller)
        calls_before = budget.calls
        prior = (
            {
                r["data"]["key"]: r["data"]["outcome"]
                for r in controller.records
                if r["kind"] == "evaluation_case"
            }
            if resume
            else {}
        )
        stopped = False

        async def execute(group, variant, eligible):
            """执行单个消融变体在可用用例集上的批量评测并更新审计会话。"""
            nonlocal stopped
            outcomes = []
            for case in eligible:
                key = f"{group}/{variant}/{case['id']}"
                if key in prior and prior[key]["status"] != "budget_stopped":
                    outcome = prior[key]
                    report["resumed_cases"] += 1
                elif stopped:
                    outcome = {
                        "id": case["id"],
                        "group": group,
                        "variant": variant,
                        "status": "budget_stopped",
                        "reason": "evaluation stopped before group",
                        "checks": {},
                    }
                else:
                    controller.append("evaluation_case_start", {"key": key})
                    outcome = await _run_case(
                        case,
                        group=group,
                        variant=variant,
                        root=root / ".weyra" / "evaluation-fixtures" / run_id / group / variant,
                        runtime=runtime,
                        budget=budget,
                        offline=offline,
                        timeout_seconds=case_timeout_seconds,
                    )
                    controller.append("evaluation_case", {"key": key, "outcome": outcome}, True)
                    if outcome["status"] == "budget_stopped":
                        stopped = True
                outcomes.append(outcome)
                if (
                    key not in prior
                    and stopped
                    and outcome.get("reason") == "evaluation stopped before group"
                ):
                    controller.append("evaluation_case", {"key": key, "outcome": outcome}, True)
            return _summary(outcomes, eligible=len(eligible))

        report["baseline"] = await execute("baseline", "standard", cases)
        controller.append(
            "evaluation_group", {"name": "baseline", "report": report["baseline"]}, True
        )
        if on_group_complete:
            value = on_group_complete({"name": "baseline", **report["baseline"]})
            if inspect.isawaitable(value):
                value = await value
            stopped |= value is False
        for group in chosen:
            specification = GROUPS[group]
            eligible = [case for case in cases if case["id"] in specification["cases"]]
            variants = {}
            for variant in specification["variants"]:
                variants[variant] = await execute(group, variant, eligible)
            summary = {
                "factor": group,
                "one_factor": True,
                "eligible_cases": list(specification["cases"]),
                "variants": variants,
            }
            report["groups"][group] = summary
            controller.append("evaluation_group", {"name": group, "report": summary}, True)
            if on_group_complete:
                value = on_group_complete({"name": group, **summary})
                if inspect.isawaitable(value):
                    value = await value
                stopped |= value is False
        report.update(
            complete=not stopped
            and all(
                item["status"] in {"pass", "fail", "error"} for item in report["baseline"]["cases"]
            ),
            identity=identity,
            new_model_calls=budget.calls - calls_before,
            total_tokens=budget.total_tokens,
            total_cost_cny=str(getattr(budget, "total_cost_cny", 0)),
            budget_stopped=stopped,
        )
        controller.append("evaluation_report", report, True)
        return report
    finally:
        if attached_here and getattr(budget, "_journal", None) is controller:
            budget._journal = None
        controller.close()
        if runtime is not None and owns_runtime:
            await runtime.close()

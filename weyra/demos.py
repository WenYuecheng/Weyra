"""生产级高保真可复现演示场景（Reproducible Task Scenarios）。

架构定位与设计哲学:
    本模块提供两个具有高度确定性与自包含特征的 Agent 任务演示用例（analysis 分析场景与 writing 写作场景）。
    区别于传统依赖真实大模型、受网络波动与生成随机性干扰的脆弱 Demo，本模块采用“全真运行时+脚本化驱动”
    （Full Production Engine with Scripted Provider）的先进测试架构。
    执行代码完全走生产级 Agent 核心主循环、上下文管理器、SQLite 会话存储、权限栅栏与预算核算账本，
    仅将最外层的网络模型请求替换为确定性响应序列，兼顾 100% 可重复性（Reproducibility）与生产级路径覆盖。

开源框架借鉴与学术溯源:
    1. DeepSeek-Harness (packages/test-support & packages/bundle):
       - 借鉴其确定性脚本化运行时设计（Scripted Runtime & Replay）：通过 ScriptedProvider 预注入
         模型推演与工具调用（ToolCall）序列，实现离线、零成本、亚秒级的全链路逻辑回归。
       - 验证 Subagent 派生委托：在 analysis 场景中完整验证主 Agent 发起 fork 子代理委托任务、
         子代理读取文件提取证据并将结论回传主代理的完整闭环。
    2. SkillOpt (arXiv:2605.23904, 2026) & WikiSkill (arXiv:2608.27454, 2026):
       - 验证持续技能自进化闭环：在 writing 场景中完整重现“用户初始指导 -> 规则创建 -> 用户提出叙事例外纠错 ->
         触发 SkillOpt 边界补丁演化（replace: OLD_RULE -> NEW_RULE）”的完整交互生命周期。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from .agent import Agent
from .contracts import ModelInfo, ModelResponse, ToolCall, Usage
from .model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from .frontmatter import reject_symlinks

# 离线模拟策略：限制紧凑的 Token 上限
OFFLINE_POLICY = {"headroom_tokens": 1024, "max_tokens": 1024, "retain_tokens": 256}
OLD_RULE = "所有写作先给结论。"
NEW_RULE = "正式汇报先给结论；故事遵循叙事需要。"


def scripted_runtime(responses, *, maintenance=None):
    """构建注入确定性脚本响应序列的离线测试运行时。

    Args:
        responses: 主模型响应序列列表。
        maintenance: 可选的演化专用模型响应序列列表。

    Returns:
        ModelRuntime: 配置完毕的真实运行时实例。
    """
    registry = ProviderRegistry()
    registry.register("offline", ScriptedProvider(responses))
    model = ModelInfo("offline", "scripted", 200000, 1024)
    routes = {"main": model}
    if maintenance is not None:
        registry.register("maintenance", ScriptedProvider(maintenance))
        routes["evolution"] = ModelInfo("maintenance", "scripted", 200000, 1024)
    runtime = ModelRuntime(registry, routes)
    runtime.context_policy = dict(OFFLINE_POLICY)
    return runtime



def _response(text="", calls=()):
    """构造用于离线演示测试的预设模型响应对象 (Scripted ModelResponse)。"""
    return ModelResponse(text, tool_calls=tuple(calls), usage=Usage(12, 4))


def _offline_runtime(scenario):
    """根据演示场景构建离线预设脚本运行时 (Offline Mock ModelRuntime)。"""
    if scenario == "analysis":
        return scripted_runtime(
            [
                _response(calls=[ToolCall("read-readme", "read_file", {"path": "README.md"})]),
                _response("项目入口为 app.py，依据 README.md。"),
                _response(
                    calls=[
                        ToolCall(
                            "delegate",
                            "agent",
                            {"mode": "fork", "task": "读取 app.py 并解释入口函数。"},
                        )
                    ]
                ),
                _response(calls=[ToolCall("read-app", "read_file", {"path": "app.py"})]),
                _response("render_report(data) 将输入转为字符串；证据 app.py。"),
                _response("已结合 README.md 和子任务结果完成入口分析。"),
            ]
        )
    create = {
        "action": "create",
        "name": "report-writing",
        "description": "正式汇报写作规则",
        "body": OLD_RULE,
    }
    patch = {
        "action": "apply",
        "name": "report-writing",
        "reason": "用户指出叙事例外",
        "edits": [{"op": "replace", "old": OLD_RULE, "new": NEW_RULE}],
        "when_to_use": "正式汇报；不适用于故事创作",
        "knowledge_note": {"kind": "boundary", "text": "用户明确纠正：故事不套用汇报结构。"},
    }
    return scripted_runtime(
        [
            _response("结论：项目可交付。依据如下。"),
            _response("已记录你对正式汇报的持续要求。"),
            _response("故事从人物和冲突展开。"),
        ],
        maintenance=[
            _response(json.dumps(create, ensure_ascii=False)),
            _response(json.dumps(patch, ensure_ascii=False)),
        ],
    )


class _DemoJudge(ScriptedProvider):
    """Scripted batch judgments over observed demo outputs; no quality claim."""

    async def stream(self, request, context):
        cases = json.loads(request.messages[0].content)
        judgments = {}
        for case_id, case in cases.items():
            scores = {alias: {key: True for key in
                      ("correctness", "constraint_adherence", "preservation", "clarity")}
                      for alias in ("A", "B")}
            for alias in scores:
                scores[alias]["constraint_adherence"] = not case[alias]["final"].startswith("结论：")
            totals = {alias: sum(row.values()) for alias, row in scores.items()}
            winner = "tie" if totals["A"] == totals["B"] else "A" if totals["A"] > totals["B"] else "B"
            judgments[case_id] = {"scores": scores, "winner": winner,
                "evidence": {alias: "Observed scripted output: " + case[alias]["final"]
                             for alias in ("A", "B")}}
        self.responses.append(_response(json.dumps({"cases": judgments}, ensure_ascii=False)))
        async for event in super().stream(request, context):
            yield event


async def run_demo(
    scenario="analysis",
    provider=None,
    offline=True,
    project=None,
    *,
    runtime=None,
    budget=None,
    owns_runtime=True,
):
    """Return an inspectable report; live execution requires a caller-built runtime.

    ``provider`` is a display label only. Credential loading belongs to the CLI.
    A supplied project is used as a parent for a fresh isolated fixture folder.
    """
    if scenario not in {"analysis", "writing"}:
        raise ValueError("Unknown scenario; choose analysis or writing")
    if not offline and runtime is None:
        raise ValueError("Live demos require a constructed runtime")
    if offline and runtime is not None:
        raise ValueError("Do not supply a live runtime to an offline demo")
    if not offline and budget is None:
        raise ValueError("Live demos require the shared root budget")
    destination = Path(project or Path.cwd()).resolve() / ".weyra" / "demos"
    reject_symlinks(destination)
    destination.mkdir(parents=True, exist_ok=True)
    directory = tempfile.mkdtemp(prefix=f"{scenario}-", dir=destination)
    fixture = Path(directory)
    (fixture / "README.md").write_text(
        "Project Aurora: report generator; entry is app.py.\n", encoding="utf-8"
    )
    (fixture / "app.py").write_text(
        "def render_report(data):\n    return str(data)\n", encoding="utf-8"
    )
    runtime = _offline_runtime(scenario) if offline else runtime
    demo_gate = None
    if scenario == "writing" and offline:
        # The fixture is a pre-existing skill. Gated cold-start creation is unsupported.
        from .skills import SkillStore
        from .skill_evolution import EvolutionStore
        seed = EvolutionStore(SkillStore(fixture))
        seed_proposal = json.loads(runtime.registry.get("maintenance").responses[0].content)
        seed.create(seed_proposal["name"], seed_proposal["body"], description=seed_proposal["description"],
                    permission_mode="acceptEdits")
        # Replay both the captured user correction and an independent host task.
        # Each arm loads the real installed skill through the production tool.
        for _case in range(2):
            for answer in ("结论：这是一段故事。", "小舟推开门，故事从冲突展开。"):
                runtime.registry.get("offline").responses.extend([
                    _response(calls=[ToolCall("load-skill", "skill", {"name": seed_proposal["name"]})]),
                    _response(answer),
                ])
        runtime.registry.register("demo_judge", _DemoJudge())
        runtime.routes["skill_judge"] = ModelInfo("demo_judge", "gpt-5.6-luna", 200000, 1024)
        from .skill_judge import ConfiguredSkillGate
        demo_gate = ConfiguredSkillGate(runtime, None, {"cases": [{
            "id": "independent-story-opening",
            "prompt": "写一个陌生旅人在雨夜抵达渡口的故事开头，直接进入场景。",
            "files": {}, "output_mode": "text",
            "rubric": "从场景与冲突展开，不使用正式汇报的结论前言。",
        }]})
    agent = None
    calls_before = budget.calls if budget is not None else 0
    tokens_before = budget.total_tokens if budget is not None else 0
    try:
        agent = Agent(
            fixture,
            runtime,
            budget=budget,
            permission_mode="acceptEdits",
            enable_memory=False,
            enable_evolution=scenario == "writing",
            evolution_validation_gate=demo_gate,
            owns_runtime=False,
        )
        if scenario == "analysis":
            await agent.run_once("先读取 README.md，概括项目。")
            answer = await agent.run_once(
                "使用 agent 工具让 fork 子任务继承已完成背景，实际读取 app.py，再解释入口。"
            )
            tasks = agent.children.list()
            results = sum(r["kind"] == "tool_result" for r in agent.session.records)
            for item in agent.children._tasks.values():
                child = item.get("child")
                if child:
                    results += sum(r["kind"] == "tool_result" for r in child.session.records)
            report = {
                "answer": answer,
                "child_state": tasks[-1]["state"] if tasks else "not_created",
                "actual_tool_results": results,
            }
        else:
            await agent.run_once("以后所有写作都先给结论，这是我的长期写作偏好。")
            await agent.run_once("以后所有写作继续遵守结论先行的长期要求。")
            if agent._background:
                await asyncio.gather(*list(agent._background))
            # A maintenance model chooses the skill identity; the demo follows
            # that published identity rather than assuming its fixture name.
            created = [
                agent.skills.load(name)
                for name in agent.skills.capture_catalog()
                if any(
                    agent.skills.load(name).path.is_relative_to(directory)
                    for directory in (fixture / ".weyra" / "skills", fixture / "skills")
                )
            ]
            original = created[0] if created else None
            skill_name = original.name if original else "report-writing"
            await agent.run_once(
                f"更正 {skill_name} 中的长期写作偏好：结论先行今后仅适用于正式汇报；故事创作按叙事需要展开，"
                "不套用结论先行。以后持续遵守这一适用边界。"
            )
            if agent._background:
                await asyncio.gather(*list(agent._background))
            catalog = agent.skills.capture_catalog()
            updated = agent.skills.load(skill_name) if skill_name in catalog else None
            experiences = agent.evolution.knowledge(skill_name) if updated else []
            restored = (
                await agent.rollback_skill(skill_name, original.version)
                if updated and original
                else {}
            )
            report = {
                "skill_name": skill_name if updated else None,
                "updated_version": updated.version if updated else None,
                "updated_body": updated.body if updated else None,
                "boundary_experience": experiences,
                "rollback_version": restored.get("version"),
                "evolution_events": [
                    r["data"] for r in agent.session.records if r["kind"] == "evolution_result"
                ],
            }
        report.update(
            scenario=scenario,
            mode="offline" if offline else "live",
            provider=provider or runtime.resolve("main").provider_id,
            accounted_tokens=agent.budget.total_tokens - tokens_before,
            model_calls=agent.budget.calls - calls_before,
            paid_model_calls=0 if offline else agent.budget.calls - calls_before,
            model="scripted proposals; no quality claim"
            if offline
            else runtime.resolve("main").model_id,
            session_id=agent.session.session_id,
        )
        report["fixture_project"] = str(fixture)
        (fixture / ".weyra" / "demo-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report
    finally:
        if agent is not None:
            await agent.close(close_runtime=False)
        if runtime is not None and (offline or owns_runtime):
            await runtime.close()

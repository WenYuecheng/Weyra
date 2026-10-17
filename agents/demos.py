"""Two reproducible task scenarios using the same production Agent and runtime."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from .agent import Agent
from .contracts import ModelInfo, ModelResponse, ToolCall, Usage
from .model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from .frontmatter import reject_symlinks

OFFLINE_POLICY = {"headroom_tokens": 1024, "max_tokens": 1024, "retain_tokens": 256}
OLD_RULE = "所有写作先给结论。"
NEW_RULE = "正式汇报先给结论；故事遵循叙事需要。"


def scripted_runtime(responses, *, maintenance=None):
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
    return ModelResponse(text, tool_calls=tuple(calls), usage=Usage(12, 4))


def _offline_runtime(scenario):
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
            await agent.run_once("正式汇报以后都先给结论。")
            await agent.run_once("以后所有正式汇报继续遵守这一要求。")
            if agent._background:
                await asyncio.gather(*list(agent._background))
            # A maintenance model chooses the skill identity; the demo follows
            # that published identity rather than assuming its fixture name.
            created = [
                agent.skills.load(name)
                for name in agent.skills.capture_catalog()
                if agent.skills.load(name).path.is_relative_to(fixture / ".weyra" / "skills")
            ]
            original = created[0] if created else None
            skill_name = original.name if original else "report-writing"
            await agent.run_once(f"{skill_name} 需要收窄：故事创作不适用汇报结构。")
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

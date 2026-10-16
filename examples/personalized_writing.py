"""Demonstrate delayed feedback, bounded edits, experience and rollback offline."""

import asyncio
import json
import tempfile
from pathlib import Path

from weyra.agent_loop import Agent
from weyra.contracts import ModelInfo, ModelResponse, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider


async def main():
    with tempfile.TemporaryDirectory(prefix="weyra-writing-") as directory:
        main_provider = ScriptedProvider(
            [
                ModelResponse("结论：项目可交付。依据如下。", usage=Usage(10, 4)),
                ModelResponse("已记录你对正式汇报的持续要求。", usage=Usage(10, 4)),
                ModelResponse("故事从人物和冲突展开。", usage=Usage(10, 4)),
            ]
        )
        create = {
            "action": "create",
            "name": "report-writing",
            "description": "正式汇报写作规则",
            "body": "所有写作先给结论。",
            "reason": "示范待收窄的规则",
        }
        update = {
            "action": "apply",
            "name": "report-writing",
            "reason": "用户指出叙事例外",
            "edits": [
                {
                    "op": "replace",
                    "old": "所有写作先给结论。",
                    "new": "正式汇报先给结论；故事遵循叙事需要。",
                }
            ],
            "when_to_use": "正式汇报；不适用于故事创作",
            "knowledge_note": {"kind": "boundary", "text": "用户明确纠正：故事不套用汇报结构。"},
        }
        evolution_provider = ScriptedProvider(
            [
                ModelResponse(json.dumps(create, ensure_ascii=False), usage=Usage(20, 10)),
                ModelResponse(json.dumps(update, ensure_ascii=False), usage=Usage(20, 10)),
            ]
        )
        registry = ProviderRegistry()
        registry.register("main", main_provider)
        registry.register("maintenance", evolution_provider)
        runtime = ModelRuntime(
            registry,
            {
                "main": ModelInfo("main", "scripted", 100000, 1000),
                "evolution": ModelInfo("maintenance", "scripted", 100000, 1000),
            },
        )
        agent = Agent(Path(directory), runtime, permission_mode="acceptEdits", enable_memory=False)
        try:
            await agent.run_once("正式汇报以后都先给结论。")
            await agent.run_once("以后所有正式汇报继续遵守这一要求。")
            await asyncio.gather(*list(agent._background))
            await agent.run_once("report-writing 需要收窄：故事创作不适用汇报结构。")
            await asyncio.gather(*list(agent._background))
            updated = agent.skills.load("report-writing")
            experiences = agent.evolution.knowledge("report-writing")
            restored = agent.evolution.rollback(
                "report-writing", "1.0.0", permission_mode="acceptEdits"
            )
            print(
                json.dumps(
                    {
                        "updated_version": updated.version,
                        "updated_body": updated.body,
                        "boundary_experience": experiences,
                        "rollback_version": restored.get("version"),
                        "paid_model_calls": 0,
                        "model": "scripted proposals; no quality claim",
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            await agent.close()


if __name__ == "__main__":
    asyncio.run(main())

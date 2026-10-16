"""Reproducible file-evidence and context-fork demonstration; no network."""

import asyncio
import json
import tempfile
from pathlib import Path

from weyra.agent_loop import Agent
from weyra.contracts import ModelInfo, ModelResponse, ToolCall, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider


async def main():
    with tempfile.TemporaryDirectory(prefix="weyra-project-") as directory:
        project = Path(directory)
        (project / "README.md").write_text("Project Aurora: report generator; entry is app.py.")
        (project / "app.py").write_text("def render_report(data):\n    return str(data)\n")
        provider = ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=(ToolCall("read-readme", "read_file", {"path": "README.md"}),),
                    usage=Usage(10, 2),
                ),
                ModelResponse("项目入口为 app.py，依据 README。", usage=Usage(12, 4)),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "delegate",
                            "agent",
                            {"mode": "fork", "task": "读取 app.py 并解释入口函数。"},
                        ),
                    ),
                    usage=Usage(15, 3),
                ),
                ModelResponse(
                    tool_calls=(ToolCall("read-app", "read_file", {"path": "app.py"}),),
                    usage=Usage(12, 2),
                ),
                ModelResponse(
                    "render_report(data) 将输入转为字符串；证据 app.py。", usage=Usage(13, 4)
                ),
                ModelResponse("已结合 README 和子任务结果完成入口分析。", usage=Usage(16, 4)),
            ]
        )
        registry = ProviderRegistry()
        registry.register("demo", provider)
        model = ModelInfo("demo", "scripted", 100000, 1000)
        agent = Agent(project, ModelRuntime(registry, {"main": model}), enable_evolution=False)
        try:
            await agent.run_once("先读取 README，概括项目。")
            answer = await agent.run_once("让子任务继承已完成的背景，继续分析 app.py。")
            task = agent.children.list()[-1]
            results = sum(r["kind"] == "tool_result" for r in agent.session.records)
            for item in agent.children._tasks.values():
                child = item.get("child")
                if child:
                    results += sum(r["kind"] == "tool_result" for r in child.session.records)
            print(
                json.dumps(
                    {
                        "answer": answer,
                        "child_state": task["state"],
                        "actual_tool_results": results,
                        "accounted_tokens": agent.budget.total_tokens,
                        "paid_model_calls": 0,
                        "model": "scripted; no quality claim",
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            await agent.close()


if __name__ == "__main__":
    asyncio.run(main())

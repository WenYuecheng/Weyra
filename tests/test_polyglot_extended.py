import asyncio
import pytest
from benchmarks.polyglot.accounting import CampaignBudget
from weyra.core.state import BudgetExceeded
from benchmarks.polyglot.runner import bind_tools
from weyra.coding.agent import CodingAgent
from weyra.contracts import ModelInfo, ModelResponse, ToolCall, Usage
from weyra.core.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider


def test_no_host_tool_fallback_and_no_home_skills(tmp_path):
    class Box:
        def worker(self, op, args):
            assert op == "read_file"
            return {"content": "container content", "status": "success"}

    async def run():
        registry = ProviderRegistry()
        provider = ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=(ToolCall("x", "read_file", {"path": "x.py"}),), usage=Usage(1, 1)
                ),
                ModelResponse("done", usage=Usage(1, 1)),
            ]
        )
        registry.register("test", provider)
        model = ModelInfo("test", "test", 131072, 4096)
        runtime = ModelRuntime(registry, {"main": model})
        agent = CodingAgent(
            tmp_path,
            runtime,
            enable_memory=False,
            enable_evolution=False,
            permission_mode="bypassPermissions",
        )
        bind_tools(agent, Box())
        try:
            await agent.run_once("Read x.py")
            assert "container content" in str([m.to_dict() for m in agent.session.project()])
            assert not (tmp_path / "x.py").exists()
            assert set(agent.tools.specs) == {
                "read_file",
                "write_file",
                "edit_file",
                "list_files",
                "grep_search",
                "run_shell",
            }
        finally:
            await agent.close()

    asyncio.run(run())


def test_phase_caps_and_80_attempts():
    async def run():
        b = CampaignBudget()
        b.begin("dev", "check")
        r = await b.reserve(1, estimated_cost_cny="4")
        await b.settle(r, 1, actual_cost_cny="4")
        b.begin("other", "check")
        with pytest.raises(BudgetExceeded):
            await b.reserve(1, estimated_cost_cny=".01")
        b.begin("python/x")
        for _ in range(80):
            r = await b.reserve(1, estimated_cost_cny=".001")
            await b.settle(r, 1, actual_cost_cny=".001")
        with pytest.raises(BudgetExceeded):
            await b.reserve(1, estimated_cost_cny=".001")

    asyncio.run(run())

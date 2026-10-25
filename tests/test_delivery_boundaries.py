import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from weyra.contracts import Message, ModelInfo, PreparedRequest
from weyra.knowledge import MemoryStore
from weyra.runtime_state import Budget, RunContext
from weyra.session import SessionStore


class DeliveryTests(unittest.TestCase):
    def test_read_pool_reuses_a_slot_before_slowest_read_finishes(self):
        from weyra.contracts import ToolCall, ToolSpec
        from weyra.tool_runtime import ToolRuntime

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                session = SessionStore(Path(directory) / "s.jsonl", directory)
                runtime = ToolRuntime(directory, session, concurrency=3)
                fourth = asyncio.Event()

                async def read(args, ctx):
                    if args["index"] == 0:
                        await fourth.wait()
                    if args["index"] == 3:
                        fourth.set()
                    return str(args["index"])

                runtime.register(ToolSpec("read", "read", {"type": "object"}, read, "read", "read"))
                context = RunContext(Path(directory), session, Budget())
                await asyncio.wait_for(
                    runtime.execute_batch(
                        [ToolCall(str(n), "read", {"index": n}) for n in range(4)], context
                    ),
                    0.1,
                )
                self.assertTrue(fourth.is_set())
                session.close()

        asyncio.run(run())

    def test_resume_preserves_session_budget_spend(self):
        from weyra.agent import Agent
        from weyra.contracts import ModelResponse, Usage
        from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                registry = ProviderRegistry()
                registry.register(
                    "test", ScriptedProvider([ModelResponse("done", usage=Usage(8, 2))])
                )
                runtime = ModelRuntime(registry, {"main": ModelInfo("test", "test", 10000, 100)})
                agent = Agent(directory, runtime, enable_evolution=False, context_policy={"headroom_tokens":100,"max_tokens":100})
                await agent.run_once("one")
                session_id = agent.session.session_id
                await agent.close()
                resumed = Agent(directory, runtime, session_id=session_id, enable_evolution=False, context_policy={"headroom_tokens":100,"max_tokens":100})
                self.assertEqual(resumed.budget.total_tokens, 10)
                self.assertEqual(resumed.budget.calls, 1)
                await resumed.close()

        asyncio.run(run())

    def test_text_only_model_projects_existing_tool_history_as_observations(self):
        from weyra.context import ContextManager
        from weyra.contracts import ToolCall

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "s.jsonl", directory)
            store.add_message(Message("assistant", tool_calls=(ToolCall("one", "read", {}),)))
            store.add_message(Message("tool", "evidence", tool_call_id="one", name="read"))
            manager = ContextManager(
                store, ModelInfo("test", "text-only", 10000, 100, supports_tools=False), "rules", policy={"headroom_tokens":100,"max_tokens":100}
            )
            request = manager.request(({"name": "read", "parameters": {}},))
            self.assertEqual(request.tools, ())
            self.assertTrue(all(not m.tool_calls and m.role != "tool" for m in request.messages))
            self.assertIn("evidence", request.messages[-1].content)
            self.assertEqual(store.project()[-1].role, "tool")
            store.close()

    def test_cleanup_failure_does_not_skip_other_resources(self):
        from weyra.agent import Agent
        from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                registry = ProviderRegistry()
                registry.register("test", ScriptedProvider([]))
                runtime = ModelRuntime(registry, {"main": ModelInfo("test", "test", 10000, 100)})
                agent = Agent(directory, runtime, enable_evolution=False, context_policy={"headroom_tokens":100,"max_tokens":100})
                agent.mcp.close = AsyncMock(side_effect=OSError("close failed"))
                agent.jobs.close = AsyncMock()
                runtime.close = AsyncMock()
                with self.assertRaises(RuntimeError):
                    await agent.close()
                agent.jobs.close.assert_awaited_once()
                runtime.close.assert_awaited_once()
                self.assertTrue(agent.session.closed)

        asyncio.run(run())

    def test_request_baselines_are_deduplicated_and_expandable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "session.jsonl", directory)
            request = PreparedRequest(
                ModelInfo("test", "test", 10000, 100), "system", (Message("user", "question"),)
            )
            first = store.append("model_request", request.to_dict(), True)
            second = store.append("model_request", request.to_dict(), True)
            self.assertEqual(first["data"]["system_ref"], second["data"]["system_ref"])
            self.assertNotIn("system", first["data"])
            self.assertEqual(store.expand_request(first)["system"], "system")
            store.close()

    def test_model_memory_selector_cannot_inject_over_five(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                memory = MemoryStore(directory)
                for number in range(7):
                    memory.create(str(number), "中文项目事实")
                text = await memory.recall(
                    "项目", selector=AsyncMock(return_value=[str(n) for n in range(7)])
                )
                self.assertEqual(text.count("<memory id="), 5)

        asyncio.run(run())

    def test_auth_parameters_never_reach_request_log(self):
        from weyra.model_runtime import ModelError, ModelRuntime, ProviderRegistry, ScriptedProvider

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                store = SessionStore(Path(directory) / "s.jsonl", directory)
                model = ModelInfo("test", "test", 10000, 100)
                registry = ProviderRegistry()
                registry.register("test", ScriptedProvider([]))
                runtime = ModelRuntime(registry, {"main": model})
                request = PreparedRequest(
                    model, "", (), parameters={"api_key": "test-private-value"}
                )
                with self.assertRaises(ModelError):
                    await runtime.complete(request, RunContext(Path(directory), store, Budget()))
                self.assertNotIn("test-private-value", store.export_jsonl())
                store.close()

        asyncio.run(run())

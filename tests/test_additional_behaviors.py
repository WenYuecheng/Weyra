import asyncio
import tempfile
import unittest

from weyra.agent import Agent
from weyra.contracts import Message, ModelInfo, ModelResponse, PreparedRequest, Usage
from weyra.model_runtime import ModelError, ModelRuntime, ProviderRegistry, ScriptedProvider


class AdditionalTests(unittest.TestCase):
    def test_request_nested_values_are_immutable_snapshots(self):
        model = ModelInfo("test", "small", 10000, 100)
        request = PreparedRequest(
            model, "rules", (Message("user", "original"),), parameters={"temperature": 0}
        )
        fingerprint = request.fingerprint
        request.messages[0].content = "mutated"
        request.parameters["temperature"] = 2
        self.assertEqual(request.messages[0].content, "original")
        self.assertEqual(request.fingerprint, fingerprint)

    def test_goal_continuations_are_bounded(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                provider = ScriptedProvider([ModelResponse("work", usage=Usage(2, 1))] * 5)
                registry = ProviderRegistry()
                registry.register("test", provider)
                agent = Agent(
                    directory,
                    ModelRuntime(registry, {"main": ModelInfo("test", "test", 100000, 1000)}),
                    enable_evolution=False,
                )
                agent.set_goal("inspect remaining work")
                await agent.run_once("work on established goal")
                self.assertLessEqual(len(provider.requests), 4)
                self.assertEqual(agent.goal.state, "stopped")
                await agent.close()

        asyncio.run(run())

    def test_branch_restores_queue_goal_and_pending_feedback(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                provider = ScriptedProvider([ModelResponse("one"), ModelResponse("two")])
                registry = ProviderRegistry()
                registry.register("test", provider)
                agent = Agent(
                    directory,
                    ModelRuntime(registry, {"main": ModelInfo("test", "test", 100000, 1000)}),
                    enable_evolution=False,
                )
                await agent.run_once("first")
                point = agent.session.leaf
                await agent.run_once("second")
                agent.set_goal("later goal")
                agent.follow_up("later queued work")
                agent.branch(point)
                self.assertIsNone(agent.goal)
                self.assertEqual(agent._pending_window["user"], "first")
                self.assertEqual(agent._queues["follow_up"], [])
                await agent.close()

        asyncio.run(run())

    def test_explicit_failover_uses_new_model_without_repeating_input(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                first = ScriptedProvider([ModelError("transient")] * 3)
                second = ScriptedProvider([ModelResponse("fallback answer", usage=Usage(2, 1))])
                registry = ProviderRegistry()
                registry.register("first", first)
                registry.register("second", second)
                model = ModelInfo("first", "test", 100000, 1000)
                runtime = ModelRuntime(registry, {"main": model})
                runtime.fallbacks = {"main": [ModelInfo("second", "small", 100000, 500)]}
                agent = Agent(directory, runtime, enable_evolution=False)
                from unittest.mock import patch

                with patch(
                    "weyra.agent.asyncio.sleep",
                    new=__import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(),
                ):
                    self.assertEqual(await agent.run_once("only input"), "fallback answer")
                self.assertEqual(sum(m.content == "only input" for m in agent.session.project()), 1)
                self.assertEqual(second.requests[0].model.provider_id, "second")
                await agent.close()

        asyncio.run(run())

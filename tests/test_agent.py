import asyncio
import tempfile
import unittest
from pathlib import Path

from weyra.contracts import ModelInfo, ModelResponse, ToolCall, ToolSpec
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider


def scripted(responses):
    provider = ScriptedProvider(responses)
    registry = ProviderRegistry()
    registry.register("test", provider)
    runtime = ModelRuntime(registry, {"main": ModelInfo("test", "test", 100000, 1000)})
    return runtime, provider


class AgentTests(unittest.TestCase):
    def test_actual_tool_loop_keeps_request_prefix_stable(self):
        from weyra.agent import Agent

        async def run():
            with tempfile.TemporaryDirectory() as folder:
                Path(folder, "fixture.txt").write_text("evidence")
                runtime, provider = scripted(
                    [
                        ModelResponse(
                            tool_calls=(ToolCall("read", "read_file", {"path": "fixture.txt"}),)
                        ),
                        ModelResponse("found evidence"),
                        ModelResponse("next answer"),
                    ]
                )
                agent = Agent(folder, runtime, enable_evolution=False)
                self.assertEqual(await agent.run_once("read fixture"), "found evidence")
                self.assertEqual(await agent.run_once("continue"), "next answer")
                self.assertEqual(provider.requests[0].system, provider.requests[2].system)
                self.assertTrue(
                    any(
                        m.tool_call_id == "read" and m.content == "evidence"
                        for m in agent.session.project()
                    )
                )
                await agent.close()

        asyncio.run(run())

    def test_truncated_response_never_executes_valid_call(self):
        from weyra.agent import Agent

        async def run():
            with tempfile.TemporaryDirectory() as folder:
                runtime, _ = scripted(
                    [
                        ModelResponse(
                            tool_calls=(
                                ToolCall("bad", "write_file", {"path": "bad.txt", "content": "x"}),
                            ),
                            stop_reason="length",
                        ),
                        ModelResponse("recovered"),
                    ]
                )
                agent = Agent(
                    folder, runtime, permission_mode="bypassPermissions", enable_evolution=False
                )
                self.assertEqual(await agent.run_once("write"), "recovered")
                self.assertFalse(Path(folder, "bad.txt").exists())
                await agent.close()

        asyncio.run(run())

    def test_steering_is_admitted_after_complete_batch(self):
        from weyra.agent import Agent

        async def run():
            with tempfile.TemporaryDirectory() as folder:
                runtime, provider = scripted(
                    [ModelResponse(tool_calls=(ToolCall("x", "ping", {}),)), ModelResponse("done")]
                )
                agent = Agent(folder, runtime, enable_evolution=False)

                async def ping(args, ctx):
                    agent.steer("new constraint")
                    return "pong"

                agent.tools.register(
                    ToolSpec("ping", "ping", {"type": "object"}, ping, "read", "read")
                )
                await agent.run_once("go")
                messages = list(provider.requests[1].messages)
                result_index = next(i for i, m in enumerate(messages) if m.tool_call_id == "x")
                steering_index = next(
                    i for i, m in enumerate(messages) if m.content == "new constraint"
                )
                self.assertGreater(steering_index, result_index)
                await agent.close()

        asyncio.run(run())

    def test_context_fork_excludes_current_turn(self):
        from weyra.agent import Agent

        async def run():
            with tempfile.TemporaryDirectory() as folder:
                runtime, _ = scripted([ModelResponse("old answer")])
                agent = Agent(folder, runtime, enable_evolution=False)
                await agent.run_once("old request")
                agent.session.add_message(
                    __import__("weyra.contracts", fromlist=["Message"]).Message(
                        "user", "in progress"
                    )
                )
                child = await agent.spawn_child("fork", "research", None)
                contents = [m.content for m in child.session.project()]
                self.assertIn("old request", contents)
                self.assertNotIn("in progress", contents)
                await child.close(close_runtime=False)
                await agent.close()

        asyncio.run(run())

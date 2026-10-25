"""Confirmed orchestration regressions; implementation remains owned by root."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from weyra.agent import Agent
from weyra.contracts import ModelInfo, ModelResponse, ToolCall
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider


def runtime_with(responses):
    provider = ScriptedProvider(responses)
    registry = ProviderRegistry()
    registry.register("test", provider)
    return ModelRuntime(registry, {"main": ModelInfo("test", "test", 100000, 1000)}), provider


class AgentRegressionTests(unittest.TestCase):
    def test_followup_not_consumed_before_turn_admission(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                runtime, _ = runtime_with([])
                agent = Agent(directory, runtime, enable_evolution=False)
                try:
                    queued = agent.follow_up("must survive")
                    with patch.object(
                        agent,
                        "_run_turn",
                        new=AsyncMock(
                            side_effect=["first", OSError("turn_start persistence failed")]
                        ),
                    ):
                        with self.assertRaises(OSError):
                            await agent.run_once("first request")
                    consumed = [
                        record
                        for record in agent.session.chain()
                        if record["kind"] == "input_consumed" and record["data"]["id"] == queued
                    ]
                    self.assertEqual(
                        consumed, [], "Input was consumed without any follow-up turn admitted"
                    )
                    self.assertEqual(agent._queues["follow_up"][0]["id"], queued)
                finally:
                    await agent.close()

        asyncio.run(run())

    def test_child_write_permission_intersects_current_parent(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                runtime, _ = runtime_with(
                    [
                        ModelResponse(
                            tool_calls=(
                                ToolCall(
                                    "write",
                                    "write_file",
                                    {"path": "forbidden.txt", "content": "changed"},
                                ),
                            )
                        ),
                        ModelResponse("done"),
                    ]
                )
                agent = Agent(
                    directory, runtime, permission_mode="bypassPermissions", enable_evolution=False
                )
                child = await agent.spawn_child()
                try:
                    agent.permission_mode = "plan"
                    await child.run_once("write forbidden.txt")
                    self.assertFalse(
                        Path(directory, "forbidden.txt").exists(),
                        "Child retained permissions revoked by parent",
                    )
                finally:
                    await child.close(close_runtime=False)
                    await agent.close()

        asyncio.run(run())

    def test_pending_feedback_window_tracks_last_followup(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                runtime, _ = runtime_with(
                    [ModelResponse("first answer"), ModelResponse("last answer")]
                )
                agent = Agent(directory, runtime, enable_evolution=False)
                try:
                    agent.follow_up("last request")
                    self.assertEqual(await agent.run_once("first request"), "last answer")
                    self.assertEqual(
                        agent._pending_window, {"user": "last request", "assistant": "last answer"}
                    )
                finally:
                    await agent.close()

        asyncio.run(run())

    def test_evolution_evidence_is_detached_when_scheduled(self):
        import json

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                runtime, provider = runtime_with(
                    [ModelResponse('{"action":"discard","name":"none"}')]
                )
                agent = Agent(directory, runtime, permission_mode="bypassPermissions")
                try:
                    window = {
                        "user": "original request",
                        "assistant": "answer",
                        "feedback": "original feedback",
                    }
                    agent._schedule_evolution(window)
                    window["user"] = "mutated request"
                    window["feedback"] = "mutated feedback"
                    await asyncio.gather(*list(agent._background))
                    payload = json.loads(provider.requests[0].messages[0].content)
                    self.assertEqual(payload["evidence"]["user"], "original request")
                    self.assertEqual(payload["evidence"]["feedback"], "original feedback")
                finally:
                    await agent.close()

        asyncio.run(run())

    def test_evolution_target_pinned_at_scheduling_boundary(self):
        import json

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "skills", "demo", "SKILL.md")
                path.parent.mkdir(parents=True)
                path.write_text(
                    "---\nname: demo\nversion: 1.0.0\ndescription: demo\n---\noriginal body"
                )
                runtime, provider = runtime_with(
                    [ModelResponse('{"action":"discard","name":"demo"}')]
                )
                agent = Agent(directory, runtime, permission_mode="bypassPermissions")
                try:
                    old = agent.skills.load("demo")
                    agent._schedule_evolution(
                        {"user": "demo", "assistant": "answer", "feedback": "improve demo"}
                    )
                    path.write_text(
                        "---\nname: demo\nversion: 2.0.0\ndescription: demo\n---\nnew body"
                    )
                    await asyncio.gather(*list(agent._background))
                    payload = json.loads(provider.requests[0].messages[0].content)
                    self.assertEqual(payload["target"]["content_hash"], old.content_hash)
                    self.assertEqual(payload["target"]["version"], "1.0.0")
                finally:
                    await agent.close()

        asyncio.run(run())

    def test_cancel_during_result_event_keeps_next_turn_tool_pairs_valid(self):
        from weyra.context import complete_groups

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, "fixture.txt").write_text("read me")
                reached = asyncio.Event()

                async def on_event(kind, data):
                    if kind == "tool_completed":
                        reached.set()
                        await asyncio.Event().wait()

                runtime, _ = runtime_with(
                    [
                        ModelResponse(
                            tool_calls=(ToolCall("read", "read_file", {"path": "fixture.txt"}),)
                        ),
                        ModelResponse("next answer"),
                    ]
                )
                agent = Agent(directory, runtime, enable_evolution=False, on_event=on_event)
                try:
                    active = asyncio.create_task(agent.run_once("read fixture"))
                    await asyncio.wait_for(reached.wait(), 2)
                    agent.abort()
                    with self.assertRaises(asyncio.CancelledError):
                        await active
                    complete_groups(agent.session.project())
                    self.assertEqual(await agent.run_once("continue"), "next answer")
                finally:
                    await agent.close()

        asyncio.run(run())

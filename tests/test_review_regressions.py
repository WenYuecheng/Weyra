"""Confirmed review regressions for durable crash boundaries."""

import asyncio
from unittest.mock import patch
import pytest
from weyra.agent import Agent
from weyra.contracts import Message, ModelInfo, ToolCall
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.session import SessionStore


def runtime():
    registry = ProviderRegistry()
    registry.register("test", ScriptedProvider([]))
    return ModelRuntime(registry, {"main": ModelInfo("test", "test", 100000, 1000)})


def test_recovery_keeps_durably_committed_tool_result(tmp_path):
    path = tmp_path / "session.jsonl"
    store = SessionStore(path, tmp_path)
    store.append("turn_start", {"id": "turn"}, True)
    store.add_message(Message("assistant", tool_calls=(ToolCall("call", "write_file", {}),)))
    store.append("tool_intent", {"call_id": "call", "name": "write_file"}, True)
    store.append(
        "tool_result",
        {
            "call_id": "call",
            "name": "write_file",
            "content": "File updated",
            "status": "success",
            "started": True,
            "effect": "completed",
        },
        True,
    )
    store.close()
    store = SessionStore(path, tmp_path)
    try:
        store.recover_interrupted()
        assert store.project()[-1].content == "File updated"
        assert store.latest("tool_result")["status"] == "success"
    finally:
        store.close()


def test_steering_recovery_does_not_duplicate_committed_message(tmp_path):
    from weyra.contracts import PreparedRequest
    from weyra.runtime_state import RunContext

    async def run():
        path = tmp_path / "session.jsonl"
        agent = Agent(tmp_path, runtime(), session_path=path, enable_evolution=False)
        agent.steer("execute exactly once")
        agent.context = RunContext(tmp_path, agent.session, agent.budget)
        original = agent.session.append

        def interrupted(kind, data, checkpoint=False):
            if kind == "input_consumed":
                raise OSError("crash after message fsync, before consume fsync")
            return original(kind, data, checkpoint)

        try:
            with patch.object(agent.session, "append", side_effect=interrupted):
                agent._consume("steering")
                with pytest.raises(OSError):
                    agent._ack_inputs(PreparedRequest(agent.runtime.resolve("main"), "", ()))
            assert len(agent._queues["steering"]) == 1
        finally:
            await agent.close()
        resumed = Agent(tmp_path, runtime(), session_path=path, enable_evolution=False)
        try:
            resumed.context = RunContext(tmp_path, resumed.session, resumed.budget)
            resumed._consume("steering")
            resumed._ack_inputs(PreparedRequest(resumed.runtime.resolve("main"), "", ()))
            assert [m.content for m in resumed.session.project()].count("execute exactly once") == 1
            assert resumed._queues["steering"] == []
        finally:
            await resumed.close()

    asyncio.run(run())


def test_child_respects_parent_deny_rules(tmp_path):
    from weyra.runtime_state import RunContext

    async def run():
        parent = Agent(
            tmp_path, runtime(), permission_mode="bypassPermissions", enable_evolution=False
        )
        parent.tools.deny_rules = (lambda spec, args: spec.name == "write_file",)
        child = await parent.spawn_child()
        try:
            context = RunContext(tmp_path, child.session, child.budget, "bypassPermissions")
            context.services["agent"] = child
            result = await child.tools.execute(
                ToolCall("call", "write_file", {"path": "denied.txt", "content": "bad"}), context
            )
            assert result.status == "denied"
            assert not (tmp_path / "denied.txt").exists()
        finally:
            await child.close(close_runtime=False)
            await parent.close()

    asyncio.run(run())


def test_duplicate_model_call_ids_rejected_before_effects(tmp_path):
    from weyra.contracts import ModelResponse

    async def run():
        registry = ProviderRegistry()
        provider = ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("same", "write_file", {"path": "one.txt", "content": "effect1"}),
                        ToolCall("same", "write_file", {"path": "two.txt", "content": "effect2"}),
                    )
                )
            ]
        )
        registry.register("test", provider)
        model = ModelRuntime(registry, {"main": ModelInfo("test", "test", 100000, 1000)})
        agent = Agent(tmp_path, model, permission_mode="bypassPermissions", enable_evolution=False)
        try:
            try:
                await agent.run_once("test malformed batch")
            except Exception:
                pass
            assert not (tmp_path / "one.txt").exists()
            assert not (tmp_path / "two.txt").exists()
        finally:
            await agent.close()

    asyncio.run(run())

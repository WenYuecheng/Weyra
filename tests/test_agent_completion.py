import asyncio
from weyra.agent import Agent
from weyra.contracts import ModelInfo, ModelResponse, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider


def runtime_for(responses):
    provider = ScriptedProvider(responses)
    registry = ProviderRegistry()
    registry.register("test", provider)
    runtime = ModelRuntime(registry, {"main": ModelInfo("test", "test", 200000, 1000)})
    return runtime, provider


def test_overlapping_memory_batches_are_deduplicated_per_entry(tmp_path):
    async def run():
        runtime, provider = runtime_for(
            [
                ModelResponse('["a","b"]', usage=Usage(1, 1)),
                ModelResponse("one"),
                ModelResponse('["a","c"]', usage=Usage(1, 1)),
                ModelResponse("two"),
            ]
        )
        agent = Agent(tmp_path, runtime, enable_evolution=False)
        for name in "abc":
            agent.memory.update(name, name + " durable fact")
        await agent.run_once("facts")
        await agent.run_once("more facts")
        visible = [m for m in agent.session.project() if m.source == "memory"]
        assert sum(m.content.count('<memory id="a"') for m in visible) == 1
        await agent.close()

    asyncio.run(run())


def test_steering_consumes_one_at_a_time(tmp_path):
    async def run():
        runtime, provider = runtime_for([ModelResponse("one"), ModelResponse("two")])
        agent = Agent(tmp_path, runtime, enable_memory=False, enable_evolution=False)
        agent.steer("first")
        agent.steer("second")
        await agent.run_once("go")
        assert "first" in [m.content for m in provider.requests[0].messages]
        assert "second" not in [m.content for m in provider.requests[0].messages]
        assert "second" in [m.content for m in provider.requests[1].messages]
        await agent.close()

    asyncio.run(run())


def test_request_prepare_failure_keeps_pending_steering(tmp_path):
    async def run():
        import pytest
        from weyra.model_runtime import ModelError

        runtime, provider = runtime_for([])
        runtime.route_parameters = {"main": {"unsupported": "bad"}}
        agent = Agent(tmp_path, runtime, enable_memory=False, enable_evolution=False)
        queued = agent.steer("keep this")
        with pytest.raises(ModelError):
            await agent.run_once("go")
        assert agent._queues["steering"][0]["id"] == queued
        assert not any(r["kind"] == "input_consumed" for r in agent.session.chain())
        await agent.close()

    asyncio.run(run())


def test_task_token_limit_is_enforced_in_agent(tmp_path):
    async def run():
        import pytest
        from weyra.runtime_state import BudgetExceeded

        runtime, provider = runtime_for([ModelResponse("never")])
        agent = Agent(
            tmp_path, runtime, task_token_limit=1, enable_memory=False, enable_evolution=False
        )
        with pytest.raises(BudgetExceeded):
            await agent.run_once("hello")
        assert not provider.requests
        await agent.close()

    asyncio.run(run())


def test_goal_completion_needs_observed_evidence(tmp_path):
    async def run():
        from weyra.contracts import ToolCall

        runtime, _ = runtime_for(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "claim",
                            "goal_update",
                            {"todos": [{"text": "verify", "done": True}], "complete": True},
                        ),
                    )
                ),
                ModelResponse("stopped"),
            ]
        )
        agent = Agent(tmp_path, runtime, enable_memory=False, enable_evolution=False, max_steps=2)
        agent.set_goal("verify")
        # Disable automatic continuation to inspect the tool decision directly.
        agent.goal.continuations = 3
        await agent.run_once("go")
        result = next(r["data"] for r in agent.session.chain() if r["kind"] == "tool_result")
        assert result["status"] != "success"
        assert agent.goal.state != "complete"
        await agent.close()

    asyncio.run(run())


def test_project_instruction_changes_rebuild_at_next_turn(tmp_path):
    async def run():
        runtime, provider = runtime_for([ModelResponse("one"), ModelResponse("two")])
        rules = tmp_path / "WEYRA.md"
        rules.write_text("Use rule A.")
        agent = Agent(tmp_path, runtime, enable_memory=False, enable_evolution=False)
        await agent.run_once("first")
        rules.write_text("Use rule B.")
        await agent.run_once("second")
        assert "Use rule A." in provider.requests[0].system
        assert "Use rule B." in provider.requests[1].system
        assert "Use rule A." not in provider.requests[1].system
        assert any(
            r["kind"] == "context_version" and r["data"]["reason"] == "project_instructions_changed"
            for r in agent.session.chain()
        )
        await agent.close()

    asyncio.run(run())


def test_invalid_configuration_releases_session_writer(tmp_path):
    import pytest
    from weyra.session import SessionStore

    runtime, _ = runtime_for([])
    with pytest.raises(ValueError):
        Agent(tmp_path, runtime, session_id="invalid", context_policy={"headroom_tokens": 300000})
    store = SessionStore(project=tmp_path, session_id="invalid")
    store.close()


def test_nested_project_environment_does_not_expose_parent_changes(tmp_path):
    import subprocess
    from weyra.prompt import environment_snapshot

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "outside-private-note.txt").write_text("not part of the operated project")
    project = tmp_path / "nested"
    project.mkdir()
    facts = asyncio.run(environment_snapshot(project))
    assert facts["project"] == str(project)
    assert "outside-private-note.txt" not in facts.get("git_status", "")

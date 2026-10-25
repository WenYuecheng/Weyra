import asyncio
from types import SimpleNamespace as NS

import pytest

from weyra.subagent import ChildManager


class Session:
    def __init__(self):
        self.events = []

    def append(self, kind, data, checkpoint=False):
        self.events.append((kind, data))


class Agent:
    def __init__(self, gate=None):
        self.depth = 0
        self.context = NS(id="owner")
        self.session = Session()
        self.budget = NS(total_tokens=0)
        self.children = []
        self.gate = gate
        self.prompts = []
        self.queued = []
        self.closed = []

    async def spawn_child(self, mode, role, allowed_tools):
        child = Agent(self.gate)
        child.depth = self.depth + 1
        self.children.append(child)
        return child

    async def run_once(self, prompt):
        self.prompts.append(prompt)
        if self.gate:
            await self.gate.wait()
        return "result:" + prompt

    def steer(self, text):
        self.queued.append(("steer", text))

    def follow_up(self, text):
        self.queued.append(("follow_up", text))

    async def abort(self):
        pass

    async def close(self, close_runtime=False):
        self.closed.append(close_runtime)


def test_results_persist_before_once_only_notification_and_reuse_idle_child():
    async def run():
        parent = Agent()
        manager = ChildManager(parent)
        result = await manager.spawn("one")
        assert result["state"] == "completed"
        assert parent.session.events[-1][0] == "child_result"
        assert manager.drain_results() == [result]
        assert manager.drain_results() == []
        next_task = await manager.send(result["task_id"], "two")
        result2 = await manager.wait(next_task["task_id"])
        assert result2["task_id"] != result["task_id"]
        assert len(parent.children) == 1
        assert parent.children[0].prompts == ["one", "two"]
        await manager.close()
        assert parent.children[0].closed == [False]

    asyncio.run(run())


def test_timeout_does_not_cancel_and_owner_cancel_is_scoped():
    async def run():
        gate = asyncio.Event()
        parent = Agent(gate)
        manager = ChildManager(parent)
        first = await manager.spawn("one", background=True)
        await asyncio.sleep(0)
        assert (await manager.wait(first["task_id"], timeout=0.001))["state"] == "running"
        await manager.send(first["task_id"], "guide", kind="steer")
        assert parent.children[0].queued == [("steer", "guide")]
        parent.context = NS(id="other")
        second = await manager.spawn("two", background=True)
        await manager.cancel_owner("owner")
        assert (await manager.wait(first["task_id"]))["state"] == "cancelled"
        gate.set()
        assert (await manager.wait(second["task_id"]))["state"] == "completed"
        assert len(manager.drain_results()) == 2
        await manager.close()

    asyncio.run(run())


def test_depth_limit_rejects_spawn_before_factory():
    async def run():
        parent = Agent()
        parent.depth = 1
        manager = ChildManager(parent, max_depth=1)
        with pytest.raises(ValueError):
            await manager.spawn("forbidden")
        assert parent.children == []

    asyncio.run(run())


def test_concurrency_and_pending_cancellation_have_single_result():
    async def run():
        parent = Agent(asyncio.Event())
        manager = ChildManager(parent, max_concurrency=1)
        first = await manager.spawn("one", background=True)
        second = await manager.spawn("two", background=True)
        await asyncio.sleep(0)
        assert len(parent.children) == 1
        assert manager.list()[1]["state"] == "pending"
        await manager.interrupt(second["task_id"])
        await manager.interrupt(first["task_id"])
        assert len(manager.drain_results()) == 2
        assert manager.drain_results() == []
        await manager.close()

    asyncio.run(run())


def test_late_child_completion_after_cancel_is_discarded():
    class Stubborn(Agent):
        async def run_once(self, prompt):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return "late answer"

    async def run():
        parent = Agent()
        child = Stubborn()

        async def factory(*args):
            return child

        parent.spawn_child = factory
        manager = ChildManager(parent)
        task = await manager.spawn("late", background=True)
        await asyncio.sleep(0)
        await manager.interrupt(task["task_id"])
        results = manager.drain_results()
        assert len(results) == 1
        assert results[0]["state"] == "cancelled"
        assert "late answer" not in str(parent.session.events)
        await manager.close()

    asyncio.run(run())


def test_failed_child_exposes_exception_type_without_raw_history():
    class Failing(Agent):
        async def run_once(self, prompt):
            raise RuntimeError("secret raw child transcript")

    async def run():
        parent = Agent()

        async def factory(*args):
            return Failing()

        parent.spawn_child = factory
        manager = ChildManager(parent)
        result = await manager.spawn("work")
        assert result["state"] == "failed"
        assert result["unresolved"] == ["RuntimeError"]
        assert "secret" not in str(result)
        await manager.close()

    asyncio.run(run())


def test_child_usage_excludes_concurrent_shared_budget_calls():
    class Metered(Agent):
        async def run_once(self, prompt):
            self.budget.total_tokens += 900
            self.session.records.append(
                {"kind": "model_usage", "data": {"total_tokens": 7, "accounted_tokens": 7}}
            )
            self.session.records.append({"kind": "attempt_end", "data": {"usage": 7}})
            return "done"

    async def run():
        parent = Agent()
        child = Metered()
        child.session.records = []
        child.budget = parent.budget

        async def factory(*args):
            return child

        parent.spawn_child = factory
        manager = ChildManager(parent)
        result = await manager.spawn("work")
        assert result["usage"] == 7
        result2 = await manager.send(result["task_id"], "more")
        assert (await manager.wait(result2["task_id"]))["usage"] == 7
        await manager.close()

    asyncio.run(run())


def test_truncated_child_is_failed_with_durable_unresolved_reason(tmp_path):
    from weyra.agent import Agent
    from weyra.contracts import ModelInfo, ModelResponse
    from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider

    async def run():
        registry = ProviderRegistry()
        registry.register("p", ScriptedProvider([ModelResponse("partial", stop_reason="length")]))
        runtime = ModelRuntime(registry, {"main": ModelInfo("p", "m", 200000, 1000)})
        parent = Agent(tmp_path, runtime, enable_memory=False, enable_evolution=False)
        try:
            result = await parent.children.spawn("Finish the delegated task.")
            assert result["state"] == "failed"
            assert result["unresolved"] == ["Child turn ended incomplete"]
            assert parent.session.pending_child_results()[0]["state"] == "failed"
        finally:
            await parent.close()

    asyncio.run(run())


def test_child_delivers_durable_evidence_and_written_artifact_refs(tmp_path):
    from weyra.agent import Agent
    from weyra.contracts import ModelInfo, ModelResponse, ToolCall, Usage
    from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider

    async def run():
        (tmp_path / "source.txt").write_text("evidence")
        registry = ProviderRegistry()
        registry.register(
            "p",
            ScriptedProvider(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall("read", "read_file", {"path": "source.txt"}),
                            ToolCall(
                                "write", "write_file", {"path": "result.txt", "content": "artifact"}
                            ),
                        ),
                        usage=Usage(1, 1),
                    ),
                    ModelResponse("done", usage=Usage(1, 1)),
                ]
            ),
        )
        parent = Agent(
            tmp_path,
            ModelRuntime(registry, {"main": ModelInfo("p", "m", 200000, 1000)}),
            permission_mode="acceptEdits",
            enable_memory=False,
            enable_evolution=False,
        )
        try:
            result = await parent.children.spawn("Read evidence and save result.")
            assert result["state"] == "completed"
            assert any("read" in ref for ref in result["evidence"])
            assert str(tmp_path / "result.txt") in result["artifacts"]
            assert parent.session.pending_child_results()[0]["evidence"] == result["evidence"]
        finally:
            await parent.close()

    asyncio.run(run())

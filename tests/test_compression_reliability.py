"""Offline regression coverage for bounded, atomic context compression."""
import asyncio

import pytest

from weyra.context import ContextManager
from weyra.contracts import Message, ModelInfo, ModelResponse, Usage
from weyra.model_runtime import ModelError, ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.runtime_state import Budget, RunContext
from weyra.session import SessionStore


def setup(tmp_path, responses, mode="dsh_checkpoint", window=20000):
    store = SessionStore(project=tmp_path)
    model = ModelInfo("p", "main", 50000, 100)
    auxiliary = ModelInfo("p", "aux", window, 100)
    provider = ScriptedProvider(responses)
    registry = ProviderRegistry()
    registry.register("p", provider)
    runtime = ModelRuntime(registry, {"main": model, "compression": auxiliary})
    manager = ContextManager(store, model, "", policy={"headroom_tokens": 100,
        "retain_tokens": 0, "summary_mode": mode})
    context = RunContext(tmp_path, store, Budget())
    store.add_message(Message("user", "history " * 1000))
    store.add_message(Message("assistant", "tail"))
    return store, manager, runtime, context, provider


def test_auto_failure_latch_is_per_turn_and_manual_resets(tmp_path):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path, [ModelResponse("")] * 10)
        for _ in range(4):
            assert not await manager.compact(runtime, ctx, trigger="overflow")
        assert len(provider.requests) == 2
        assert not await manager.compact(runtime, ctx, trigger="manual")
        assert len(provider.requests) == 3
        next_ctx = RunContext(tmp_path, store, ctx.budget)
        assert not await manager.compact(runtime, next_ctx, trigger="pressure")
        assert len(provider.requests) == 4
        failures = [r["data"] for r in store.records if r["kind"] == "compression_failure"]
        assert len(failures) == 4
        assert all(f["stage"] and f["reason"] and f["calls"] == 1 and "usage" in f for f in failures)
        assert not any(r["kind"] == "compaction" for r in store.records)
        store.close()
    asyncio.run(run())


@pytest.mark.parametrize("bad", ['{"episode_memory":{"task":"wrapped"}}', '[]', '{}', '{"x":'])
def test_parallel_components_receive_one_format_correction(tmp_path, bad):
    async def run():
        good = ModelResponse('{"fact":"keep"}')
        store, manager, runtime, ctx, provider = setup(tmp_path,
            [ModelResponse(bad), good, good, good], "three_parallel")
        assert await manager.compact(runtime, ctx, trigger="overflow")
        assert len(provider.requests) == 4
        assert all("Return only JSON with nonempty episode_memory" not in r.system for r in provider.requests)
        summary = store.latest("compaction")["summary"]
        assert all(v == {"fact": "keep"} for v in summary.values())
        store.close()
    asyncio.run(run())


def test_bad_components_never_publish_and_only_retry_once(tmp_path):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path,
            [ModelResponse('{"episode_memory":{"x":1}}')] * 20, "three_parallel")
        assert not await manager.compact(runtime, ctx, trigger="overflow")
        assert len(provider.requests) <= 6
        assert not any(r["kind"] == "compaction" for r in store.records)
        store.close()
    asyncio.run(run())


def test_segment_retry_budget_counts_physical_attempts(tmp_path, monkeypatch):
    async def no_sleep(*args):
        pass
    monkeypatch.setattr("weyra.model_runtime.asyncio.sleep", no_sleep)
    async def run():
        responses = []
        for _ in range(20):
            responses.extend([ModelError("transient"), ModelError("transient"),
                ModelResponse("keep", usage=Usage(input_tokens=3, output_tokens=1))])
        store, manager, runtime, ctx, provider = setup(tmp_path, responses, window=2300)
        # Each group fits alone, while all groups need more than eight physical calls.
        for _ in range(4):
            store.add_message(Message("user", "x" * 1500))
        # Replace first history group so no single group is oversized.
        # Use a fresh projection populated with complete, small groups.
        manager.session.project = lambda: [Message("user", "x" * 1500, id=str(i))
            for i in range(5)] + [Message("assistant", "tail", id="tail")]
        before = [m.to_dict() for m in manager.session.project()]
        assert not await manager.compact(runtime, ctx, trigger="overflow")
        assert len(provider.requests) == 8
        assert [m.to_dict() for m in manager.session.project()] == before
        failure = store.latest("compression_failure")
        assert failure["calls"] == 8
        assert failure["reason"] == "attempt_limit"
        assert failure["usage"]["total_tokens"] == 8
        assert not any(r["kind"] == "compaction" for r in store.records)
        store.close()
    asyncio.run(run())


def test_truncated_component_at_output_cap_does_not_retry(tmp_path):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path,
            [ModelResponse('{"fact":"incomplete"}', stop_reason="length")]
            + [ModelResponse('{"fact":"keep"}')] * 3, "three_parallel")
        before = [m.to_dict() for m in store.project()]
        assert not await manager.compact(runtime, ctx, trigger="overflow")
        assert len(provider.requests) <= 3
        assert all(r.parameters["max_output_tokens"] == 100 for r in provider.requests)
        assert [m.to_dict() for m in store.project()] == before
        assert store.latest("compaction") is None
        store.close()
    asyncio.run(run())


def test_single_json_correction_cannot_repeat_indefinitely(tmp_path):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path,
            [ModelResponse("[]")] * 10, "single_json")
        assert not await manager.compact(runtime, ctx, trigger="overflow")
        assert len(provider.requests) == 2
        assert store.latest("compression_failure")["calls"] == 2
        assert not any(r["kind"] == "compaction" for r in store.records)
        store.close()
    asyncio.run(run())


def test_auto_latch_still_allows_free_tool_pruning(tmp_path):
    from weyra.contracts import ToolCall
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path, [ModelResponse('')] * 2)
        assert not await manager.compact(runtime, ctx, trigger='overflow')
        assert not await manager.compact(runtime, ctx, trigger='overflow')
        store.add_message(Message('assistant', tool_calls=(ToolCall('late', 'read_file', {}),)))
        store.add_message(Message('tool', 'x' * 10000, tool_call_id='late'))
        assert await manager.compact(runtime, ctx, trigger='overflow')
        assert len(provider.requests) == 2
        assert store.latest('compaction')['kind'] == 'result_reduction'
        store.close()
    asyncio.run(run())

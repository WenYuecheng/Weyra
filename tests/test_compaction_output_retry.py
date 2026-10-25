"""Offline checks for bounded output expansion after a truncated summary."""
import asyncio

import pytest

from weyra.context import ContextManager
from weyra.contracts import Message, ModelInfo, ModelResponse, Usage
from weyra.model_runtime import ModelError, ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.runtime_state import Budget, BudgetExceeded, RunContext
from weyra.session import SessionStore


JSON_SUMMARY = '{"episode_memory":{"fact":"keep"},"working_memory":{"next":"continue"},"tool_memory":{}}'


def setup(tmp_path, responses, *, mode="dsh_checkpoint", cap=8192, window=20000, budget=None):
    store = SessionStore(project=tmp_path)
    model = ModelInfo("p", "main", window, cap)
    provider = ScriptedProvider(responses)
    registry = ProviderRegistry()
    registry.register("p", provider)
    runtime = ModelRuntime(registry, {"main": model, "compression": model})
    manager = ContextManager(store, model, "source system", policy={
        "headroom_tokens": 2048, "summary_max_tokens": 2048,
        "retain_tokens": 0, "summary_mode": mode,
    })
    ctx = RunContext(tmp_path, store, budget or Budget())
    store.add_message(Message("user", "history " * 900))
    store.add_message(Message("assistant", "tail"))
    return store, manager, runtime, ctx, provider


def projection(store):
    return [m.to_dict() for m in store.project()]


@pytest.mark.parametrize("mode", ["dsh_checkpoint", "single_json", "three_parallel"])
def test_length_expands_once_with_fresh_request(tmp_path, mode):
    async def run():
        good = '{"fact":"keep"}' if mode == "three_parallel" else JSON_SUMMARY
        store, manager, runtime, ctx, provider = setup(tmp_path,
            [ModelResponse("partial", stop_reason="length")] + [ModelResponse(good)] * 4,
            mode=mode)
        try:
            assert await manager.compact(runtime, ctx, trigger="overflow")
            first, second = provider.requests[:2]
            assert [r.parameters["max_output_tokens"] for r in (first, second)] == [2048, 4096]
            assert first.id != second.id
            assert first.fingerprint != second.fingerprint
            assert first.system == second.system
            assert first.messages == second.messages
            assert first.tools == second.tools
            assert first.context_version == second.context_version
            assert "partial" not in str(store.latest("compaction"))
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["dsh_checkpoint", "single_json", "three_parallel"])
def test_second_length_never_publishes_or_issues_third_attempt(tmp_path, mode):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path,
            [ModelResponse("partial", stop_reason="length")] * 20, mode=mode)
        before = projection(store)
        try:
            assert not await manager.compact(runtime, ctx, trigger="overflow")
            limits = [r.parameters["max_output_tokens"] for r in provider.requests]
            assert limits.count(4096) == 1
            assert len(limits) <= (4 if mode == "three_parallel" else 2)
            assert projection(store) == before
            assert store.latest("compaction") is None
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("kwargs", [{"cap": 2048}, {"window": 8000, "cap": 4096}])
def test_expansion_requires_larger_cap_and_context_capacity(tmp_path, kwargs):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path,
            [ModelResponse("partial", stop_reason="length"), ModelResponse("keep")], **kwargs)
        before = projection(store)
        try:
            assert not await manager.compact(runtime, ctx, trigger="overflow")
            assert len(provider.requests) == 1
            assert projection(store) == before
        finally:
            store.close()
    asyncio.run(run())


def test_expansion_uses_remaining_physical_attempt_budget(tmp_path):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path,
            [ModelResponse("partial", stop_reason="length"), ModelResponse("keep")])
        attempts = {"attempts": 7, "limit": 8}
        try:
            with pytest.raises(ModelError) as exc:
                await manager._summarize(runtime, ctx, store.project()[:1], attempt_budget=attempts)
            assert exc.value.kind == "attempt_limit"
            assert len(provider.requests) == 1
            assert attempts["attempts"] == 8
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("limits, calls", [({"call_limit": 1}, 1), ({"token_limit": 7000}, 1),
                                          ({"cost_limit_cny": "1"}, 0)])
def test_budget_refusal_preserves_history(tmp_path, limits, calls):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path,
            [ModelResponse("partial", stop_reason="length", usage=Usage(100, 20)),
             ModelResponse("keep")], budget=Budget(**limits))
        before = projection(store)
        try:
            with pytest.raises(BudgetExceeded):
                await manager.compact(runtime, ctx, trigger="overflow")
            assert len(provider.requests) == calls
            assert projection(store) == before
            assert store.latest("compaction") is None
        finally:
            store.close()
    asyncio.run(run())


def test_cancel_after_truncation_preserves_history(tmp_path):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path,
            [ModelResponse("partial", stop_reason="length"), ModelResponse("keep")])
        original = runtime.complete

        async def cancel_after_response(request, context, *, attempt_budget=None):
            result = await original(request, context, attempt_budget=attempt_budget)
            context.cancel_event.set()
            return result

        runtime.complete = cancel_after_response
        before = projection(store)
        try:
            with pytest.raises(asyncio.CancelledError):
                await manager.compact(runtime, ctx, trigger="overflow")
            assert len(provider.requests) == 1
            assert projection(store) == before
        finally:
            store.close()
    asyncio.run(run())


def test_expansion_is_shared_across_summary_segments(tmp_path):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path, [
            ModelResponse("first partial", stop_reason="length"), ModelResponse("keep"),
            ModelResponse("second partial", stop_reason="length"), ModelResponse("unused"),
        ])
        attempts = {"attempts": 0, "limit": 8}
        try:
            assert await manager._summarize_once(runtime, ctx, store.project()[:1], attempts) == "keep"
            with pytest.raises(ValueError, match="truncated"):
                await manager._summarize_once(runtime, ctx, store.project()[:1], attempts)
            assert [r.parameters["max_output_tokens"] for r in provider.requests] == [2048, 4096, 2048]
            assert attempts["attempts"] == 3
        finally:
            store.close()
    asyncio.run(run())


def test_json_format_correction_retains_expanded_output_limit(tmp_path):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path, [
            ModelResponse("partial", stop_reason="length"), ModelResponse("[]"),
            ModelResponse(JSON_SUMMARY),
        ], mode="single_json")
        try:
            assert await manager.compact(runtime, ctx, trigger="overflow")
            assert [r.parameters["max_output_tokens"] for r in provider.requests] == [2048, 4096, 4096]
        finally:
            store.close()
    asyncio.run(run())


def test_expansion_is_clamped_to_selected_model_output_cap(tmp_path):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path, [
            ModelResponse("partial", stop_reason="length"), ModelResponse("keep"),
        ], cap=3072)
        try:
            assert await manager.compact(runtime, ctx, trigger="overflow")
            assert [r.parameters["max_output_tokens"] for r in provider.requests] == [2048, 3072]
        finally:
            store.close()
    asyncio.run(run())


def test_expanded_output_cost_is_reserved_before_second_provider_call(tmp_path):
    async def run():
        store, manager, runtime, ctx, provider = setup(tmp_path, [
            ModelResponse("partial", stop_reason="length", usage=Usage(100, 20)),
            ModelResponse("keep"),
        ], budget=Budget(cost_limit_cny="0.3"))
        runtime.pricing = {"p/main": {
            "currency": "CNY", "input_per_million": "0", "output_per_million": "100",
        }}
        before = projection(store)
        try:
            with pytest.raises(BudgetExceeded):
                await manager.compact(runtime, ctx, trigger="overflow")
            assert len(provider.requests) == 1
            assert ctx.budget.calls == 1
            assert projection(store) == before
        finally:
            store.close()
    asyncio.run(run())

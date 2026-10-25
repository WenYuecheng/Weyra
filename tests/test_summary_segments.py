import asyncio

import pytest

from weyra.context import ContextManager, ContextOverflow, complete_groups
from weyra.contracts import Message, ModelInfo, ModelResponse, ToolCall, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.prompt import COMPACTION_INSTRUCTION
from weyra.runtime_state import Budget, RunContext
from weyra.session import SessionStore


def setup(tmp_path, responses, *, window=2300, mode="dsh_checkpoint"):
    store = SessionStore(project=tmp_path)
    main = ModelInfo("main", "large", 50000, 100)
    auxiliary = ModelInfo("aux", "small", window, 100)
    unused = ScriptedProvider([])
    provider = ScriptedProvider(responses)
    registry = ProviderRegistry()
    registry.register("main", unused)
    registry.register("aux", provider)
    runtime = ModelRuntime(registry, {"main": main, "compression": auxiliary})
    runtime.pricing = {
        "aux/small": {
            "currency": "CNY",
            "input_per_million": "1",
            "output_per_million": "2",
        }
    }
    manager = ContextManager(
        store,
        main,
        "system",
        policy={
            "headroom_tokens": 100,
            "summary_mode": mode,
        },
    )
    context = RunContext(tmp_path, store, Budget())
    context.budget.attach(store)
    return store, manager, runtime, context, provider, unused


def response(text="Keep all constraints."):
    return ModelResponse(text, usage=Usage(input_tokens=20, output_tokens=5))


def test_explicit_auxiliary_and_fitting_prefix(tmp_path):
    async def run():
        store, manager, runtime, context, provider, unused = setup(tmp_path, [response()])
        prefix = Message("user", "history " * 100)
        assert await manager._summarize(runtime, context, [prefix]) == "Keep all constraints."
        request = provider.requests[0]
        assert not unused.requests
        assert request.model == runtime.routes["compression"]
        assert request.messages[0].to_dict() == prefix.to_dict()
        assert request.messages[-1].content == COMPACTION_INSTRUCTION
        assert request.system == manager.system and request.tools == manager._tools
        assert not any(r["kind"] == "summary_segment" for r in store.records)
        runtime.inherited_purposes.add("compression")
        assert manager._summary_model(runtime) == manager.model
        store.close()

    asyncio.run(run())


def test_segments_cover_every_message_and_record_all_calls(tmp_path):
    async def run():
        store, manager, runtime, context, provider, unused = setup(
            tmp_path, [response() for _ in range(20)]
        )
        messages = [Message("user", str(i) + "x" * 1500) for i in range(5)]
        for m in messages:
            store.add_message(m)
        store.add_message(Message("assistant", "tail"))
        assert await manager.compact(runtime, context, trigger="overflow")
        facts = [r["data"] for r in store.records if r["kind"] == "summary_segment"]
        leaves = [f for f in facts if f["merge_level"] == 0]
        assert [raw for f in leaves for raw in f["covered"]] == [m.id for m in messages]
        assert facts[-1]["covered"] == [m.id for m in messages]
        assert facts[-1]["merge_level"] > 0
        assert all(f["input_hash"] and f["result_hash"] for f in facts)
        assert all(f["strategy"] == "complete_group_hierarchy" for f in facts)
        assert context.budget.calls == len(provider.requests) == len(facts)
        assert context.budget.total_tokens == 25 * len(facts)
        from decimal import Decimal

        assert context.budget.total_cost_cny == Decimal("0.00003") * len(facts)
        usage = [r["data"] for r in store.records if r["kind"] == "model_usage"]
        assert all(
            u["cost_known"] and Decimal(u["actual_cost_cny"]) == Decimal("0.00003") for u in usage
        )
        for kind in ("model_request", "model_usage", "budget_settlement"):
            assert len([r for r in store.records if r["kind"] == kind]) == len(facts)
        assert not unused.requests
        assert [r["data"]["covered"] for r in store.records if r["kind"] == "compaction"][-1] == [
            m.id for m in messages
        ]
        store.close()

    asyncio.run(run())


def test_tool_batch_is_whole_and_later_failure_never_publishes(tmp_path):
    async def run():
        store, manager, runtime, context, provider, _ = setup(
            tmp_path, [response(), ModelResponse("", stop_reason="length")]
        )
        messages = [
            Message("user", "x" * 1500),
            Message("assistant", tool_calls=(ToolCall("a", "read", {}), ToolCall("b", "read", {}))),
            Message("tool", "y" * 400, tool_call_id="a"),
            Message("tool", "z" * 400, tool_call_id="b"),
            Message("user", "w" * 1500),
            Message("assistant", "tail"),
        ]
        for m in messages:
            store.add_message(m)
        before = [m.to_dict() for m in store.project()]
        assert not await manager.compact(runtime, context, trigger="overflow")
        assert len(provider.requests) == 2
        for request in provider.requests:
            complete_groups(list(request.messages[:-1]))
            if any(m.tool_calls for m in request.messages):
                assert {m.tool_call_id for m in request.messages if m.role == "tool"} == {"a", "b"}
        assert [m.to_dict() for m in store.project()] == before
        assert manager.version == 0
        assert not any(r["kind"] == "compaction" for r in store.records)
        store.close()

    asyncio.run(run())


def test_single_oversize_group_stops_without_call(tmp_path):
    async def run():
        store, manager, runtime, context, provider, _ = setup(tmp_path, [])
        with pytest.raises(ContextOverflow, match="complete interaction group"):
            await manager._summarize(runtime, context, [Message("user", "x" * 10000)])
        assert not provider.requests
        store.close()

    asyncio.run(run())


def test_nonshrinking_segments_stop_without_loop(tmp_path):
    async def run():
        store, manager, runtime, context, provider, _ = setup(
            tmp_path, [response("s" * 3000) for _ in range(10)]
        )
        with pytest.raises(ContextOverflow, match="did not shrink"):
            await manager._summarize(
                runtime, context, [Message("user", "x" * 1500) for _ in range(5)]
            )
        assert len(provider.requests) < 10
        assert not any(r["kind"] == "compaction" for r in store.records)
        store.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["single_json", "three_parallel"])
def test_segmented_json_retains_schema(tmp_path, mode):
    async def run():
        from weyra.contracts import canonical

        data = {
            "episode_memory": {"task": "keep"},
            "working_memory": {"next": "continue"},
            "tool_memory": {"observations": "keep"},
        }
        if mode == "three_parallel":
            responses = [response(canonical(data[k])) for _ in range(30) for k in data]
        else:
            responses = [response(canonical(data)) for _ in range(30)]
        store, manager, runtime, context, provider, _ = setup(
            tmp_path, responses, window=1400, mode=mode
        )
        messages = [Message("user", "x" * 1500) for _ in range(4)]
        if mode == "three_parallel":
            from weyra.model_runtime import ModelError

            # Two leaves plus a merge already require nine component requests.
            # The shared eight-physical-request budget must stop that hierarchy.
            with pytest.raises(ModelError, match="physical attempt limit"):
                await manager._summarize(runtime, context, messages)
            assert len(provider.requests) == 8
            assert not any(r["kind"] == "compaction" for r in store.records)
        else:
            result = await manager._summarize(runtime, context, messages)
            assert result == data
        assert any(r["kind"] == "summary_segment" for r in store.records)
        assert context.budget.calls == len(provider.requests)
        store.close()

    asyncio.run(run())


def test_policy_override_wins_over_explicit_compression_route(tmp_path):
    store, manager, runtime, context, provider, unused = setup(tmp_path, [])
    from weyra.context import resolve_context_policy

    manager.policy = resolve_context_policy(
        {
            "headroom_tokens": 100,
            "summarization_provider": "main",
            "summarization_model": "large",
        },
        manager.model,
    )
    assert manager._summary_model(runtime) == manager.model
    store.close()


def test_config_check_uses_explicit_auxiliary_capacity(tmp_path, monkeypatch):
    from weyra.config import check_config

    monkeypatch.delenv("MODEL", raising=False)
    main = '[routes.main]\nprovider="openai"\nmodel="main"\ncontext_window=200000\nmax_output_tokens=100\n'
    auxiliary = '[routes.compression]\nprovider="openai"\nmodel="aux"\ncontext_window=200000\nmax_output_tokens=1000\n'
    policy = "[context_policy]\nheadroom_tokens=100\nsummary_max_tokens=500\n"
    path = tmp_path / "weyra.toml"
    path.write_text(main + auxiliary + policy)
    check_config(tmp_path)
    path.write_text(main + policy)
    with pytest.raises(ValueError, match="Summary output limit"):
        check_config(tmp_path)
    path.write_text(
        main + auxiliary + policy + 'summarization_provider="openai"\nsummarization_model="main"\n'
    )
    with pytest.raises(ValueError, match="Summary output limit"):
        check_config(tmp_path)


@pytest.mark.parametrize("mode", ["dsh_checkpoint", "single_json", "three_parallel"])
def test_multimodal_summary_never_publishes(tmp_path, mode):
    async def run():
        from weyra.contracts import canonical

        body = (
            "short summary"
            if mode == "dsh_checkpoint"
            else canonical(
                {
                    "episode_memory": {"task": "x"},
                    "working_memory": {"next": "x"},
                    "tool_memory": {},
                }
            )
        )
        bad = ModelResponse(
            body,
            provider_data={
                "content": [{"type": "image", "source": {"type": "base64", "data": "fake"}}]
            },
        )
        store, manager, runtime, context, provider, _ = setup(
            tmp_path, [bad] * 3, window=20000, mode=mode
        )
        store.add_message(Message("user", "x" * 10000))
        store.add_message(Message("assistant", "tail"))
        assert not await manager.compact(runtime, context, trigger="overflow")
        assert not any(r["kind"] == "compaction" for r in store.records)
        store.close()

    asyncio.run(run())

import asyncio
from types import SimpleNamespace

import pytest

from weyra.context import ContextManager, resolve_context_policy
from weyra.contracts import Message, ModelInfo, PreparedRequest
from weyra.runtime_state import Budget, RunContext
from weyra.session import SessionStore


def manager_for(tmp_path, **kwargs):
    store = SessionStore(tmp_path / "anchor.jsonl", tmp_path)
    model = ModelInfo("test", "active", 200000, 1000)
    manager = ContextManager(
        store, model, "stable system", policy={"headroom_tokens": 100}, **kwargs
    )
    return store, manager


def record_usage(store, request, input_tokens, **usage):
    store.append("model_request", request.to_dict(), True)
    store.append(
        "model_usage",
        {
            "request_id": request.id,
            "purpose": request.purpose,
            "provider_id": request.model.provider_id,
            "model_id": request.model.model_id,
            "input_tokens": input_tokens,
            "output_tokens": 10,
            **usage,
        },
        True,
    )


def test_compatible_usage_anchor_counts_new_messages_and_survives_resume(tmp_path):
    store, manager = manager_for(tmp_path)
    try:
        store.add_message(Message("user", "original"))
        anchor = manager.request()
        heuristic = anchor.parameters["estimated_input_tokens"]
        record_usage(store, anchor, heuristic + 500, cache_read_tokens=200)
        unchanged = manager.request()
        assert unchanged.parameters["estimated_input_tokens"] == heuristic + 500
        assert manager.measurement["estimation_method"] == "provider_usage_signed_delta"
        assert manager.measurement["anchor_request_id"] == anchor.id
        assert manager.measurement["anchor_cache_read_tokens"] == 200
        store.add_message(Message("assistant", "new response " * 100))
        current = manager.request()
        assert current.parameters["estimated_input_tokens"] > heuristic + 500
        assert manager.measurement["projection_delta_tokens"] > 0
        assert (
            current.parameters["estimated_input_tokens"]
            == manager.measurement["heuristic_input_tokens"] + 500
        )
        records = len(store.records)
        messages = [m.to_dict() for m in current.messages]
        assert [m.to_dict() for m in manager.request().messages] == messages
        assert len(store.records) == records
        reloaded = ContextManager(store, manager.model, "unused", policy={"headroom_tokens": 100})
        assert (
            reloaded.request().parameters["estimated_input_tokens"]
            == current.parameters["estimated_input_tokens"]
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    "change", ["route", "system", "tools", "parameters", "prefix", "zero_usage"]
)
def test_incompatible_usage_never_anchors_pressure(tmp_path, change):
    store, manager = manager_for(tmp_path)
    try:
        store.add_message(Message("user", "original"))
        base = manager.request()
        model = base.model
        system = base.system
        tools = base.tools
        parameters = base.parameters
        messages = base.messages
        usage = 10000
        if change == "route":
            model = ModelInfo("test", "other", 200000, 1000)
        if change == "system":
            system = "different"
        if change == "tools":
            tools = ({"name": "other"},)
        if change == "parameters":
            parameters = {**parameters, "temperature": 0.3}
        if change == "prefix":
            messages = (Message("user", "different prefix"),)
        if change == "zero_usage":
            usage = 0
        anchor = PreparedRequest(model, system, messages, tools, parameters=parameters)
        record_usage(store, anchor, usage)
        request = manager.request()
        assert (
            request.parameters["estimated_input_tokens"]
            == base.parameters["estimated_input_tokens"]
        )
        assert manager.measurement["estimation_method"] == "weyra_utf8_bytes_div2"
        assert manager.measurement["anchor_request_id"] is None
    finally:
        store.close()


def test_smaller_real_usage_calibrates_pressure_and_conservative_growth(tmp_path):
    store, manager = manager_for(tmp_path)
    try:
        store.add_message(Message("user", "English code and JSON " * 1000))
        anchor = manager.request()
        heuristic = anchor.parameters["estimated_input_tokens"]
        actual = heuristic // 2
        record_usage(store, anchor, actual)
        assert manager.request().parameters["estimated_input_tokens"] == actual
        store.add_message(Message("assistant", "new content " * 100))
        request = manager.request()
        delta = manager.measurement["heuristic_input_tokens"] - heuristic
        assert request.parameters["estimated_input_tokens"] == actual + delta
        assert manager.measurement["anchor_request_id"] == anchor.id
        reloaded = ContextManager(store, manager.model, "unused", policy={"headroom_tokens": 100})
        assert reloaded.request().parameters["estimated_input_tokens"] == actual + delta
    finally:
        store.close()


@pytest.mark.parametrize("overestimated", [False, True])
def test_compaction_signed_delta_reduces_anchored_pressure(tmp_path, overestimated):
    async def run():
        store, manager = manager_for(tmp_path)
        try:
            store.add_message(Message("user", "long history " * 1000))
            store.add_message(Message("assistant", "tail"))
            anchor = manager.request()
            heuristic = anchor.parameters["estimated_input_tokens"]
            actual = heuristic // 2 if overestimated else heuristic + 1000
            record_usage(store, anchor, actual)
            before = manager.request().parameters["estimated_input_tokens"]

            class Runtime:
                async def complete(self, request, context):
                    from weyra.contracts import ModelResponse

                    return ModelResponse("Continue work")

            assert await manager.compact(
                Runtime(), RunContext(tmp_path, store, Budget()), trigger="overflow"
            )
            after = manager.request().parameters["estimated_input_tokens"]
            assert after < before
            assert manager.measurement["projection_delta_tokens"] < 0
            if overestimated:
                import math

                assert after == actual + math.ceil(
                    manager.measurement["projection_delta_tokens"] * actual / heuristic
                )
                assert after > 1
            else:
                assert after == manager.measurement["heuristic_input_tokens"] + 1000
            checkpoint = store.latest("compaction")
            assert checkpoint["measurement_after"]["anchor_request_id"] == anchor.id
        finally:
            store.close()

    asyncio.run(run())


def test_effective_route_parameters_are_part_of_usage_compatibility(tmp_path):
    async def run():
        store, manager = manager_for(tmp_path)
        try:
            store.add_message(Message("user", "request"))
            runtime = SimpleNamespace(
                parameters_for=lambda purpose: {"temperature": 0.7, "max_output_tokens": 500}
            )
            context = RunContext(tmp_path, store, Budget())
            anchor = await manager.prepare(runtime, context)
            record_usage(store, anchor, 5000)
            assert (await manager.prepare(runtime, context)).parameters[
                "estimated_input_tokens"
            ] == 5000
            runtime.parameters_for = lambda purpose: {"temperature": 0.3, "max_output_tokens": 500}
            assert (await manager.prepare(runtime, context)).parameters[
                "estimated_input_tokens"
            ] < 5000
            assert manager.measurement["anchor_request_id"] is None
        finally:
            store.close()

    asyncio.run(run())


def test_summary_max_tokens_alias_and_model_override():
    policy = resolve_context_policy(
        {
            "headroom_tokens": 100,
            "summary_max_tokens": 50,
            "model_policies": [{"provider": "p", "model": "m", "summary_max_tokens": 75}],
        },
        ModelInfo("p", "m", 10000, 100),
    )
    assert policy.max_tokens == 75
    with pytest.raises(ValueError, match="conflict"):
        resolve_context_policy({"max_tokens": 50, "summary_max_tokens": 60})


def test_runtime_usage_anchor_works_with_persisted_request_blobs(tmp_path):
    from weyra.contracts import ModelResponse, Usage
    from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider

    async def run():
        store, manager = manager_for(tmp_path)
        try:
            registry = ProviderRegistry()
            registry.register(
                "test", ScriptedProvider([ModelResponse("done", usage=Usage(1000, 10, 300))])
            )
            runtime = ModelRuntime(registry, {"main": manager.model})
            store.add_message(Message("user", "request"))
            context = RunContext(tmp_path, store, Budget())
            request = await manager.prepare(runtime, context)
            await runtime.complete(request, context)
            measured = await manager.prepare(runtime, context)
            assert measured.parameters["estimated_input_tokens"] == 1000
            assert manager.measurement["anchor_cache_read_tokens"] == 300
            assert store.latest("context_measurement") == manager.measurement
        finally:
            store.close()

    asyncio.run(run())


def test_branch_ancestry_cannot_reuse_usage_from_another_branch(tmp_path):
    store, manager = manager_for(tmp_path)
    try:
        split = store.add_message(Message("user", "shared"))["id"]
        store.add_message(Message("assistant", "old branch"))
        record_usage(store, manager.request(), 10000)
        assert manager.request().parameters["estimated_input_tokens"] == 10000
        store.branch(split)
        store.add_message(Message("user", "new branch"))
        manager.request()
        assert manager.measurement["anchor_request_id"] is None
    finally:
        store.close()


def test_model_switch_resolves_exact_policy_and_checks_effective_output_cap(tmp_path):
    store = SessionStore(tmp_path / "switch.jsonl", tmp_path)
    first = ModelInfo("test", "a", 200000, 1000)
    second = ModelInfo("test", "b", 50000, 100)
    policy = {
        "headroom_tokens": 100,
        "summary_max_tokens": 50,
        "model_policies": [
            {"provider": "test", "model": "b", "headroom_tokens": 200, "summary_max_tokens": 75}
        ],
    }
    manager = ContextManager(store, first, "stable", policy=policy)
    try:
        runtime = SimpleNamespace(
            parameters_for=lambda purpose, model: {
                "max_output_tokens": 500 if model == first else 50
            }
        )
        manager._sync_policy(runtime)
        store.add_message(Message("user", "request"))
        record_usage(store, manager.request(), 5000)
        manager.model = second
        manager._sync_policy(runtime)
        assert manager.policy.headroom_tokens == 200 and manager.policy.max_tokens == 75
        assert manager.request().parameters["max_output_tokens"] == 50
        assert manager.measurement["anchor_request_id"] is None
        runtime.parameters_for = lambda purpose, model: {"max_output_tokens": 101}
        with pytest.raises(ValueError, match="configured model capacity"):
            manager._sync_policy(runtime)
    finally:
        store.close()


def test_summary_settings_cannot_exceed_selected_model_output_cap(tmp_path):
    from weyra.context import ContextOverflow

    async def run():
        store = SessionStore(tmp_path / "summary-cap.jsonl", tmp_path)
        model = ModelInfo("test", "a", 20000, 50)
        manager = ContextManager(store, model, "stable", policy={"headroom_tokens": 100})
        runtime = SimpleNamespace()
        try:
            with pytest.raises(ContextOverflow, match="Summary output limit"):
                await manager._summarize(
                    runtime, RunContext(tmp_path, store, Budget()), [Message("user", "history")]
                )
        finally:
            store.close()

    asyncio.run(run())

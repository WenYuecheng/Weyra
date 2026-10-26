import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from weyra.context import ContextManager
from weyra.contracts import Message, ModelInfo, ModelResponse, ToolCall
from weyra.runtime_state import Budget, RunContext
from weyra.session import SessionStore


def test_dsh_policy_defaults_and_exact_target():
    from weyra.context import resolve_context_policy, context_budgets

    model = ModelInfo("p", "m", 200000, 16000)
    policy = resolve_context_policy()
    assert policy.headroom_tokens == policy.max_tokens == 65536
    assert policy.compaction_retries == policy.max_overflow_retries == 1
    assert policy.summary_mode == "dsh_checkpoint"
    assert context_budgets(model, policy) == (118464, 29440)
    config = {
        "headroom_tokens": 100,
        "model_policies": [
            {"provider": "p", "model": "m", "headroom_tokens": 200, "retain_tokens": 50}
        ],
    }
    exact = resolve_context_policy(config, model)
    assert exact.max_tokens == 200
    assert exact.retain_tokens == 50
    assert (
        resolve_context_policy(config, ModelInfo("other", "m", 10000, 100)).headroom_tokens == 100
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        resolve_context_policy({"retain_ratio": 0.2, "retain_tokens": 10})
    with pytest.raises(ValueError, match="pressure"):
        context_budgets(ModelInfo("p", "m", 8000, 100), policy)


def test_dsh_checkpoint_replays_prefix_and_tools(tmp_path):
    from weyra.prompt import COMPACTION_INSTRUCTION, CHECKPOINT_PREAMBLE, SUMMARY_OPEN_TAG

    async def run():
        store = SessionStore(tmp_path / "s.jsonl", tmp_path)
        model = ModelInfo("p", "m", 10000, 100)
        manager = ContextManager(store, model, "original system", policy={"headroom_tokens": 100})
        prefix = Message("user", "history " * 1000)
        tail = Message("assistant", "latest")
        store.add_message(prefix)
        store.add_message(tail)
        declarations = ({"name": "read", "parameters": {"type": "object"}},)
        manager.request(declarations)
        calls = []

        class Runtime:
            routes = {"main": model}

            def resolve(self, route):
                return model

            async def complete(self, request, context):
                calls.append(request)
                return ModelResponse("## Current Work\n- Continue")

        ctx = RunContext(tmp_path, store, Budget())
        assert await manager.compact(Runtime(), ctx, trigger="overflow")
        request = calls[0]
        assert request.system == "original system"
        assert request.tools == declarations
        assert [m.to_dict() for m in request.messages[:-1]] == [prefix.to_dict()]
        assert request.messages[-1].role == "user"
        assert request.messages[-1].content == COMPACTION_INSTRUCTION
        assert request.parameters["max_output_tokens"] == 100
        assert store.project()[0].content.startswith(
            CHECKPOINT_PREAMBLE + "\n\n" + SUMMARY_OPEN_TAG
        )
        assert store.project()[-1].id == tail.id
        store.close()

    asyncio.run(run())


def test_prompt_constants_match_pinned_upstream_hashes():
    # DeepSeek Harness d743267388641bc76f17c45ce8b4c231aed1d32c,
    # packages/compaction/compaction-basic/src/summarizer.ts.
    from weyra.prompt import COMPACTION_INSTRUCTION, CHECKPOINT_PREAMBLE

    assert hashlib.sha256(COMPACTION_INSTRUCTION.encode()).hexdigest() == (
        "3b7aa71f525b9a82afc2f1747c4434ceb7b3bc1fadf8b64c37c4e97db70d074b"
    )
    assert hashlib.sha256(CHECKPOINT_PREAMBLE.encode()).hexdigest() == (
        "306ffa304ad5eee1b9a19a5744b7bf665eac77c4fd154ad3eed572987a534da1"
    )


def test_pruning_uses_unicode_points_and_all_tool_groups(tmp_path):
    from weyra.output import PRUNE_MARKER

    async def run():
        store = SessionStore(tmp_path / "s.jsonl", tmp_path)
        model = ModelInfo("p", "m", 10000, 100)
        manager = ContextManager(store, model, "", policy={"headroom_tokens": 100})
        for i, text in enumerate(["😀" * 8192, "😀" * 8193]):
            call = ToolCall(str(i), "read", {})
            store.add_message(Message("assistant", tool_calls=(call,)))
            store.add_message(Message("tool", text, tool_call_id=call.id))
        assert await manager.reduce_large_results()
        results = [m.content for m in store.project() if m.role == "tool"]
        assert results[0] == "😀" * 8192
        assert results[1] == "😀" * 4096 + PRUNE_MARKER + "😀" * 1024
        assert not await manager.reduce_large_results()
        assert any(
            r["kind"] == "message" and r["data"].get("content") == "😀" * 8193
            for r in store.records
        )
        store.close()

    asyncio.run(run())


def test_overflow_prune_only_counts_as_changed(tmp_path):
    async def run():
        store = SessionStore(tmp_path / "s.jsonl", tmp_path)
        manager = ContextManager(
            store, ModelInfo("p", "m", 20000, 100), "", policy={"headroom_tokens": 100}
        )
        call = ToolCall("one", "read", {})
        store.add_message(Message("assistant", tool_calls=(call,)))
        store.add_message(Message("tool", "x" * 9000, tool_call_id="one"))
        ctx = RunContext(tmp_path, store, Budget())
        assert await manager.compact(SimpleNamespace(), ctx, trigger="overflow")
        assert not await manager.compact(SimpleNamespace(), ctx, trigger="overflow")
        store.close()

    asyncio.run(run())


def test_summary_rejects_nonshrink_and_concurrent_surface_change(tmp_path):
    async def run():
        store = SessionStore(tmp_path / "s.jsonl", tmp_path)
        model = ModelInfo("p", "m", 20000, 100)
        manager = ContextManager(store, model, "", policy={"headroom_tokens": 100})
        store.add_message(Message("user", "history " * 500))
        store.add_message(Message("assistant", "tail"))
        ctx = RunContext(tmp_path, store, Budget())

        class Runtime:
            routes = {"main": model}

            async def complete(self, request, context):
                store.add_message(Message("user", "new request"))
                return ModelResponse("small")

        assert not await manager.compact(Runtime(), ctx, trigger="overflow")
        assert store.project()[-1].content == "new request"
        assert not any(m.source == "compaction" for m in store.project())
        store.close()

    asyncio.run(run())


def test_policy_rejects_one_sided_summarizer_override():
    from weyra.context import resolve_context_policy

    with pytest.raises(ValueError, match="together"):
        resolve_context_policy(
            {
                "summarization_provider": "p",
                "summarization_model": "m",
                "model_policies": [{"provider": "p", "model": "m", "summarization_model": "other"}],
            }
        )


def test_pressure_repeats_successful_compaction_until_below_threshold(tmp_path):
    async def run():
        store = SessionStore(tmp_path / "s.jsonl", tmp_path)
        model = ModelInfo("p", "m", 30000, 100)
        manager = ContextManager(
            store,
            model,
            "",
            policy={
                "headroom_tokens": 100,
                "threshold_ratio": 0.1,
                "retain_tokens": 50,
                "compaction_retries": 1,
            },
        )
        store.add_message(Message("user", "h" * 18000))
        store.add_message(Message("assistant", "tail"))

        class Runtime:
            calls = 0
            routes = {"main": model}

            async def complete(self, request, context):
                self.calls += 1
                return ModelResponse("s" * (7000 if self.calls == 1 else 100))

        runtime = Runtime()
        ctx = RunContext(tmp_path, store, Budget())
        request = await manager.prepare(runtime, ctx)
        assert runtime.calls == 2
        assert request.parameters["estimated_input_tokens"] < manager.threshold
        store.close()

    asyncio.run(run())


def test_explicit_manager_policy_is_not_overwritten_by_runtime_defaults(tmp_path):
    from weyra.context import ContextPolicy

    async def run():
        store = SessionStore(tmp_path / "s.jsonl", tmp_path)
        model = ModelInfo("p", "m", 10000, 100)
        manager = ContextManager(store, model, "", policy={"headroom_tokens": 100})
        store.add_message(Message("user", "hello"))
        runtime = SimpleNamespace(context_policy=ContextPolicy())
        request = await manager.prepare(runtime, RunContext(tmp_path, store, Budget()))
        assert request.messages[-1].content == "hello"
        store.close()

    asyncio.run(run())


def test_pressure_uses_effective_request_output_reserve(tmp_path):
    from weyra.context import ContextPolicy, context_budgets

    model = ModelInfo("p", "m", 100000, 40000)
    assert context_budgets(model, ContextPolicy(), reserved_output=1000) == (33464, 15840)
    store = SessionStore(tmp_path / "s.jsonl", tmp_path)
    try:
        manager = ContextManager(store, model, "", reserved_output=1000)
        runtime = SimpleNamespace(
            context_policy=ContextPolicy(),
            parameters_for=lambda purpose: {"max_output_tokens": 1000},
        )
        manager._sync_policy(runtime)
        assert manager.threshold == 33464 and manager.retain == 15840
    finally:
        store.close()


@pytest.mark.parametrize("prune", [True, False])
def test_projection_and_version_publish_atomically(tmp_path, monkeypatch, prune):
    from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider

    async def run():
        store = SessionStore(project=tmp_path)
        model = ModelInfo("p", "m", 100000, 1000)
        manager = ContextManager(store, model, "", policy={"headroom_tokens": 1000})
        if prune:
            call = ToolCall("r", "read", {})
            store.add_message(Message("assistant", tool_calls=(call,)))
            store.add_message(Message("tool", "x" * 10000, tool_call_id="r"))
        else:
            store.add_message(Message("user", "x" * 10000))
            store.add_message(Message("assistant", "tail"))
        previous = [m.to_dict() for m in store.project()]
        old_version = manager.version
        original = store.append

        def append(kind, data, checkpoint=False):
            if kind == "context_version":
                raise OSError("version checkpoint failed")
            return original(kind, data, checkpoint)

        monkeypatch.setattr(store, "append", append)
        registry = ProviderRegistry()
        registry.register("p", ScriptedProvider([ModelResponse("Keep the constraint.")]))
        runtime = ModelRuntime(registry, {"main": model})
        with pytest.raises(OSError):
            if prune:
                await manager.reduce_large_results()
            else:
                await manager.compact(
                    runtime, RunContext(tmp_path, store, Budget()), trigger="overflow"
                )
        assert [m.to_dict() for m in store.project()] == previous
        assert manager.version == old_version
        assert not any(r["kind"] == "compaction" for r in store.records)
        store.close()

    asyncio.run(run())

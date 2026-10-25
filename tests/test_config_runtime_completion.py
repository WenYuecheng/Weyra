import asyncio
from types import SimpleNamespace as NS

import pytest

from weyra.config import load_config
from weyra.contracts import Message, ModelInfo, ModelResponse, PreparedRequest, Usage
from weyra.model_runtime import (
    AnthropicProvider,
    ModelError,
    ModelRuntime,
    ProviderRegistry,
    ScriptedProvider,
)
from weyra.runtime_state import Budget


def ctx():
    records = []
    return NS(
        budget=Budget(),
        session=NS(append=lambda kind, data, **kw: records.append((kind, data))),
        check_cancelled=lambda: None,
        records=records,
    )


def test_runtime_retries_each_physical_attempt_and_caps_shared_budget(monkeypatch):
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("weyra.model_runtime.asyncio.sleep", sleep)
    r = ProviderRegistry()
    p = ScriptedProvider(
        [ModelError("transient"), ModelError("transient"), ModelResponse("yes", usage=Usage(2, 1))]
    )
    r.register("x", p)
    model = ModelInfo("x", "m", 1000, 100)
    runtime = ModelRuntime(r, {"main": model})
    context = ctx()
    attempts = {"attempts": 0, "limit": 4}
    result = asyncio.run(
        runtime.complete(
            PreparedRequest(model, "", (), parameters={"estimated_input_tokens": 1}),
            context,
            attempt_budget=attempts,
        )
    )
    assert result.content == "yes" and context.budget.calls == 3 and attempts["attempts"] == 3
    assert len(sleeps) == 2 and 0.45 <= sleeps[0] <= 0.55 and 0.9 <= sleeps[1] <= 1.1
    assert len([x for x in context.records if x[0] == "model_usage"]) == 3


def test_runtime_accepts_valid_anthropic_thinking_and_rejects_foreign_parameters():
    model = ModelInfo("anthropic", "m", 10000, 2000, supports_reasoning=True)
    registry = ProviderRegistry()
    p = ScriptedProvider([ModelResponse("ok")])
    registry.register("anthropic", p)
    runtime = ModelRuntime(registry, {"main": model})
    req = PreparedRequest(
        model,
        "",
        (),
        parameters={
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "max_output_tokens": 1500,
        },
    )
    assert asyncio.run(runtime.complete(req, ctx())).content == "ok"
    with pytest.raises(ModelError):
        asyncio.run(
            runtime.complete(
                PreparedRequest(model, "", (), parameters={"reasoning_effort": "high"}), ctx()
            )
        )


def test_route_activation_preserves_explicit_auxiliary_routes():
    a = ModelInfo("x", "a", 1000, 100)
    b = ModelInfo("x", "b", 2000, 200)
    c = ModelInfo("x", "c", 3000, 300)
    r = ProviderRegistry()
    r.register("x", ScriptedProvider())
    runtime = ModelRuntime(r, {"main": a, "compression": a, "memory": c})
    runtime.inherited_purposes = {"compression", "evolution"}
    runtime.fallbacks = {"main": [b, c]}
    assert runtime.next_fallback("main", a) == b
    runtime.activate_route("main", b)
    assert runtime.resolve("compression") == b and runtime.resolve("memory") == c
    assert runtime.next_fallback("main", b) == c


def test_env_layers_and_secret_snapshot_are_private(tmp_path, monkeypatch):
    for name in (
        "MODEL",
        "API",
        "APIKEY",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("MODEL=deepseek-chat\nPROVIDER=deepseek\nAPIKEY=dotenv-secret\n")
    monkeypatch.setenv("MODEL", "unknown-live-model")
    monkeypatch.setenv("CONTEXT_WINDOW", "200000")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "10000")
    monkeypatch.setenv("APIKEY", "process-secret")
    config = load_config(tmp_path, {"model": "cli-model"})
    assert config.routes["main"].model_id == "cli-model"
    assert config.routes["main"].context_window == 200000
    assert "process-secret" not in repr(config) and "dotenv-secret" not in repr(config)
    runtime = config.build_runtime()
    assert runtime.registry.get("deepseek")._api_key == "process-secret"
    assert "DEEPSEEK_API_KEY" not in __import__("os").environ
    assert config.safe_summary()["routes"]["main"]["model_id"] == "cli-model"


def test_provider_specific_env_beats_generic_in_same_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER", "deepseek")
    monkeypatch.setenv("MODEL", "generic")
    monkeypatch.setenv("DEEPSEEK_MODEL", "exact")
    monkeypatch.setenv("CONTEXT_WINDOW", "200000")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "10000")
    assert load_config(tmp_path).routes["main"].model_id == "exact"


def test_model_output_cap_can_equal_context_capacity():
    assert ModelInfo("x", "m", 1000, 1000).max_output_tokens == 1000


def test_anthropic_cache_marks_stable_system_and_last_two_without_mutation():
    calls = []

    class Client:
        def __init__(self):
            self.messages = self

        async def create(self, **kw):
            calls.append(kw)
            return NS(content=[NS(type="text", text="ok")], stop_reason="end_turn", usage=None)

    model = ModelInfo("anthropic", "m", 10000, 1000, supports_cache=True)
    req = PreparedRequest(
        model,
        "stable\n\ndynamic",
        [Message("user", str(i)) if i % 2 == 0 else Message("assistant", str(i)) for i in range(6)],
    )
    before = req.to_dict()

    async def run():
        return [e async for e in AnthropicProvider(client=Client()).stream(req, None)]

    asyncio.run(run())
    kw = calls[0]
    assert isinstance(kw["system"], list) and len(kw["system"]) == 1
    assert kw["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert [i for i, m in enumerate(kw["messages"]) if "cache_control" in m["content"][-1]] == [
        3,
        4,
        5,
    ]
    assert req.to_dict() == before


def test_cny_cost_uses_decimal_and_distinguishes_cache_read_write():
    from decimal import Decimal
    from weyra.pricing import Price

    price = Price.from_dict(
        {
            "currency": "CNY",
            "input_per_million": "2",
            "output_per_million": "8",
            "cache_read_per_million": ".2",
            "cache_write_per_million": "2.5",
        }
    )
    assert price.estimate(1000000, 1000000) == Decimal("10.5")
    assert price.actual(Usage(1000000, 1000000, 200000, 100000)) == Decimal("9.69")
    with pytest.raises(ValueError):
        Price.from_dict({"currency": "USD", "input_per_million": "2", "output_per_million": "8"})


def test_anthropic_cache_splits_only_declared_prefix_and_uses_four_markers():
    from weyra.model_runtime import _anthropic_cache

    kwargs = {
        "system": "stable\nvolatile",
        "messages": [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": [{"type": "text", "text": str(i)}],
            }
            for i in range(6)
        ],
    }
    _anthropic_cache(kwargs, "5m", "stable")
    assert [b["text"] for b in kwargs["system"]] == ["stable", "\nvolatile"]
    assert [i for i, m in enumerate(kwargs["messages"]) if "cache_control" in m["content"][-1]] == [
        4,
        5,
    ]


def test_retry_budget_caps_across_overflow_reprepared_calls(monkeypatch):
    async def sleep(_):
        pass

    monkeypatch.setattr("weyra.model_runtime.asyncio.sleep", sleep)
    r = ProviderRegistry()
    p = ScriptedProvider([ModelError("transient")] * 4)
    r.register("x", p)
    model = ModelInfo("x", "m", 1000, 100)
    runtime = ModelRuntime(r, {"main": model})
    context = ctx()
    attempts = {"attempts": 0, "limit": 4}
    req = PreparedRequest(model, "", (), parameters={"estimated_input_tokens": 1})
    with pytest.raises(ModelError):
        asyncio.run(runtime.complete(req, context, attempt_budget=attempts))
    with pytest.raises(ModelError):
        asyncio.run(runtime.complete(req, context, attempt_budget=attempts))
    assert len(p.requests) == 4 and context.budget.calls == 4


def test_retry_does_not_repeat_auth_or_invalid(monkeypatch):
    async def sleep(_):
        raise AssertionError("must not sleep")

    monkeypatch.setattr("weyra.model_runtime.asyncio.sleep", sleep)
    for kind in ("auth", "invalid", "overflow"):
        r = ProviderRegistry()
        p = ScriptedProvider([ModelError(kind)])
        r.register("x", p)
        m = ModelInfo("x", "m", 1000, 100)
        with pytest.raises(ModelError):
            asyncio.run(ModelRuntime(r, {"main": m}).complete(PreparedRequest(m, "", ()), ctx()))
        assert len(p.requests) == 1


def test_route_parameters_are_separate_from_model_capacity(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    (tmp_path / "weyra.toml").write_text(
        '[routes.main]\nprovider="anthropic"\nmodel="test"\ncontext_window=200000\nmax_output_tokens=64000\nsupports_reasoning=true\n[routes.main.parameters]\nmax_output_tokens=4096\nthinking={type="enabled",budget_tokens=1024}\n'
    )
    runtime = load_config(tmp_path).build_runtime()
    assert runtime.resolve().max_output_tokens == 64000
    assert runtime.parameters_for("memory")["max_output_tokens"] == 4096
    assert runtime.parameters_for("main")["thinking"]["budget_tokens"] == 1024


def test_config_check_refuses_summary_capacity_overflow_without_key(tmp_path, monkeypatch):
    from weyra.config import check_config

    monkeypatch.delenv("MODEL", raising=False)
    (tmp_path / "weyra.toml").write_text(
        '[routes.main]\nprovider="openai"\nmodel="test"\ncontext_window=200000\nmax_output_tokens=4096\n'
    )
    with pytest.raises(ValueError, match="summary|Summary"):
        check_config(tmp_path)


def test_configuration_credentials_do_not_re_read_mutated_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL", "test")
    monkeypatch.setenv("PROVIDER", "openai")
    monkeypatch.setenv("CONTEXT_WINDOW", "200000")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "4096")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("APIKEY", raising=False)
    provider = load_config(tmp_path).build_runtime().registry.get("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "later-unrelated-secret")
    with pytest.raises(ModelError, match="credential"):
        provider._client()


def test_physical_attempt_passes_exact_cny_reserve_and_actual_to_budget():
    from decimal import Decimal

    class CostBudget:
        async def reserve(self, amount, purpose, *, estimated_cost_cny=None, scope=None):
            self.estimate = estimated_cost_cny
            self.scope = scope
            return "held"

        async def settle(self, reservation, actual, *, actual_cost_cny=None):
            self.actual = actual_cost_cny

    model = ModelInfo("x", "m", 200000, 1000)
    r = ProviderRegistry()
    r.register("x", ScriptedProvider([ModelResponse("yes", usage=Usage(1000, 500))]))
    runtime = ModelRuntime(r, {"main": model})
    runtime.pricing = {
        "x/m": {"currency": "CNY", "input_per_million": "2", "output_per_million": "8"}
    }
    context = ctx()
    context.budget = CostBudget()
    context.id = "scope"
    asyncio.run(
        runtime.complete(
            PreparedRequest(model, "", (), parameters={"estimated_input_tokens": 1000}), context
        )
    )
    assert (
        context.budget.estimate == Decimal(".408")
        and context.budget.actual == Decimal(".006")
        and context.budget.scope == "scope"
    )


def test_quota_exhaustion_is_not_retriable():
    from weyra.model_runtime import _error

    error = type(
        "RateLimitError",
        (Exception,),
        {"status_code": 429, "body": {"error": {"code": "insufficient_quota"}}},
    )("private")
    assert _error(error).kind == "budget"


def test_runtime_route_parameters_remain_sticky_for_frozen_request():
    old = ModelInfo("x", "old", 1000, 100)
    new = ModelInfo("x", "new", 1000, 100)
    registry = ProviderRegistry()
    provider = ScriptedProvider([ModelResponse("yes")])
    registry.register("x", provider)
    runtime = ModelRuntime(registry, {"main": old})
    runtime.route_parameters = {"main": {"temperature": 0.25}}
    runtime.fallback_parameters = {("main", "x", "new"): {"temperature": 0.75}}
    frozen = PreparedRequest(old, "", ())
    runtime.activate_route("main", new)
    asyncio.run(runtime.complete(frozen, ctx()))
    assert provider.requests[0].parameters["temperature"] == 0.25


def test_custom_thinking_format_reaches_fake_sdk_without_extra_body_escape():
    from weyra.model_runtime import OpenAIProvider

    calls = []

    class Client:
        def __init__(self):
            self.chat = NS(completions=self)

        async def create(self, **kw):
            calls.append(kw)
            return NS(
                choices=[NS(message=NS(content="ok", tool_calls=[]), finish_reason="stop")],
                usage=NS(prompt_tokens=2, completion_tokens=1),
            )

    model = ModelInfo(
        "cun", "glm-5.3-flash", 1000000, 65500, supports_reasoning=True, reasoning_format="thinking"
    )
    provider = OpenAIProvider(provider_id="cun", base_url="https://www.cun.ai/v1", client=Client())
    registry = ProviderRegistry()
    registry.register("cun", provider)
    runtime = ModelRuntime(registry, {"main": model})
    history = [
        Message(
            "assistant",
            "foreign",
            provider_data={
                "provider_id": "cun",
                "model_id": "another-model",
                "reasoning_content": "private",
            },
        ),
        Message("user", "next"),
    ]
    request = PreparedRequest(
        model,
        "",
        history,
        parameters={
            "thinking": {"type": "disabled"},
            "estimated_input_tokens": 2,
            "max_output_tokens": 100,
        },
    )
    assert asyncio.run(runtime.complete(request, ctx())).content == "ok"
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "thinking" not in calls[0] and calls[0]["max_tokens"] == 100
    assert "reasoning_content" not in calls[0]["messages"][0]
    with pytest.raises(ModelError):
        asyncio.run(
            runtime.complete(
                PreparedRequest(model, "", (), parameters={"extra_body": {"arbitrary": True}}),
                ctx(),
            )
        )
    assert len(calls) == 1


def test_custom_thinking_format_is_configurable(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    (tmp_path / "weyra.toml").write_text(
        '[providers.cun]\nprotocol="openai"\napi_key_env="CUN_API_KEY"\nbase_url="https://www.cun.ai/v1"\n[routes.main]\nprovider="cun"\nmodel="glm-5.3-flash"\ncontext_window=1000000\nmax_output_tokens=65500\nsupports_reasoning=true\nreasoning_format="thinking"\n[routes.main.parameters]\nthinking={type="disabled"}\n'
    )
    config = load_config(tmp_path)
    assert config.routes["main"].reasoning_format == "thinking"
    assert config.build_runtime().parameters_for("main")["thinking"] == {"type": "disabled"}


def test_each_retry_has_unique_physical_attempt_id_for_auxiliary_calls(monkeypatch):
    async def sleep(_):
        pass

    monkeypatch.setattr("weyra.model_runtime.asyncio.sleep", sleep)
    model = ModelInfo("x", "m", 1000, 100)
    registry = ProviderRegistry()
    registry.register(
        "x", ScriptedProvider([ModelError("transient"), ModelResponse("ok", usage=Usage(2, 1))])
    )
    runtime = ModelRuntime(registry, {"main": model})
    context = ctx()
    req = PreparedRequest(model, "", (), purpose="memory")
    asyncio.run(runtime.complete(req, context))
    requests = [data for kind, data in context.records if kind == "model_request"]
    starts = [data for kind, data in context.records if kind == "attempt_start"]
    ends = [data for kind, data in context.records if kind == "attempt_end"]
    usage = [data for kind, data in context.records if kind == "model_usage"]
    assert len(starts) == len(ends) == len(usage) == len(requests) == 2
    assert len({data["attempt_id"] for data in requests}) == 2
    assert {data["request_id"] for data in starts + ends + usage} == {req.id}
    assert (
        {data["attempt_id"] for data in requests}
        == {data["attempt_id"] for data in starts}
        == {data["attempt_id"] for data in ends}
        == {data["attempt_id"] for data in usage}
    )
    assert [data["state"] for data in ends] == ["failed", "completed"]
    assert ends[-1]["stop_reason"] == "stop" and ends[-1]["usage"]["output_tokens"] == 1
    assert all(data["purpose"] == "memory" for data in starts + ends)

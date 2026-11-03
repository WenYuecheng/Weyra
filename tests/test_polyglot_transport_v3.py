"""Offline transport regressions for complete, atomic tool argument decoding."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from weyra.contracts import ModelInfo, PreparedRequest, Usage
from weyra.model_runtime import ModelError, ModelRuntime, OpenAIProvider, ProviderRegistry


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.chat = NS(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


def response(arguments='{"value":', *, streaming=False, finish="length"):
    calls = [
        NS(id="first", function=NS(name="edit", arguments='{"value":1}')),
        NS(id="second", function=NS(name="edit", arguments=arguments)),
    ]
    usage = NS(prompt_tokens=10, completion_tokens=4)
    if not streaming:
        return NS(
            choices=[NS(message=NS(content="draft", tool_calls=calls), finish_reason=finish)],
            usage=usage,
        )

    async def chunks():
        yield NS(choices=[NS(delta=NS(content="draft"), finish_reason=None)], usage=None)
        yield NS(
            choices=[NS(
                delta=NS(tool_calls=[NS(index=i, id=c.id, function=c.function)
                                    for i, c in enumerate(calls)]),
                finish_reason=finish,
            )], usage=None,
        )
        yield NS(choices=[], usage=usage)

    return chunks()


def setup(responses):
    client = Client(responses)
    provider = OpenAIProvider(client=client)
    model = ModelInfo("openai", "test", 1000, 100)
    req = PreparedRequest(model, "", (), parameters={"estimated_input_tokens": 1})
    registry = ProviderRegistry()
    registry.register("openai", provider)
    runtime = ModelRuntime(registry, {"main": model})
    runtime.pricing["openai/test"] = {"input_per_million": "1", "output_per_million": "2"}
    records, settlements, resets = [], [], []

    async def reserve(amount, purpose, estimated_cost_cny=None, scope=None):
        return len(settlements)

    async def settle(reservation, actual, actual_cost_cny=None, cost_known=False):
        settlements.append((actual, actual_cost_cny, cost_known))

    async def emit(kind, data):
        resets.append((kind, data))

    context = NS(
        budget=NS(reserve=reserve, settle=settle),
        session=NS(append=lambda kind, data, **kw: records.append((kind, data))),
        check_cancelled=lambda: None, emit=emit,
    )
    return client, provider, req, runtime, context, records, settlements, resets


@pytest.mark.parametrize("streaming", [False, True])
def test_malformed_batch_keeps_usage_finish_and_safe_diagnostic(streaming):
    _, provider, req, _, _, _, _, _ = setup([response(streaming=streaming)])
    events = []

    async def run():
        async for event in provider.stream(req, None):
            events.append(event)

    with pytest.raises(ModelError) as failure:
        asyncio.run(run())
    error = failure.value
    assert error.kind == "tool_arguments"
    assert error.usage == Usage(10, 4)
    assert error.stop_reason == "length"
    assert error.diagnostic == {"exception_type": "JSONDecodeError", "error_code": None}
    assert '"value"' not in str(error)
    assert all(event.kind != "response" for event in events)


@pytest.mark.parametrize("streaming", [False, True])
def test_parse_retry_is_atomic_and_preserves_cost(streaming):
    client, _, req, runtime, context, records, settlements, resets = setup([
        response(streaming=streaming), response('{"value":2}', streaming=streaming, finish="tool_calls")
    ])
    result = asyncio.run(runtime.complete(req, context))
    assert len(client.calls) == 2
    assert [call.arguments for call in result.tool_calls] == [{"value": 1}, {"value": 2}]
    assert settlements == [(14, Decimal("0.000018"), True)] * 2
    ends = [data for kind, data in records if kind == "attempt_end"]
    assert ends[0]["state"] == "failed" and ends[0]["kind"] == "tool_arguments"
    assert ends[0]["stop_reason"] == "length"
    assert ends[0]["exception_type"] == "JSONDecodeError"
    assert resets == [("attempt_reset", {"kind": "tool_arguments", "retry": 1})]


def test_parse_retry_happens_only_once():
    client, _, req, runtime, context, _, settlements, _ = setup([response()] * 3)
    with pytest.raises(ModelError) as failure:
        asyncio.run(runtime.complete(req, context))
    assert failure.value.kind == "tool_arguments"
    assert len(client.calls) == len(settlements) == 2


def test_parse_retry_respects_shared_physical_limit():
    client, _, req, runtime, context, _, settlements, resets = setup([response()] * 2)
    attempts = {"attempts": 2, "limit": 3}
    with pytest.raises(ModelError):
        asyncio.run(runtime.complete(req, context, attempt_budget=attempts))
    assert attempts["attempts"] == 3
    assert len(client.calls) == len(settlements) == 1
    assert resets == []


def test_cancellation_during_reset_prevents_retry():
    client, _, req, runtime, context, _, settlements, _ = setup([response()] * 2)
    cancelled = False

    async def emit(kind, data):
        nonlocal cancelled
        cancelled = True

    def check():
        if cancelled:
            raise asyncio.CancelledError

    context.emit, context.check_cancelled = emit, check
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.complete(req, context))
    assert len(client.calls) == len(settlements) == 1


def test_nonobject_arguments_keep_usage_without_parse_retry():
    client, _, req, runtime, context, records, settlements, resets = setup([response("[]")])
    with pytest.raises(ModelError) as failure:
        asyncio.run(runtime.complete(req, context))
    assert failure.value.kind == "invalid"
    assert len(client.calls) == 1 and resets == []
    assert settlements == [(14, Decimal("0.000018"), True)]
    assert next(data for kind, data in records if kind == "attempt_end")["stop_reason"] == "length"


def test_parse_and_transient_retries_share_physical_limit(monkeypatch):
    from weyra.contracts import ModelResponse, StreamEvent

    class Provider:
        def __init__(self):
            self.calls = 0

        async def stream(self, req, context):
            self.calls += 1
            if self.calls == 1:
                error = ModelError("tool_arguments")
                raise error
            if self.calls <= 3:
                raise ModelError("transient")
            yield StreamEvent("response", ModelResponse("done"))

        async def close(self):
            pass

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("weyra.core.model_runtime.asyncio.sleep", sleep)
    _, _, req, runtime, context, _, _, _ = setup([])
    provider, delays = Provider(), []
    runtime.registry.providers["openai"] = provider
    attempts = {"attempts": 0, "limit": 4}
    assert asyncio.run(runtime.complete(req, context, attempt_budget=attempts)).content == "done"
    assert provider.calls == attempts["attempts"] == 4
    assert len(delays) == 2


def test_parse_failure_after_accepted_response_never_retries():
    from weyra.contracts import ModelResponse, StreamEvent

    class Provider:
        calls = 0

        async def stream(self, req, context):
            self.calls += 1
            yield StreamEvent("response", ModelResponse("already accepted"))
            raise ModelError("tool_arguments")

        async def close(self):
            pass

    _, _, req, runtime, context, _, _, resets = setup([])
    provider = Provider()
    runtime.registry.providers["openai"] = provider
    with pytest.raises(ModelError):
        asyncio.run(runtime.complete(req, context))
    assert provider.calls == 1 and resets == []

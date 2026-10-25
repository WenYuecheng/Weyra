import asyncio
from types import SimpleNamespace as NS

import pytest

from weyra.contracts import Message, ModelInfo, ModelResponse, PreparedRequest, ToolCall, Usage
from weyra.model_runtime import (
    AnthropicProvider,
    ModelError,
    ModelRuntime,
    OpenAIProvider,
    ProviderRegistry,
    ScriptedProvider,
    default_model,
)


def request(provider="openai", messages=()):
    return PreparedRequest(
        ModelInfo(provider, "test", 1000, 100, supports_reasoning=True),
        "system",
        tuple(messages),
        parameters={"estimated_input_tokens": 10},
    )


class Client:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error
        self.chat = NS(completions=self)
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


async def collect(provider, req):
    return [event async for event in provider.stream(req, None)]


def test_unknown_model_requires_explicit_capacities():
    with pytest.raises(ValueError):
        default_model("custom", "unknown")
    assert default_model("custom", "unknown", 1000, 100).context_window == 1000


def test_openai_pairing_reasoning_and_usage():
    response = NS(
        choices=[
            NS(
                message=NS(content="done", tool_calls=[], reasoning_content="thought"),
                finish_reason="stop",
            )
        ],
        usage=NS(prompt_tokens=12, completion_tokens=3, prompt_cache_hit_tokens=8),
    )
    client = Client(response)
    provider = OpenAIProvider(provider_id="deepseek", client=client)
    messages = [
        Message(
            "assistant",
            tool_calls=(ToolCall("a", "lookup", {"x": 1}),),
            provider_data={
                "provider_id": "deepseek",
                "model_id": "test",
                "reasoning_content": "prior",
            },
        ),
        Message("tool", "result", tool_call_id="a"),
        Message(
            "assistant",
            "foreign",
            provider_data={"provider_id": "anthropic", "reasoning_content": "secret"},
        ),
    ]
    events = asyncio.run(collect(provider, request("deepseek", messages)))
    encoded = client.calls[0]["messages"]
    assert encoded[1]["tool_calls"][0]["function"]["arguments"] == '{"x":1}'
    assert encoded[1]["reasoning_content"] == "prior"
    assert "reasoning_content" not in encoded[3]
    assert client.calls[0]["stream"] is True
    assert events[-1].data.usage == Usage(12, 3, 8, None)
    assert events[-1].kind == "response"


def test_anthropic_preserves_signed_blocks_and_adds_cache_input():
    blocks = [
        {"type": "thinking", "thinking": "prior", "signature": "signed"},
        {"type": "text", "text": "answer"},
    ]
    response = NS(
        content=[NS(type="text", text="done")],
        stop_reason="end_turn",
        usage=NS(
            input_tokens=2,
            output_tokens=3,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=7,
        ),
    )
    client = Client(response)
    req = request(
        "anthropic",
        [
            Message(
                "assistant",
                "answer",
                provider_data={"provider_id": "anthropic", "model_id": "test", "content": blocks},
            ),
            Message("user", "next"),
        ],
    )
    events = asyncio.run(collect(AnthropicProvider(client=client), req))
    assert client.calls[0]["messages"][0]["content"] == blocks
    assert events[-1].data.usage == Usage(14, 3, 5, 7)


def test_runtime_settles_unknown_failure_and_records_request():
    class Budget:
        async def reserve(self, amount, purpose):
            self.reserved = (amount, purpose)
            return "r"

        async def settle(self, reservation, actual):
            self.settled = (reservation, actual)

    class Session:
        def append(self, *args, **kwargs):
            self.record = (args, kwargs)

    context = NS(budget=Budget(), session=Session(), check_cancelled=lambda: None)
    registry = ProviderRegistry()
    provider = ScriptedProvider([ModelResponse("ok", usage=Usage(4, 2)), RuntimeError("failed")])
    registry.register("openai", provider)
    runtime = ModelRuntime(registry, {"main": request().model})
    assert asyncio.run(runtime.complete(request(), context)).content == "ok"
    assert context.budget.reserved == (110, "main")
    assert context.budget.settled == ("r", 6)
    with pytest.raises(ModelError):
        asyncio.run(runtime.complete(request(), context))
    assert context.budget.settled == ("r", None)
    assert context.session.record[0][0] == "model_usage"


def test_error_classification_and_orphan_tool_rejection():
    client = Client(error=type("RateLimitError", (Exception,), {"status_code": 429})("secret-key"))
    with pytest.raises(ModelError) as error:
        asyncio.run(collect(OpenAIProvider(client=client), request()))
    assert error.value.kind == "transient"
    assert error.value.status_code == 429
    assert "secret-key" not in str(error.value)
    with pytest.raises(ModelError):
        asyncio.run(
            collect(
                OpenAIProvider(client=client),
                request(messages=[Message("tool", "x", tool_call_id="missing")]),
            )
        )


def test_budget_is_settled_when_stream_is_closed_early():
    class Budget:
        async def reserve(self, amount, purpose):
            return "reserved"

        async def settle(self, reservation, actual):
            self.settled = (reservation, actual)

    budget = Budget()
    context = NS(
        budget=budget, session=NS(append=lambda *a, **k: None), check_cancelled=lambda: None
    )
    registry = ProviderRegistry()
    registry.register("openai", ScriptedProvider([ModelResponse("hello", usage=Usage(2, 3))]))

    async def run():
        stream = ModelRuntime(registry, {"main": request().model}).stream(request(), context)
        assert (await anext(stream)).kind == "text"
        await stream.aclose()

    asyncio.run(run())
    assert budget.settled == ("reserved", None)


def test_context_overflow_error_is_normalized_without_exposing_body():
    exc = type(
        "BadRequestError",
        (Exception,),
        {"status_code": 400, "body": {"error": {"code": "context_length_exceeded"}}},
    )("private request body")
    with pytest.raises(ModelError) as error:
        asyncio.run(collect(OpenAIProvider(client=Client(error=exc)), request()))
    assert error.value.kind == "overflow"
    assert "private" not in str(error.value)


class Chunks:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def test_openai_stream_assembles_tool_arguments_and_usage():
    chunks = Chunks(
        [
            NS(
                choices=[
                    NS(
                        delta=NS(
                            content="hi",
                            tool_calls=[
                                NS(index=0, id="a", function=NS(name="lookup", arguments='{"x":'))
                            ],
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            NS(
                choices=[
                    NS(
                        delta=NS(
                            content=None,
                            tool_calls=[
                                NS(index=0, id=None, function=NS(name=None, arguments="1}"))
                            ],
                        ),
                        finish_reason="length",
                    )
                ],
                usage=None,
            ),
            NS(choices=[], usage=NS(prompt_tokens=10, completion_tokens=4)),
        ]
    )
    client = Client(chunks)
    events = asyncio.run(collect(OpenAIProvider(client=client), request()))
    assert client.calls[0]["stream"] is True
    assert events[0].data == "hi"
    assert events[-1].data.tool_calls == (ToolCall("a", "lookup", {"x": 1}),)
    assert events[-1].data.stop_reason == "length"
    assert events[-1].data.usage.total_tokens == 14


def test_openai_disconnect_emits_no_final_response():
    client = Client(
        Chunks(
            [
                NS(choices=[NS(delta=NS(content="partial"), finish_reason=None)], usage=None),
                ConnectionError("gone"),
            ]
        )
    )
    events = []

    async def run():
        async for event in OpenAIProvider(client=client).stream(request(), None):
            events.append(event)

    with pytest.raises(ModelError):
        asyncio.run(run())
    assert [event.kind for event in events] == ["text"]


def test_anthropic_stream_yields_deltas_and_preserves_final_signature():
    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return Chunks(
                [NS(type="content_block_delta", delta=NS(type="text_delta", text="done"))]
            ).__aiter__()

        async def get_final_message(self):
            return NS(
                content=[
                    NS(type="thinking", thinking="private", signature="signed"),
                    NS(type="text", text="done"),
                ],
                stop_reason="end_turn",
                usage=NS(input_tokens=2, output_tokens=3),
            )

    class StreamingClient:
        def __init__(self):
            self.messages = self

        def stream(self, **kwargs):
            return Stream()

    events = asyncio.run(collect(AnthropicProvider(client=StreamingClient()), request("anthropic")))
    assert events[0].data == "done"
    assert events[-1].data.provider_data["content"][0]["signature"] == "signed"


@pytest.mark.parametrize("arguments", ["{broken", "[]"])
def test_malformed_tool_arguments_are_invalid_and_never_final(arguments):
    chunks = Chunks(
        [
            NS(
                choices=[
                    NS(
                        delta=NS(
                            content=None,
                            tool_calls=[
                                NS(index=0, id="a", function=NS(name="lookup", arguments=arguments))
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ],
                usage=None,
            )
        ]
    )
    events = []

    async def run():
        async for event in OpenAIProvider(client=Client(chunks)).stream(request(), None):
            events.append(event)

    with pytest.raises(ModelError) as error:
        asyncio.run(run())
    assert error.value.kind == ("tool_arguments" if arguments == "{broken" else "invalid")
    assert not any(event.kind == "response" for event in events)


def test_runtime_fallback_and_validation():
    registry = ProviderRegistry()
    registry.register("openai", ScriptedProvider())
    runtime = ModelRuntime(registry, {"main": request().model})
    assert runtime.resolve("compression") == request().model
    with pytest.raises(ValueError):
        ModelRuntime(registry, {})
    with pytest.raises(TypeError):
        registry.register("bad", object())


def test_runtime_does_not_turn_persistence_errors_into_model_errors():
    from weyra.runtime_state import Budget

    class Broken:
        def append(self, *args, **kwargs):
            raise OSError("disk failed")

    context = NS(id="turn", budget=Budget(), session=Broken(), check_cancelled=lambda: None)
    registry = ProviderRegistry()
    provider = ScriptedProvider([ModelResponse("unused")])
    registry.register("openai", provider)
    runtime = ModelRuntime(registry, {"main": request().model})
    with pytest.raises(OSError):
        asyncio.run(runtime.complete(request(), context))
    assert not provider.requests
    assert context.budget.unknown_calls == 1


def test_runtime_caps_and_tagged_usage():
    from weyra.runtime_state import Budget

    records = []
    context = NS(
        id="child-turn",
        budget=Budget(),
        session=NS(append=lambda kind, data, **kw: records.append((kind, data))),
        check_cancelled=lambda: None,
    )
    registry = ProviderRegistry()
    provider = ScriptedProvider([ModelResponse("ok", usage=Usage(5, 2))])
    registry.register("openai", provider)
    runtime = ModelRuntime(registry, {"main": request().model})
    bad = PreparedRequest(request().model, "", (), parameters={"max_output_tokens": 101})
    with pytest.raises(ModelError):
        asyncio.run(runtime.complete(bad, context))
    assert context.budget.calls == 0
    asyncio.run(runtime.complete(request(), context))
    ledger = next(data for kind, data in records if kind == "model_usage")
    assert ledger["turn_id"] == "child-turn"
    assert ledger["total_tokens"] == 7
    assert ledger["request_id"]


def test_unsupported_parameters_never_reach_sdk():
    client = Client(
        NS(choices=[NS(message=NS(content="ok", tool_calls=[]), finish_reason="stop")], usage=None)
    )
    req = PreparedRequest(
        request().model,
        "",
        (),
        parameters={"api_key": "private", "estimated_input_tokens": 1, "unknown_option": True},
    )
    asyncio.run(collect(OpenAIProvider(client=client), req))
    assert not {"api_key", "estimated_input_tokens", "unknown_option"} & client.calls[0].keys()


def test_cancellation_closes_provider_and_charges_unknown_usage():
    from weyra.runtime_state import Budget

    closed = []

    class Paused:
        async def stream(self, request, context):
            try:
                yield __import__("weyra.contracts", fromlist=["StreamEvent"]).StreamEvent(
                    "text", "partial"
                )
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        async def close(self):
            pass

    async def run():
        registry = ProviderRegistry()
        registry.register("openai", Paused())
        context = NS(
            id="cancel",
            budget=Budget(),
            session=NS(append=lambda *a, **k: None),
            check_cancelled=lambda: None,
        )
        stream = ModelRuntime(registry, {"main": request().model}).stream(request(), context)
        assert (await anext(stream)).kind == "text"
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]
        assert context.budget.unknown_calls == 1

    asyncio.run(run())


def test_anthropic_replays_redacted_thinking_and_tool_error_status():
    client = Client(NS(content=[NS(type="text", text="done")], stop_reason="end_turn", usage=None))
    blocks = [
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "tool_use", "id": "a", "name": "lookup", "input": {}},
    ]
    messages = [
        Message(
            "assistant",
            tool_calls=(ToolCall("a", "lookup"),),
            provider_data={"provider_id": "anthropic", "model_id": "test", "content": blocks},
        ),
        Message("tool", "denied", tool_call_id="a", provider_data={"tool_status": "denied"}),
    ]
    asyncio.run(collect(AnthropicProvider(client=client), request("anthropic", messages)))
    assert client.calls[0]["messages"][0]["content"] == blocks
    assert client.calls[0]["messages"][1]["content"][0]["is_error"] is True


def test_openai_compatible_reports_cache_creation_when_present():
    client = Client(
        NS(
            choices=[NS(message=NS(content="ok", tool_calls=[]), finish_reason="stop")],
            usage=NS(
                prompt_tokens=100,
                completion_tokens=2,
                prompt_tokens_details=NS(cached_tokens=20, cache_write_tokens=30),
            ),
        )
    )
    response = asyncio.run(collect(OpenAIProvider(client=client), request()))[-1].data
    assert response.usage == Usage(100, 2, 20, 30)


@pytest.mark.parametrize("values", [(-1, 2), (2, -1), (True, 2), (2, 1, 3), (2, 1, 1, 2)])
def test_invalid_provider_usage_cannot_refund_budget(values):
    with pytest.raises(ValueError):
        Usage(*values)


def test_unknown_provider_stop_reasons_are_not_success():
    openai = Client(
        NS(
            choices=[
                NS(message=NS(content="partial", tool_calls=[]), finish_reason="new_partial_reason")
            ],
            usage=NS(prompt_tokens=2, completion_tokens=1),
        )
    )
    anthropic = Client(
        NS(
            content=[NS(type="text", text="partial")],
            stop_reason="pause_turn",
            usage=NS(input_tokens=2, output_tokens=1),
        )
    )
    assert (
        asyncio.run(collect(OpenAIProvider(client=openai), request()))[-1].data.stop_reason
        != "stop"
    )
    assert (
        asyncio.run(collect(AnthropicProvider(client=anthropic), request("anthropic")))[
            -1
        ].data.stop_reason
        != "stop"
    )


def test_runtime_records_client_first_text_latency(tmp_path):
    from weyra.runtime_state import Budget, RunContext
    from weyra.session import SessionStore
    from weyra.contracts import StreamEvent

    class Provider:
        async def stream(self, request, context):
            yield StreamEvent("text", "ok")
            yield StreamEvent("response", ModelResponse("ok", usage=Usage(1, 1)))

        async def close(self):
            pass

    async def run():
        registry = ProviderRegistry()
        registry.register("p", Provider())
        model = ModelInfo("p", "m", 10000, 100)
        runtime = ModelRuntime(registry, {"main": model})
        session = SessionStore(project=tmp_path)
        await runtime.complete(
            PreparedRequest(model, "", ()), RunContext(tmp_path, session, Budget())
        )
        usage = session.latest("model_usage")
        assert 0 <= usage["first_text_seconds"] <= usage["request_duration_seconds"]
        session.close()

    asyncio.run(run())

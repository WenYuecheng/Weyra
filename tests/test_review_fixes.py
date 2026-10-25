"""Regression cases established by the cross-review, without paid requests."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from weyra.contracts import ModelInfo, ModelResponse, PreparedRequest
from weyra.model_runtime import ModelError, OpenAIProvider, _error
from weyra.mcp_client import MCPClient


@pytest.mark.parametrize("message,code,kind", [
    ("input length exceeds the model's context window", "", "overflow"),
    ("input length exceeds maximum context capacity", "", "overflow"),
    ("unsupported parameter", "context_feature_unsupported", "invalid"),
    ("file path is too long", "", "invalid"),
    ("reduce the length of tool name", "", "invalid"),
    ("private input", "context_length_exceeded", "overflow"),
])
def test_error_classification_is_specific(message, code, kind):
    error = type("ProviderError", (Exception,), {
        "status_code": 400, "body": {"error": {"code": code}},
    })(message)
    normalized = _error(error)
    assert normalized.kind == kind
    assert message not in str(normalized)


@pytest.mark.parametrize("index", [None, -1, True, "0"])
def test_ambiguous_stream_index_is_explicitly_rejected(index):
    async def run():
        async def chunks():
            yield NS(choices=[NS(finish_reason="tool_calls", delta=NS(
                tool_calls=[NS(index=index, id="a", function=NS(name="read_file", arguments="{}"))]
            ))])
        async def create(**kwargs):
            return chunks()
        client = NS(chat=NS(completions=NS(create=create)))
        request = PreparedRequest(ModelInfo("test", "model", 10000, 100), "system", ())
        with pytest.raises(ModelError, match="tool call index") as failure:
            _ = [event async for event in OpenAIProvider(client=client).stream(request, None)]
        assert failure.value.kind == "invalid"
    asyncio.run(run())


@pytest.mark.parametrize("text,expected", [
    ('["preference"]', 1),
    ('```json\n["preference", "unknown", "preference"]\n```', 1),
    ('```\n["preference"]\n```', 1),
    ('Explanation ["preference"]', 0),
    ('["preference", 123]', 0),
    ('{"id":"preference"}', 0),
])
def test_memory_selection_accepts_only_complete_arrays(tmp_path, text, expected):
    from weyra.agent import Agent
    from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
    async def run():
        registry = ProviderRegistry()
        registry.register("test", ScriptedProvider([ModelResponse(text), ModelResponse("done")]))
        runtime = ModelRuntime(registry, {"main": ModelInfo("test", "model", 200000, 1000)})
        agent = Agent(tmp_path, runtime, enable_evolution=False)
        try:
            agent.memory.create("preference", "Prefer concise answers")
            await agent.run_once("Use my preference")
            assert sum(m.source == "memory" for m in agent.session.project()) == expected
        finally:
            await agent.close()
    asyncio.run(run())


def test_failed_mcp_reader_rejects_new_requests_without_send():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b"not a protocol message\n")
        client = MCPClient("unused", timeout=0.01)
        client.process = NS(stdout=reader)
        future = asyncio.get_running_loop().create_future()
        client.pending[99] = future
        await client._read()
        with pytest.raises(ConnectionError):
            await future
        async def send(value):
            pytest.fail("A failed connection must not send another request")
        client._send = send
        with pytest.raises(ConnectionError):
            await client.request("tools/list")
    asyncio.run(run())


def test_real_mcp_protocol_failure_requires_explicit_reconnect():
    import sys

    script = '''import sys,json
for line in sys.stdin:
    value=json.loads(line)
    if 'id' not in value: continue
    if value['method']=='break':
        print('invalid protocol output',flush=True)
    else:
        print(json.dumps({'jsonrpc':'2.0','id':value['id'],'result':{}}),flush=True)
'''

    async def run():
        client = MCPClient(sys.executable, ["-u", "-c", script], trusted=True, timeout=1)
        try:
            await client.start()
            original = client.process
            with pytest.raises(ConnectionError):
                await client.request("break")
            with pytest.raises(ConnectionError, match="reconnect"):
                await client.start()
            with pytest.raises(ConnectionError, match="reconnect"):
                await client.request("tools/list")
            assert not client.pending
            await client.close()
            assert original.returncode is not None
            await client.start()
            assert client.process is not original
            assert await client.request("tools/list") == {}
        finally:
            await client.close()
    asyncio.run(run())

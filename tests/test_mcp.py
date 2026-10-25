import asyncio
import pytest
from weyra.mcp_client import MCPClient


def test_untrusted():
    async def run():
        with pytest.raises(PermissionError):
            await MCPClient("python").start()

    asyncio.run(run())


def test_local_fixture(tmp_path):
    import sys

    fixture = tmp_path / "fixture.py"
    fixture.write_text("""import sys,json
for line in sys.stdin:
 v=json.loads(line)
 if 'id' not in v: continue
 method=v['method']
 result={'tools':[{'name':'echo','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}}]} if method=='tools/list' else ({'content':[{'type':'text','text':v['params']['arguments']['text']}]} if method=='tools/call' else {})
 print(json.dumps({'jsonrpc':'2.0','id':v['id'],'result':result}),flush=True)
""")

    async def run():
        client = MCPClient(sys.executable, [str(fixture)], trusted=True)
        assert (await client.list_tools())[0]["name"] == "echo"
        assert (await client.call_tool("echo", {"text": "hello"}))["content"][0]["text"] == "hello"
        await client.close()
        assert not client.pending

    asyncio.run(run())


def test_bridge_respects_actual_schema_and_permission(tmp_path):
    from types import SimpleNamespace
    from weyra.tool_runtime import ToolRuntime
    from weyra.contracts import ToolCall
    from weyra.mcp_client import MCPManager

    class Session:
        def append(self, *args, **kwargs):
            pass

    class Client:
        trusted = True
        called = False

        async def list_tools(self):
            return [
                {
                    "name": "write",
                    "inputSchema": {
                        "type": "object",
                        "required": ["text"],
                        "properties": {"text": {"type": "string"}},
                        "additionalProperties": False,
                    },
                }
            ]

        async def call_tool(self, name, args):
            self.called = True
            return {}

    async def run():
        runtime = ToolRuntime(tmp_path, Session())
        manager = MCPManager(runtime)
        client = Client()
        manager.clients["fixture"] = client
        await manager.search()
        ctx = SimpleNamespace(
            id="t", permission_mode="dontAsk", services={}, check_cancelled=lambda: None, emit=None
        )
        result = await runtime.execute(
            ToolCall(
                "one", "mcp_call", {"name": "mcp__fixture__write", "arguments": {"text": "x"}}
            ),
            ctx,
        )
        assert result.status == "denied"
        ctx.permission_mode = "bypass"
        result = await runtime.execute(
            ToolCall("two", "mcp_call", {"name": "mcp__fixture__write", "arguments": {}}), ctx
        )
        assert result.status == "invalid"
        assert not client.called

    asyncio.run(run())

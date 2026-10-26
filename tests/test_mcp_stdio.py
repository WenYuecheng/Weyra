"""Real subprocess protocol evidence, independent of model availability."""
import asyncio
from pathlib import Path
import sys

import pytest

from weyra.mcp_client import MCPClient

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_server.py"


def test_real_stdio_initialize_call_schema_change_timeout_shutdown():
    async def run():
        client = MCPClient(sys.executable, [str(FIXTURE)], trusted=True, timeout=1)
        try:
            first = await client.list_tools()
            assert first[0]["inputSchema"]["required"] == ["text"]
            result = await client.call_tool("echo", {"text": "你好"})
            assert result["content"][0]["text"] == "你好"
            await client.call_tool("change_schema", {})
            second = await client.list_tools()
            assert second[0]["inputSchema"]["required"] == ["value"]
            with pytest.raises(RuntimeError, match="schema"):
                await client.call_tool("echo", {"text": "old"})
            assert (await client.call_tool("echo", {"value": "new"}))["content"][0]["text"] == "new"
            process = client.process
            client.timeout = 0.03
            with pytest.raises(TimeoutError, match="unknown"):
                await client.call_tool("slow", {})
            assert client.pending == {}
        finally:
            await client.close()
        assert process.returncode is not None
        assert client.process is None and not client.pending
    asyncio.run(run())

"""Trusted local stdio MCP with bounded requests and policy-preserving bridges."""

from __future__ import annotations
import asyncio
import json
import os
from .contracts import ToolCall, ToolSpec, ToolResult


class MCPClient:
    def __init__(self, command, args=(), trusted=False, env=None, cwd=None, timeout=10):
        self.command, self.args, self.trusted = command, args, trusted
        self.env = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        # Explicitly supplied environment is an allowlist, never inherited credentials.
        self.env.update(env or {})
        self.cwd, self.timeout = cwd, timeout
        self.process = None
        self.pending = {}
        self.counter = 0
        self.reader_task = None
        self.write_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()

    async def start(self):
        async with self.start_lock:
            return await self._start()

    async def _start(self):
        if not self.trusted:
            raise PermissionError("MCP server must be explicitly trusted before launch")
        if self.process:
            return self
        self.process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            env=self.env,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=4 * 1024 * 1024,
        )
        self.reader_task = asyncio.create_task(self._read())
        try:
            await self.request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "weyra", "version": "0.1.0"},
                },
            )
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            await self.close()
            raise
        return self

    async def _send(self, value):
        async with self.write_lock:
            self.process.stdin.write((json.dumps(value) + "\n").encode())
            await self.process.stdin.drain()

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                future = self.pending.get(value.get("id"))
                if future and not future.done():
                    if "error" in value:
                        future.set_exception(RuntimeError(str(value["error"])))
                    else:
                        future.set_result(value.get("result"))
        except Exception as exc:
            error = exc
        else:
            error = ConnectionError("MCP server closed")
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)

    async def request(self, method, params=None):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
            return await asyncio.wait_for(future, self.timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("MCP request timed out; effect is unknown") from exc
        finally:
            self.pending.pop(request_id, None)

    async def list_tools(self):
        await self.start()
        tools = []
        cursor = None
        while True:
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name, arguments):
        await self.start()
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def close(self):
        if self.process:
            import signal

            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await asyncio.wait_for(self.process.wait(), 2)
            except ProcessLookupError:
                pass
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.reader_task:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("MCP client closed"))
        self.pending.clear()
        self.process = None


class MCPManager:
    def __init__(self, runtime):
        self.runtime = runtime
        self._owns_clients = True
        self.clients = {}
        self.loaded = set()
        self.identities = {}
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        }
        runtime.register(
            ToolSpec(
                "mcp_search",
                "Discover trusted MCP tools",
                schema,
                self._search_handler,
                "read",
                "read",
            )
        )
        runtime.register(
            ToolSpec(
                "mcp_call",
                "Invoke a discovered MCP tool",
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
                    "required": ["name", "arguments"],
                    "additionalProperties": False,
                },
                self._call_handler,
                "dispatch",
                "read",
            )
        )

    def attach(self, runtime):
        """Attach shared trusted connections to a child policy boundary."""
        child = MCPManager(runtime)
        child._owns_clients = False
        child.clients = self.clients
        child.loaded = set(self.loaded)
        child.identities = dict(self.identities)
        for identity in child.identities:
            runtime.register(self.runtime.specs[identity])
        return child

    def register(self, name, config=None, **kwargs):
        config = {**(config or {}), **kwargs}
        self.clients[name] = MCPClient(**config)

    async def search(self, query=""):
        for server, client in self.clients.items():
            if server in self.loaded or not client.trusted:
                continue
            for tool in await client.list_tools():
                identity = f"mcp__{server}__{tool['name']}"
                self.identities[identity] = (client, tool["name"])

                async def invoke(args, ctx, client=client, name=tool["name"]):
                    result = await client.call_tool(name, args)
                    return ToolResult(
                        "",
                        name,
                        json.dumps(result),
                        status="error" if result.get("isError") else "success",
                        started=True,
                        effect="unknown" if result.get("isError") else "completed",
                        structured=result,
                    )

                self.runtime.register(
                    ToolSpec(
                        identity,
                        tool.get("description", ""),
                        tool.get("inputSchema", {"type": "object"}),
                        invoke,
                        "exclusive",
                        "mcp",
                    )
                )
            self.loaded.add(server)
        return [
            spec.declaration()
            for name, spec in sorted(self.runtime.specs.items())
            if name in self.identities and query.lower() in (name + " " + spec.description).lower()
        ]

    async def _search_handler(self, args, context):
        return await self.search(args.get("query", ""))

    async def _call_handler(self, args, context):
        if args["name"] not in self.identities:
            raise ValueError("Search for the MCP tool before calling it")
        return await self.runtime.execute(
            ToolCall("mcp-" + str(id(args)), args["name"], args["arguments"]), context
        )

    async def close(self):
        if self._owns_clients:
            await asyncio.gather(*(client.close() for client in self.clients.values()))

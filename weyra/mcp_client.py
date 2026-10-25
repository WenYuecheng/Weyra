"""基于本地 Stdio JSON-RPC 2.0 的安全受信模型上下文协议（Model Context Protocol, MCP）客户端。

架构定位与设计哲学:
    本模块负责对接符合 Anthropic MCP 协议规范的外部工具与数据服务。
    在企业级 Agent 架构中，外部 MCP 服务通常以独立子进程运行，具有不可预测的执行时长与输入输出。
    本模块通过零信任安全环境白名单、POSIX 进程组硬隔离、显式信任授权门禁、双向 JSON-RPC 2.0 异步解析流
    以及分级两阶段工具注册（Search-then-Call），为 Weyra 提供生产级的扩展工具桥接能力。

开源框架借鉴与协议实现溯源:
    1. Anthropic Model Context Protocol (MCP 2024-11-05 Specification):
       - 完整实现基于标准输入输出（Stdio）的双向 JSON-RPC 2.0 协议流（initialize, notifications/initialized,
         tools/list, tools/call），支持大容量工具集的分页游标遍历（Cursor Pagination）。
    2. DeepSeek-Harness (packages/mcp & packages/subprocess):
       - 严格的安全环境变量白名单（Environment Allowlist）：默认仅继承系统基础变量（PATH, LANG, LC_ALL, TMPDIR），
         严禁无节制继承宿主环境中的敏感 API 凭证与鉴权密钥，防止 MCP 恶意子进程窃取机密。
       - 显式受信门禁（Explicit Trust Gate）：未显式标记 trusted=True 的 MCP 服务严禁调用 start() 启动，
         防范未授权本地命令注入。
       - 级联优雅终止与进程组销毁（killpg Escalation）：子进程生命周期绑定独立会话组（start_new_session=True），
         关闭时优先发送 SIGTERM 优雅退出；2秒超时后自动级联升级为 SIGKILL 强杀，彻底杜绝孤儿僵尸进程。
       - 子代理策略隔离桥接（attach）：通过共享受信连接池支持 Subagent 快速继承 MCP 工具，无需重复派生进程。
"""

from __future__ import annotations
import asyncio
import json
import os
from .contracts import ToolCall, ToolSpec, ToolResult


class MCPClient:
    """受控标准输入输出（Stdio）MCP 协议通信客户端。

    管理单个外部 MCP 子进程的生命周期、双向异步 JSON-RPC 2.0 通信流以及超时熔断。
    """

    def __init__(self, command, args=(), trusted=False, env=None, cwd=None, timeout=10):
        """初始化 MCPClient。

        Args:
            command: 启动 MCP 服务的命令可执行文件路径。
            args: 命令行参数元组。
            trusted: 显式受信任标识；只有为 True 时才允许被拉起。
            env: 显式注入的环境变量白名单。
            cwd: 运行工作目录。
            timeout: 单次 RPC 请求等待超时时间（秒）。
        """
        self.command, self.args, self.trusted = command, args, trusted
        self.env = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        # 显式提供的环境是白名单，严禁静默继承宿主非安全环境变量
        self.env.update(env or {})
        self.cwd, self.timeout = cwd, timeout
        self.process = None
        self.pending = {}
        self.counter = 0
        self.reader_task = None
        self._reader_error = None
        self.write_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()

    async def start(self):
        """线程安全地启动 MCP 客户端连接与初始化协议握手。"""
        async with self.start_lock:
            return await self._start()

    async def _start(self):
        """实际启动子进程并完成 initialize 握手协议。"""
        if not self.trusted:
            raise PermissionError("MCP server must be explicitly trusted before launch")
        if self.process:
            if self._reader_error:
                raise ConnectionError("MCP connection failed; close and reconnect")
            return self
        self._reader_error = None
        # 采用独立进程组启动，防止子进程接收宿主终端信号干扰
        self.process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            env=self.env,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=4 * 1024 * 1024,  # 4MiB 缓冲区，适配包含大型模式图谱的 tools/list
        )
        self.reader_task = asyncio.create_task(self._read())
        try:
            # 发送 MCP 形式化握手包
            await self.request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "weyra", "version": "0.1.0"},
                },
            )
            # 发送已初始化单向通知
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            await self.close()
            raise
        return self

    async def _send(self, value):
        """在写互斥锁保护下向子进程标准输入写入换行分隔的 JSON 字符串。"""
        async with self.write_lock:
            self.process.stdin.write((json.dumps(value) + "\n").encode())
            await self.process.stdin.drain()

    async def _read(self):
        """持续从子进程标准输出中按行读取并分发 JSON-RPC 响应消息。"""
        try:
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    raise ValueError("Invalid MCP envelope")
                # 忽略服务端发起的单向通知
                if "id" not in value and isinstance(value.get("method"), str):
                    continue
                if type(value.get("id")) not in (str, int):
                    raise ValueError("Invalid MCP response ID")
                if ("result" in value) == ("error" in value):
                    raise ValueError("Invalid MCP response")
                future = self.pending.get(value.get("id"))
                if future and not future.done():
                    if "error" in value:
                        future.set_exception(RuntimeError(str(value["error"])))
                    else:
                        future.set_result(value.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception:
            # 协议原始输出可能包含服务端私密信息，仅记录状态异常
            pass
        finally:
            self._reader_error = ConnectionError("MCP connection closed or protocol failed")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(str(self._reader_error)))

    async def request(self, method, params=None):
        """发起同步等待的 JSON-RPC 请求并挂起 Future。

        Args:
            method: RPC 方法名称。
            params: 参数字典。

        Returns:
            Any: 服务端返回的 result 字段。

        Raises:
            TimeoutError: 超时未响应。
            ConnectionError: 连接异常或子进程断开。
        """
        if self._reader_error:
            raise ConnectionError("MCP connection failed; close and reconnect")
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
        """列出该 MCP 服务端声明的所有工具，支持深度游标分页。"""
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
        """调用指定工具并回传结果。"""
        await self.start()
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def close(self):
        """双阶段级联终止子进程并清理所有悬挂协程与管道。"""
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
    """MCP 服务注册发现与运行时调度管理器。

    实现工具命名前缀隔离、按需两阶段发现（mcp_search / mcp_call）以及子代理连接共享。
    """

    def __init__(self, runtime):
        """初始化 MCPManager 并注册两阶段发现门禁工具 (mcp_search / mcp_call)。

        【两阶段按需加载设计】
        针对包含大量工具的 MCP 服务，若全部注入 System Prompt 会严重占用上下文 Token。
        通过注册 `mcp_search` 与 `mcp_call`，模型仅在需要时检索具体工具 Schema 并发起调用。

        Args:
            runtime: 绑定的宿主工具运行时实例 ToolRuntime。
        """
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
        # 注册两阶段发现工具：mcp_search 用于检索可用 MCP 工具，避免一次性向 Prompt 注入过多 Schema
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
        """将当前已建立的受信任连接共享给子代理或衍生策略边界。"""
        child = MCPManager(runtime)
        child._owns_clients = False
        child.clients = self.clients
        child.loaded = set(self.loaded)
        child.identities = dict(self.identities)
        for identity in child.identities:
            runtime.register(self.runtime.specs[identity])
        return child

    def register(self, name, config=None, **kwargs):
        """注册一个命名 MCP 服务配置。"""
        config = {**(config or {}), **kwargs}
        self.clients[name] = MCPClient(**config)

    async def search(self, query=""):
        """检索匹配的 MCP 工具，并按需完成工具在运行时中的动态声明注册。"""
        for server, client in self.clients.items():
            if server in self.loaded or not client.trusted:
                continue
            for tool in await client.list_tools():
                identity = f"mcp__{server}__{tool['name']}"
                self.identities[identity] = (client, tool["name"])

                async def invoke(args, ctx, client=client, name=tool["name"]):
                    """代理执行特定 MCP 服务端工具调用的闭包处理函数 (MCP Invocation Handler)。

                    Args:
                        args: 工具入参字典。
                        ctx: 运行时上下文 RunContext。
                        client: 目标 MCP 客户端实例。
                        name: 目标工具原始名称。

                    Returns:
                        ToolResult: 包装 MCP 返回结果的标准工具结果对象。
                    """
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
        """处理 mcp_search 工具调用的内置路由 (Handler for mcp_search tool)。

        Args:
            args: 包含 query 检索关键词的参数字典。
            context: 运行时上下文。

        Returns:
            list[dict]: 匹配到的 MCP 工具声明列表。
        """
        return await self.search(args.get("query", ""))

    async def _call_handler(self, args, context):
        """处理 mcp_call 工具调用的内置转发路由 (Handler for mcp_call tool)。

        Args:
            args: 包含 name 与 arguments 的调用字典。
            context: 运行时上下文。

        Returns:
            ToolResult: 转发至 ToolRuntime 实际执行后的结果对象。

        Raises:
            ValueError: 当尝试调用未被 search 预加载发现的工具时抛出。
        """
        if args["name"] not in self.identities:
            raise ValueError("Search for the MCP tool before calling it")
        return await self.runtime.execute(
            ToolCall("mcp-" + str(id(args)), args["name"], args["arguments"]), context
        )

    async def close(self):
        """释放所有受控 MCP 客户端资源。"""
        if self._owns_clients:
            await asyncio.gather(*(client.close() for client in self.clients.values()))


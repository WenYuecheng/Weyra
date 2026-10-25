"""无状态大模型协议转换与流式事件归一化传输驱动 (Stateless Protocol Conversion & Streaming Transport)。

架构定位与设计哲学:
    本模块负责对接具体大模型厂商 API（如 OpenAI、Anthropic、DeepSeek 等），提供纯粹的无状态协议转换与流式归一化传输。
    根据单一职责原则（SRP），传输层仅持有取消凭据（ModelContext），不持有上层会话持久化、
    多轮轮次控制、预算计费或重试策略。支持独立于 Agent 应用层单独导入与测试。

开源框架借鉴与协议实现溯源:
    1. PiAgent (packages/ai/src/providers/):
       - 借鉴其厂商中立传输抽象（Provider Protocol）：统一暴露 `stream()` 异步生成器与 `close()` 清理接口；
       - 工业级错误多维分类法 (`_error`)：统一将底层各 SDK 或 HTTP 异常映射为五大标准类型：
         * `auth` (401/403 鉴权失败或缺少密钥)
         * `budget` (账户欠费、额度不足或预算溢出)
         * `overflow` (物理上下文超限，触发上层结构化压缩)
         * `transient` (429 限流、5xx 服务端故障或网络超时，允许退避重试)
         * `invalid` (参数不合法或协议违规，快速失败)
       - 严格的工具调用成对校验 (`_validate_pairs`)：静态验证消息历史中每个 ToolCall 必须紧跟匹配的 ToolResult，
         防止因消息序列错乱导致大模型 API 拒绝服务。
    2. DeepSeek-Harness (packages/llm/):
       - 提示词缓存与前缀对齐 (`_anthropic_cache`)：支持向 Anthropic 消息块注入 `cache_control: {"type": "ephemeral"}`，
         最大化复用服务端 KV Cache；
       - 细粒度 Token 消耗归一化计量：同时统计 prompt_tokens、completion_tokens 以及细分的 cache_read_tokens 和 cache_write_tokens；
       - 深度推理思考链解析 (`reasoning_content` / `thinking`)：原生支持 DeepSeek R1 及 Claude 思考过程分块提取。
"""

from __future__ import annotations
import copy
import inspect
import json
import os
from collections import deque
from typing import Any, AsyncIterator, Protocol
from .types import ModelInfo, ModelResponse, PreparedRequest, StreamEvent, ToolCall, Usage, canonical

class ModelContext(Protocol):
    """大模型调用上下文协议，提供运行期取消中断检测契约。"""

    def check_cancelled(self) -> None:
        """检查当前调用是否已被用户或系统取消。

        Raises:
            asyncio.CancelledError: 当操作已收到取消信号时抛出。
        """
        ...

class ModelError(Exception):
    """供应商调用异常统一封装类，携带标准化分类以便于重试策略与容灾决策。"""

    def __init__(
        self, kind: str, message: str = "Model request failed", *, status_code=None,
        usage: Usage | None = None, stop_reason: str | None = None,
    ):
        """初始化统一模型异常。

        Args:
            kind: 标准错误大类（"auth", "budget", "overflow", "transient", "invalid", "tool_arguments"）。
            message: 人类可读错误摘要。
            status_code: 原始 HTTP 响应状态码。
            usage: 失败前已消耗的 Token 计量。
            stop_reason: 模型停止原因。
        """
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code if isinstance(status_code, int) else None
        self.usage = usage
        self.stop_reason = stop_reason

def _error(exc: Exception) -> ModelError:
    """将厂商底层 SDK 异常与 HTTP 错误映射归一化为标准的五维 ModelError。

    【映射分类体系】
    - auth: 401/403 鉴权失败、API Key 失效或缺少凭证；
    - budget: 账户欠费、余额耗尽或额度熔断；
    - overflow: 上下文超限（Context Length Exceeded），触发八段式压缩；
    - transient: 429 限流、5xx 服务端故障、连接超时，允许按退避策略重试；
    - invalid: 入参非法或数据格式错误，快速失败。

    【核心不变性与安全约束】
    脱敏防御：严禁将厂商原始返回的错误体或任意未知代码直接持久化至日志，防止在错误信息中泄露 API 密钥。

    Args:
        exc: 捕获的原始底层异常实例。

    Returns:
        ModelError: 归一化后的标准异常。
    """
    if isinstance(exc, ModelError):
        return exc
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    body = getattr(exc, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else {}
    code = str(
        getattr(exc, "code", None) or (detail.get("code", "") if isinstance(detail, dict) else "")
    ).lower()
    # SDKs use different structured error shapes for context exhaustion.
    message = str(exc).lower()
    overflow = any(
        term in message
        for term in ("context length", "context_length", "prompt is too long", "maximum context")
    ) or (
        "context" in message
        and any(term in message for term in ("exceeds", "exceeded", "too long"))
        and any(term in message for term in ("input", "prompt", "token"))
    )
    if status in (401, 403) or "authentication" in name:
        kind = "auth"
    elif any(
        term in code
        for term in ("insufficient_quota", "credit_balance", "billing", "budget_exceeded")
    ):
        kind = "budget"
    elif code in {
        "context_length_exceeded", "context_window_exceeded", "max_context_length_exceeded",
        "prompt_too_long", "input_too_long", "too_many_tokens",
    } or "overflow" in name or overflow:
        kind = "overflow"
    elif (
        status in (408, 409, 429)
        or (isinstance(status, int) and status >= 500)
        or any(x in name for x in ("timeout", "connection", "ratelimit"))
    ):
        kind = "transient"
    else:
        kind = "invalid"
    result = ModelError(kind, status_code=status)
    # Never persist vendor text or arbitrary error codes: either may contain credentials.
    safe_codes = {
        "invalid_request_error", "invalid_parameter", "rate_limit_exceeded",
        "context_length_exceeded", "context_window_exceeded", "max_context_length_exceeded",
        "prompt_too_long", "input_too_long", "too_many_tokens", "model_not_found",
        "invalid_api_key", "insufficient_quota", "budget_exceeded", "server_error",
        "service_unavailable", "permission_denied", "authentication_error",
    }
    result.diagnostic = {
        "exception_type": type(exc).__name__[:64],
        "error_code": code if code in safe_codes else "other" if code else None,
    }
    return result

class Provider(Protocol):
    """供应商无状态传输驱动协议（Pi Agent 规范）。"""

    def stream(
        self, request: PreparedRequest, context: ModelContext
    ) -> AsyncIterator[StreamEvent]:
        """开启异步流式推理生成器。

        Args:
            request: 不可变模型请求信封。
            context: 包含取消检测的运行上下文。

        Returns:
            AsyncIterator[StreamEvent]: 流式事件异步迭代器。
        """
        ...

    async def close(self) -> None:
        """释放底层网络客户端连接池句柄。"""
        ...

class ProviderRegistry:
    """供应商驱动注册表，维护厂商 ID 与驱动实例的映射。"""

    def __init__(self):
        """初始化空的提供者驱动注册表。"""
        self.providers: dict[str, Provider] = {}

    def register(self, provider_id: str, provider: Provider) -> None:
        """注册新的供应商传输驱动。

        Args:
            provider_id: 供应商唯一标识符（如 "openai", "anthropic", "deepseek"）。
            provider: 实现 Provider 协议的驱动实例。

        Raises:
            ValueError: 标识符为空或已注册。
            TypeError: 驱动未实现 stream 或 close 方法。
        """
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("Provider identifier is required")
        if not callable(getattr(provider, "stream", None)) or not callable(
            getattr(provider, "close", None)
        ):
            raise TypeError("Provider must implement stream and close")
        if provider_id in self.providers:
            raise ValueError(f"Provider already registered: {provider_id}")
        self.providers[provider_id] = provider

    def get(self, provider_id: str) -> Provider:
        """获取指定标识符的供应商驱动实例。

        Args:
            provider_id: 目标供应商标识。

        Returns:
            Provider: 驱动实例。

        Raises:
            KeyError: 驱动尚未注册。
        """
        return self.providers[provider_id]

class ScriptedProvider:
    """确定性脚本化模拟供应商，用于单元测试与 0 费用 CI 验证（Pi Agent 规范）。"""

    def __init__(self, responses=()):
        """初始化确定性模拟提供者。

        Args:
            responses: 预先编排的响应序列（可包含 ModelResponse 或 Exception）。
        """
        self.responses = deque(responses)
        self.requests: list[PreparedRequest] = []

    async def stream(self, request, context):
        """按序回放预设的模型响应。

        Args:
            request: 传入的请求信封。
            context: 运行上下文。

        Yields:
            StreamEvent: 预设的流式分块与完整响应事件。
        """
        self.requests.append(request)
        result = self.responses.popleft()
        if isinstance(result, Exception):
            raise result
        if result.content:
            yield StreamEvent("text", result.content)
        yield StreamEvent("response", result)

    async def close(self):
        """空实现释放。"""
        pass

def _get(obj: Any, key: str, default=None):
    """安全读取字典键或对象属性值。

    Args:
        obj: 字典或属性对象。
        key: 属性或键名。
        default: 默认回退值。

    Returns:
        Any: 取得的值或默认值。
    """
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

def _plain(obj):
    """将 SDK 结构体对象转换为深拷贝的纯原生字典。

    Args:
        obj: Pydantic 模型、dataclass、字典或原生对象。

    Returns:
        dict: 深拷贝的原生字典。
    """
    if isinstance(obj, dict):
        return copy.deepcopy(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    return copy.deepcopy(vars(obj))

def _arguments(value):
    """反序列化并断言工具入参必须为合法的 JSON 对象字典。

    Args:
        value: JSON 字符串。

    Returns:
        dict: 解析后的字典。

    Raises:
        ModelError: 当 JSON 格式非法或解析结果非字典时抛出。
    """
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ModelError("invalid", "Tool arguments must be a JSON object")
    return parsed

def _validate_pairs(request):
    """静态校验消息历史中的工具调用与结果是否严格一一成对紧邻（Pi Agent 契约）。

    【核心不变性与安全约束】
    根据 OpenAI / Anthropic API 契约：
    1. 每个带有 `tool_calls` 的 assistant 消息后，必须紧随匹配的 `tool` 结果消息；
    2. 不允许悬空未决的 ToolCall，也不允许无对应 Call 的孤儿 ToolResult；
    3. 禁止同一轮次中出现重复的 `tool_call_id`。

    Args:
        request: 待检验的模型调用请求信封。

    Raises:
        ModelError: 出现不成对、重复或孤立的工具消息。
    """
    pending = set()
    seen = set()
    for message in request.messages:
        if message.role == "tool":
            if message.tool_call_id not in pending:
                raise ModelError("invalid", "Tool result has no matching call")
            pending.remove(message.tool_call_id)
        else:
            if pending:
                raise ModelError("invalid", "Tool calls must be followed by their results")
            for call in message.tool_calls:
                if call.id in seen:
                    raise ModelError("invalid", "Duplicate tool call identifier")
                seen.add(call.id)
                pending.add(call.id)
    if pending:
        raise ModelError("invalid", "Tool calls are missing results")

class OpenAIProvider:
    """OpenAI 协议无状态流式传输驱动，兼容 DeepSeek, Ollama 等 OpenAI 兼容接口。"""
    protocol = "openai"

    def __init__(
        self, provider_id="openai", api_key_env="OPENAI_API_KEY", base_url=None, client=None
    ):
        """初始化 OpenAI 协议驱动。

        Args:
            provider_id: 供应商标识符（如 "openai", "deepseek"）。
            api_key_env: 环境变量密钥名称。
            base_url: 自定义 API 网关端点基础 URL。
            client: 可选的预先初始化的 AsyncOpenAI 客户端实例。
        """
        self.provider_id, self.api_key_env, self.base_url = provider_id, api_key_env, base_url
        self.client = client
        self._api_key = None
        self._credential_snapshot = False

    def _client(self):
        """延迟初始化并返回底层的 AsyncOpenAI 客户端实例。

        Returns:
            AsyncOpenAI: 客户端单例。

        Raises:
            ModelError: 未配置鉴权密钥时抛出 "auth" 错误。
        """
        if self.client is None:
            key = (
                self._api_key
                if self._credential_snapshot
                else self._api_key or os.environ.get(self.api_key_env)
            )
            if not key:
                raise ModelError("auth", "Provider credential is not configured")
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(api_key=key, base_url=self.base_url, max_retries=0)
        return self.client

    async def stream(self, request, context):
        """执行流式推理，按块 yield 文本分块并组装最终标准化 ModelResponse。

        Args:
            request: 不可变请求信封。
            context: 包含取消检测的运行上下文。

        Yields:
            StreamEvent: 文本分块事件或最终完整的 ModelResponse 事件。

        Raises:
            ModelError: 出现网络故障、限流、超限或协议错误。
        """
        try:
            _validate_pairs(request)
            messages = [{"role": "system", "content": request.system}] if request.system else []
            for message in request.messages:
                item = {"role": message.role, "content": message.content}
                if message.role == "tool":
                    item["tool_call_id"] = message.tool_call_id
                if message.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": canonical(c.arguments)},
                        }
                        for c in message.tool_calls
                    ]
                if (
                    request.model.supports_reasoning
                    and message.provider_data.get("provider_id") == self.provider_id
                    and message.provider_data.get("model_id") == request.model.model_id
                ):
                    if "reasoning_content" in message.provider_data:
                        item["reasoning_content"] = message.provider_data["reasoning_content"]
                messages.append(item)
            kwargs = {
                "model": request.model.model_id,
                "messages": messages,
                "max_tokens": request.parameters.get(
                    "max_output_tokens", request.model.max_output_tokens
                ),
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if self.provider_id == "openai":
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
            for key in ("temperature", "top_p", "seed", "stop", "reasoning_effort"):
                if key in request.parameters:
                    kwargs[key] = request.parameters[key]
            if _reasoning_format(request, self) == "thinking" and "thinking" in request.parameters:
                kwargs["extra_body"] = {"thinking": request.parameters["thinking"]}
            if request.tools:
                kwargs["tools"] = [{"type": "function", "function": tool} for tool in request.tools]
            raw = await self._client().chat.completions.create(**kwargs)
            streamed = hasattr(raw, "__aiter__")
            if streamed:
                parts, reasoning, assembled = [], [], {}
                usage, finish = None, None
                try:
                    async for chunk in raw:
                        if context is not None:
                            context.check_cancelled()
                        if _get(chunk, "usage") is not None:
                            usage = chunk.usage
                        for choice in chunk.choices:
                            if _get(choice, "finish_reason"):
                                finish = choice.finish_reason
                            delta = choice.delta
                            if _get(delta, "content"):
                                parts.append(delta.content)
                                yield StreamEvent("text", delta.content)
                            if _get(delta, "reasoning_content"):
                                reasoning.append(delta.reasoning_content)
                            for call in _get(delta, "tool_calls", []) or []:
                                index = _get(call, "index")
                                if type(index) is not int or index < 0:
                                    raise ModelError("invalid", "Invalid streamed tool call index")
                                item = assembled.setdefault(
                                    index, {"id": "", "name": "", "arguments": ""}
                                )
                                item["id"] += _get(call, "id") or ""
                                function = _get(call, "function")
                                item["name"] += _get(function, "name") or ""
                                item["arguments"] += _get(function, "arguments") or ""
                finally:
                    close = getattr(raw, "close", None)
                    if close:
                        closed = close()
                        if inspect.isawaitable(closed):
                            await closed
                if finish is None:
                    raise ModelError("transient", "Model stream ended before completion")
                raw_calls = [item for _, item in sorted(assembled.items())]
                content, thought = "".join(parts), "".join(reasoning)
            else:
                choice = raw.choices[0]
                msg, usage, finish = choice.message, raw.usage, choice.finish_reason
                content, thought = _get(msg, "content") or "", _get(msg, "reasoning_content")
                raw_calls = [
                    {"id": c.id, "name": c.function.name, "arguments": c.function.arguments}
                    for c in (_get(msg, "tool_calls") or [])
                ]
            details = _get(usage, "prompt_tokens_details")
            cache = _get(usage, "prompt_cache_hit_tokens", _get(details, "cached_tokens"))
            cache_write = _get(
                usage,
                "cache_creation_input_tokens",
                _get(usage, "cache_write_tokens", _get(details, "cache_write_tokens")),
            )
            normalized_usage = Usage(
                _get(usage, "prompt_tokens"),
                _get(usage, "completion_tokens"),
                cache,
                cache_write,
            )
            stop_reason = {
                "stop": "stop",
                "length": "length",
                "tool_calls": "tool_calls",
                "content_filter": "error",
            }.get(finish, str(finish or "invalid"))
            # Decode the whole batch before exposing any executable tool calls.
            # Keep accounting separate: malformed JSON still consumed model tokens.
            try:
                calls = tuple(
                    ToolCall(item["id"], item["name"], _arguments(item["arguments"]))
                    for item in raw_calls
                )
            except json.JSONDecodeError:
                error = ModelError(
                    "tool_arguments", "Tool arguments contain invalid JSON",
                    usage=normalized_usage, stop_reason=stop_reason,
                )
                error.diagnostic = {"exception_type": "JSONDecodeError", "error_code": None}
                raise error from None
            except ModelError as error:
                error.usage = normalized_usage
                error.stop_reason = stop_reason
                raise
            result = ModelResponse(
                content, calls, stop_reason, normalized_usage,
                {"provider_id": self.provider_id, "model_id": request.model.model_id},
            )
            if thought is not None:
                result.provider_data["reasoning_content"] = thought
            if result.content and not streamed:
                yield StreamEvent("text", result.content)
            yield StreamEvent("response", result)
        except Exception as exc:
            raise _error(exc) from None

    async def close(self):
        """优雅关闭底层 AsyncOpenAI 客户端连接池。"""
        if self.client is not None and hasattr(self.client, "close"):
            result = self.client.close()
            if inspect.isawaitable(result):
                await result

class AnthropicProvider(OpenAIProvider):
    """Anthropic Claude 协议流式传输驱动，支持多断点 Prompt Caching 与思考链签名防护。"""
    protocol = "anthropic"

    def __init__(
        self, provider_id="anthropic", api_key_env="ANTHROPIC_API_KEY", base_url=None, client=None
    ):
        """初始化 Anthropic 协议驱动。

        Args:
            provider_id: 供应商标识符，固定为 "anthropic"。
            api_key_env: 环境变量密钥名称。
            base_url: 自定义 API 网关端点基础 URL。
            client: 可选的预先初始化的 AsyncAnthropic 客户端实例。
        """
        super().__init__(provider_id, api_key_env, base_url, client)

    def _client(self):
        """延迟初始化并返回底层的 AsyncAnthropic 客户端实例。

        Returns:
            AsyncAnthropic: 客户端单例。

        Raises:
            ModelError: 未配置鉴权密钥时抛出 "auth" 错误。
        """
        if self.client is None:
            key = (
                self._api_key
                if self._credential_snapshot
                else self._api_key or os.environ.get(self.api_key_env)
            )
            if not key:
                raise ModelError("auth", "Provider credential is not configured")
            from anthropic import AsyncAnthropic

            self.client = AsyncAnthropic(api_key=key, base_url=self.base_url, max_retries=0)
        return self.client

    async def stream(self, request, context):
        """执行 Anthropic 流式推理，自动处理多断点 KV 缓存与工具调用协议转换。

        Args:
            request: 不可变请求信封。
            context: 包含取消检测的运行上下文。

        Yields:
            StreamEvent: 文本分块与最终 ModelResponse。

        Raises:
            ModelError: 出现网络故障、超限或协议错误。
        """
        try:
            _validate_pairs(request)
            messages = []
            for message in request.messages:
                role = "user" if message.role == "tool" else message.role
                if message.role == "tool":
                    content = [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id,
                            "content": message.content,
                        }
                    ]
                    status = message.provider_data.get("tool_status")
                    if status is not None:
                        content[0]["is_error"] = status != "success"
                elif (
                    message.provider_data.get("provider_id") == self.provider_id
                    and message.provider_data.get("model_id") == request.model.model_id
                    and "content" in message.provider_data
                ):
                    content = copy.deepcopy(message.provider_data["content"])
                else:
                    content = [{"type": "text", "text": message.content}] if message.content else []
                    content.extend(
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                        for c in message.tool_calls
                    )
                for block in content:
                    block.pop("cache_control", None)
                if messages and messages[-1]["role"] == role:
                    messages[-1]["content"].extend(content)
                else:
                    messages.append({"role": role, "content": content})
            kwargs = {
                "model": request.model.model_id,
                "system": request.system,
                "messages": messages,
                "max_tokens": request.parameters.get(
                    "max_output_tokens", request.model.max_output_tokens
                ),
            }
            if request.tools:
                kwargs["tools"] = [
                    {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "input_schema": t["parameters"],
                    }
                    for t in request.tools
                ]
            for key in ("temperature", "top_p", "thinking"):
                if key in request.parameters:
                    kwargs[key] = request.parameters[key]
            if request.model.supports_cache:
                _anthropic_cache(
                    kwargs,
                    request.parameters.get("cache_ttl", "5m"),
                    request.parameters.get("cache_system_prefix"),
                )
            endpoint = self._client().messages
            streamed = hasattr(endpoint, "stream")
            if streamed:
                async with endpoint.stream(**kwargs) as stream:
                    async for event in stream:
                        if context is not None:
                            context.check_cancelled()
                        if _get(event, "type") == "content_block_delta":
                            delta = event.delta
                            if _get(delta, "type") == "text_delta":
                                yield StreamEvent("text", delta.text)
                    raw = await stream.get_final_message()
            else:
                raw = await endpoint.create(**kwargs)
            text = "".join(_get(b, "text", "") for b in raw.content if _get(b, "type") == "text")
            calls = tuple(
                ToolCall(_get(b, "id"), _get(b, "name"), _get(b, "input"))
                for b in raw.content
                if _get(b, "type") == "tool_use"
            )
            u = raw.usage
            read, write = _get(u, "cache_read_input_tokens"), _get(u, "cache_creation_input_tokens")
            base = _get(u, "input_tokens")
            usage = Usage(
                None if base is None else base + (read or 0) + (write or 0),
                _get(u, "output_tokens"),
                read,
                write,
            )
            response = ModelResponse(
                text,
                calls,
                {
                    "end_turn": "stop",
                    "stop_sequence": "stop",
                    "max_tokens": "length",
                    "tool_use": "tool_calls",
                    "refusal": "error",
                }.get(raw.stop_reason, str(raw.stop_reason or "invalid")),
                usage,
                {
                    "provider_id": self.provider_id,
                    "model_id": request.model.model_id,
                    "content": [_plain(b) for b in raw.content],
                },
            )
            if text and not streamed:
                yield StreamEvent("text", text)
            yield StreamEvent("response", response)
        except Exception as exc:
            raise _error(exc) from None

def default_model(
    provider: str, model: str, context_window=None, max_output_tokens=None
) -> ModelInfo:
    """为缺少显式元数据声明的模型提供默认规格，要求必须显式指定物理容量。

    Args:
        provider: 厂商标识符。
        model: 模型标识符。
        context_window: 物理上下文窗口大小（Token 数）。
        max_output_tokens: 单次调用最大生成输出 Token 数。

    Returns:
        ModelInfo: 构造的元数据规格。

    Raises:
        ValueError: 当目录中未预置且未显式指定 context_window 或 max_output_tokens 时抛出。
    """
    from .catalog import MODEL_CATALOG

    metadata = MODEL_CATALOG.get((provider, model), {})
    context_window = (
        context_window if context_window is not None else metadata.get("context_window")
    )
    max_output_tokens = (
        max_output_tokens if max_output_tokens is not None else metadata.get("max_output_tokens")
    )
    if context_window is None or max_output_tokens is None:
        raise ValueError(
            "Explicit context_window and max_output_tokens are required for this model"
        )
    return ModelInfo(
        provider,
        model,
        context_window,
        max_output_tokens,
        supports_reasoning=metadata.get("supports_reasoning", False),
        supports_cache=metadata.get("supports_cache", False),
    )

def _reasoning_format(request: PreparedRequest, provider: Provider) -> str:
    """推导当前模型请求所采用的深度思考链（Reasoning / Thinking）协议格式。

    【格式归一化】
    - anthropic: Anthropic 扩展思考协议块；
    - thinking: DeepSeek 原生 thinking 协议块（支持 extra_body 传参）；
    - openai: 标准 OpenAI reasoning_effort 协议。

    Args:
        request: 请求信封。
        provider: 目标提供者驱动。

    Returns:
        str: 推理格式字符串标识。
    """
    protocol = getattr(
        provider, "protocol", "anthropic" if request.model.provider_id == "anthropic" else "openai"
    )
    derived = (
        "anthropic"
        if protocol == "anthropic"
        else "thinking"
        if request.model.provider_id == "deepseek"
        else "openai"
    )
    return request.model.reasoning_format or derived

def _validate_parameters(request: PreparedRequest, provider: Provider) -> None:
    """严格校验大模型调用请求的超参数白名单与数值范围（Fail-Closed 校验）。

    【核心不变性与安全约束】
    1. 协议匹配：断言推理格式与提供者协议强一致；
    2. 白名单约束：禁止传入未声明参数，防止静默忽略导致行为偏差；
    3. 数值边界：temperature [0, 2], top_p [0, 1]；
    4. 思考预算：Anthropic thinking budget 必须大于等于 1024 且小于 max_output_tokens。

    Args:
        request: 待校验的模型请求信封。
        provider: 目标执行提供者驱动。

    Raises:
        ModelError: 当存在非法参数、数值越界或不匹配协议时抛出 "invalid" 异常。
    """
    parameters = request.parameters
    protocol = getattr(
        provider, "protocol", "anthropic" if request.model.provider_id == "anthropic" else "openai"
    )
    reasoning_format = _reasoning_format(request, provider)
    if (protocol == "anthropic") != (reasoning_format == "anthropic"):
        raise ModelError("invalid", "Reasoning format does not match the provider protocol")
    allowed = {"estimated_input_tokens", "max_output_tokens", "temperature", "top_p"}
    if protocol == "anthropic":
        allowed |= {"thinking", "cache_ttl", "cache_system_prefix"}
    else:
        allowed |= {"seed", "stop"}
        if request.model.supports_reasoning:
            allowed.add("thinking" if reasoning_format == "thinking" else "reasoning_effort")
    if set(parameters) - allowed:
        raise ModelError("invalid", "Unsupported model request parameter")
    for key, upper in (("temperature", 2), ("top_p", 1)):
        if key in parameters and (
            type(parameters[key]) not in (int, float) or not 0 <= parameters[key] <= upper
        ):
            raise ModelError("invalid", "Invalid sampling parameter")
    if "seed" in parameters and type(parameters["seed"]) is not int:
        raise ModelError("invalid", "Invalid seed parameter")
    if "stop" in parameters and not (
        isinstance(parameters["stop"], str)
        or isinstance(parameters["stop"], list)
        and all(isinstance(x, str) for x in parameters["stop"])
    ):
        raise ModelError("invalid", "Invalid stop parameter")
    if "reasoning_effort" in parameters and parameters["reasoning_effort"] not in {
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "none",
    }:
        raise ModelError("invalid", "Invalid reasoning effort")
    if "thinking" in parameters:
        thinking = parameters["thinking"]
        if not request.model.supports_reasoning or not isinstance(thinking, dict):
            raise ModelError("invalid", "Thinking is unsupported")
        mode = thinking.get("type")
        allowed_thinking = {"type", "budget_tokens"} if protocol == "anthropic" else {"type"}
        if (
            set(thinking) - allowed_thinking
            or mode not in {"enabled", "disabled", "adaptive"}
            or protocol != "anthropic"
            and mode == "adaptive"
        ):
            raise ModelError("invalid", "Invalid thinking parameters")
        if protocol == "anthropic" and mode == "enabled":
            budget = thinking.get("budget_tokens")
            output = parameters.get("max_output_tokens", request.model.max_output_tokens)
            if type(budget) is not int or not 1024 <= budget < output:
                raise ModelError("invalid", "Thinking budget must fit the request output limit")
        elif "budget_tokens" in thinking:
            raise ModelError("invalid", "Thinking budget requires enabled thinking")
    if "cache_system_prefix" in parameters and (
        not isinstance(parameters["cache_system_prefix"], str)
        or not parameters["cache_system_prefix"]
        or not request.system.startswith(parameters["cache_system_prefix"])
    ):
        raise ModelError("invalid", "Cache prefix must match the system prompt")
    if "cache_ttl" in parameters and (
        not request.model.supports_cache or parameters["cache_ttl"] not in {"5m", "1h"}
    ):
        raise ModelError("invalid", "Invalid cache policy")

def _anthropic_cache(kwargs, ttl="5m", static_system_prefix=None):
    """遵循 Pi Agent 与 Anthropic 规范的 4 断点 KV Cache 前缀缓存对齐策略。

    【算法机制与设计思想】
    Anthropic 提示词缓存（Prompt Caching）支持最多 4 个 `cache_control: {"type": "ephemeral"}` 断点。
    本函数参考 Pi Agent (`@earendil-works/pi-ai`) 实现严格的断点预算分配：
    1. 系统提示词前缀与尾部：若存在静态系统提示词前缀 `static_system_prefix`，分配第 1 个断点在静态前缀，第 2 个断点在动态系统提示词末尾；
    2. 最近消息轮次：根据系统提示词消耗的断点数，将剩余断点（至多 2~3 个）精准分配给最后几轮历史消息末尾；
    3. 缓存标记清理：在注入前彻底剔除所有旧消息块和签名块上遗留的陈旧 `cache_control`，防御跨轮次缓存污染；
    4. 思考签名防护：严格跳过 `thinking` 和 `redacted_thinking` 块，确保模型思考签名（thinkingSignature）字节级不可篡改。

    Args:
        kwargs: 待发送给 Anthropic SDK 的请求参数字典（被就地原地修改）。
        ttl: 缓存存活生命周期，默认 "5m"。
        static_system_prefix: 可选的静态系统提示词前缀文本。
    """
    marker = {"type": "ephemeral", "ttl": ttl}
    system = kwargs.get("system")
    if isinstance(system, str):
        if (
            isinstance(static_system_prefix, str)
            and static_system_prefix
            and system.startswith(static_system_prefix)
            and system[len(static_system_prefix) :].strip()
        ):
            system = [
                {"type": "text", "text": static_system_prefix},
                {"type": "text", "text": system[len(static_system_prefix) :]},
            ]
        else:
            system = [{"type": "text", "text": system}] if system else []
        kwargs["system"] = system
    # Never replay stale cache controls from signed provider blocks.
    for message in kwargs["messages"]:
        for block in message["content"]:
            block.pop("cache_control", None)
    if system:
        for block in system:
            block.pop("cache_control", None)
        system[0]["cache_control"] = dict(marker)
        system[-1]["cache_control"] = dict(marker)
    for message in kwargs["messages"][-(4 - min(len(system), 2)) :]:
        if message["content"]:
            # Thinking and redacted-thinking signatures must remain byte for byte intact.
            eligible = [
                b
                for b in message["content"]
                if b.get("type") not in {"thinking", "redacted_thinking"}
            ]
            if eligible:
                eligible[-1]["cache_control"] = dict(marker)
    marker = {"type": "ephemeral", "ttl": ttl}
    system = kwargs.get("system")
    if isinstance(system, str):
        if (
            isinstance(static_system_prefix, str)
            and static_system_prefix
            and system.startswith(static_system_prefix)
            and system[len(static_system_prefix) :].strip()
        ):
            system = [
                {"type": "text", "text": static_system_prefix},
                {"type": "text", "text": system[len(static_system_prefix) :]},
            ]
        else:
            system = [{"type": "text", "text": system}] if system else []
        kwargs["system"] = system
    # Never replay stale cache controls from signed provider blocks.
    for message in kwargs["messages"]:
        for block in message["content"]:
            block.pop("cache_control", None)
    if system:
        for block in system:
            block.pop("cache_control", None)
        system[0]["cache_control"] = dict(marker)
        system[-1]["cache_control"] = dict(marker)
    for message in kwargs["messages"][-(4 - min(len(system), 2)) :]:
        if message["content"]:
            # Thinking and redacted-thinking signatures must remain byte for byte intact.
            eligible = [
                b
                for b in message["content"]
                if b.get("type") not in {"thinking", "redacted_thinking"}
            ]
            if eligible:
                eligible[-1]["cache_control"] = dict(marker)

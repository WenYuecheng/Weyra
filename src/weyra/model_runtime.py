"""Single-call model execution and provider-neutral response normalization."""

from __future__ import annotations

import copy
import inspect
import json
import os
from collections import deque
from typing import Any, AsyncIterator, Protocol, TYPE_CHECKING

from .contracts import (
    ModelInfo,
    ModelResponse,
    PreparedRequest,
    StreamEvent,
    ToolCall,
    Usage,
    canonical,
)

if TYPE_CHECKING:
    from .runtime_state import RunContext


class ModelError(Exception):
    """A sanitized provider failure suitable for retry policy decisions."""

    def __init__(self, kind: str, message: str = "Model request failed"):
        super().__init__(message)
        self.kind = kind


def _error(exc: Exception) -> ModelError:
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
    overflow = any(
        term in str(exc).lower()
        for term in ("context length", "context_length", "prompt is too long", "maximum context")
    )
    if status in (401, 403) or "authentication" in name:
        kind = "auth"
    elif "context" in code or "overflow" in name or overflow:
        kind = "overflow"
    elif (
        status in (408, 409, 429)
        or (isinstance(status, int) and status >= 500)
        or any(x in name for x in ("timeout", "connection", "ratelimit"))
    ):
        kind = "transient"
    else:
        kind = "invalid"
    return ModelError(kind)


class Provider(Protocol):
    def stream(
        self, request: PreparedRequest, context: RunContext
    ) -> AsyncIterator[StreamEvent]: ...
    async def close(self) -> None: ...


class ProviderRegistry:
    def __init__(self):
        self.providers: dict[str, Provider] = {}

    def register(self, provider_id: str, provider: Provider) -> None:
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
        return self.providers[provider_id]


class ModelRuntime:
    def __init__(self, registry: ProviderRegistry, routes: dict[str, ModelInfo]):
        if not isinstance(registry, ProviderRegistry):
            raise TypeError("A ProviderRegistry instance is required")
        if not isinstance(routes, dict) or "main" not in routes:
            raise ValueError("A main model route is required")
        if any(
            not isinstance(purpose, str) or not isinstance(model, ModelInfo)
            for purpose, model in routes.items()
        ):
            raise TypeError("Routes must map purposes to ModelInfo instances")
        for model in routes.values():
            registry.get(model.provider_id)
        self.registry = registry
        self.routes = dict(routes)

    def resolve(self, purpose: str = "main") -> ModelInfo:
        return self.routes.get(purpose, self.routes["main"])

    async def stream(
        self, request: PreparedRequest, context: RunContext
    ) -> AsyncIterator[StreamEvent]:
        context.check_cancelled()
        if set(request.parameters) - {
            "estimated_input_tokens",
            "max_output_tokens",
            "temperature",
            "top_p",
            "seed",
            "stop",
        }:
            raise ModelError("invalid", "Unsupported model request parameter")
        estimate = request.parameters.get(
            "estimated_input_tokens",
            max(1, (len(canonical(request.to_dict()).encode("utf-8")) + 1) // 2),
        )
        output = request.parameters.get("max_output_tokens", request.model.max_output_tokens)
        if (
            not isinstance(output, int)
            or isinstance(output, bool)
            or not 0 < output <= request.model.max_output_tokens
        ):
            raise ModelError("invalid", "Output limit exceeds configured model capacity")
        if not isinstance(estimate, int) or isinstance(estimate, bool) or estimate < 0:
            raise ModelError("invalid", "Invalid input token estimate")
        provider = self.registry.get(request.model.provider_id)
        reservation = await context.budget.reserve(estimate + output, purpose=request.purpose)
        actual, usage = None, None
        logged = False
        try:
            context.session.append(
                "model_request",
                {**request.to_dict(), "turn_id": getattr(context, "id", None)},
                checkpoint=True,
            )
            logged = True
            stream = provider.stream(request, context)
            try:
                while True:
                    context.check_cancelled()
                    try:
                        event = await anext(stream)
                    except StopAsyncIteration:
                        break
                    except Exception as exc:
                        raise _error(exc) from None
                    context.check_cancelled()
                    if event.kind == "response":
                        actual, usage = event.data.usage.total_tokens, event.data.usage
                    yield event
            finally:
                await stream.aclose()
        finally:
            await context.budget.settle(reservation, actual)
            if logged:
                context.session.append(
                    "model_usage",
                    {
                        "request_id": request.id,
                        "turn_id": getattr(context, "id", None),
                        "purpose": request.purpose,
                        "reservation": reservation,
                        "total_tokens": actual,
                        "input_tokens": usage.input_tokens if usage else None,
                        "output_tokens": usage.output_tokens if usage else None,
                        "cache_read_tokens": usage.cache_read_tokens if usage else None,
                        "cache_write_tokens": usage.cache_write_tokens if usage else None,
                        "provider_id": request.model.provider_id,
                        "model_id": request.model.model_id,
                        "accounted_tokens": actual if actual is not None else estimate + output,
                    },
                    checkpoint=True,
                )

    async def complete(self, request: PreparedRequest, context: RunContext) -> ModelResponse:
        response = None
        async for event in self.stream(request, context):
            if event.kind == "response":
                response = event.data
        if response is None:
            raise ModelError("invalid", "Provider returned no final response")
        return response

    async def close(self) -> None:
        for provider in self.registry.providers.values():
            await provider.close()


class ScriptedProvider:
    def __init__(self, responses=()):
        self.responses = deque(responses)
        self.requests: list[PreparedRequest] = []

    async def stream(self, request, context):
        self.requests.append(request)
        result = self.responses.popleft()
        if isinstance(result, Exception):
            raise result
        if result.content:
            yield StreamEvent("text", result.content)
        yield StreamEvent("response", result)

    async def close(self):
        pass


def _get(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _plain(obj):
    if isinstance(obj, dict):
        return copy.deepcopy(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    return copy.deepcopy(vars(obj))


def _arguments(value):
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ModelError("invalid", "Tool arguments must be a JSON object")
    return parsed


def _validate_pairs(request):
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
    def __init__(
        self, provider_id="openai", api_key_env="OPENAI_API_KEY", base_url=None, client=None
    ):
        self.provider_id, self.api_key_env, self.base_url = provider_id, api_key_env, base_url
        self.client = client

    def _client(self):
        if self.client is None:
            from openai import AsyncOpenAI

            key = os.environ.get(self.api_key_env)
            if not key:
                raise ModelError("auth", "Provider credential is not configured")
            self.client = AsyncOpenAI(api_key=key, base_url=self.base_url, max_retries=0)
        return self.client

    async def stream(self, request, context):
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
                    and message.provider_data.get("model_id", request.model.model_id)
                    == request.model.model_id
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
            for key in ("temperature", "top_p", "seed", "stop"):
                if key in request.parameters:
                    kwargs[key] = request.parameters[key]
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
                                item = assembled.setdefault(
                                    call.index, {"id": "", "name": "", "arguments": ""}
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
                calls = tuple(
                    ToolCall(item["id"], item["name"], _arguments(item["arguments"]))
                    for _, item in sorted(assembled.items())
                )
                content, thought = "".join(parts), "".join(reasoning)
            else:
                choice = raw.choices[0]
                msg, usage, finish = choice.message, raw.usage, choice.finish_reason
                content, thought = _get(msg, "content") or "", _get(msg, "reasoning_content")
                calls = tuple(
                    ToolCall(c.id, c.function.name, _arguments(c.function.arguments))
                    for c in (_get(msg, "tool_calls") or [])
                )
            details = _get(usage, "prompt_tokens_details")
            cache = _get(usage, "prompt_cache_hit_tokens", _get(details, "cached_tokens"))
            result = ModelResponse(
                content,
                calls,
                {"length": "length", "tool_calls": "tool_calls", "content_filter": "error"}.get(
                    finish, "stop"
                ),
                Usage(_get(usage, "prompt_tokens"), _get(usage, "completion_tokens"), cache),
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
        if self.client is not None and hasattr(self.client, "close"):
            result = self.client.close()
            if inspect.isawaitable(result):
                await result


class AnthropicProvider(OpenAIProvider):
    def __init__(
        self, provider_id="anthropic", api_key_env="ANTHROPIC_API_KEY", base_url=None, client=None
    ):
        super().__init__(provider_id, api_key_env, base_url, client)

    def _client(self):
        if self.client is None:
            from anthropic import AsyncAnthropic

            key = os.environ.get(self.api_key_env)
            if not key:
                raise ModelError("auth", "Provider credential is not configured")
            self.client = AsyncAnthropic(api_key=key, base_url=self.base_url, max_retries=0)
        return self.client

    async def stream(self, request, context):
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
                    and message.provider_data.get("model_id", request.model.model_id)
                    == request.model.model_id
                    and "content" in message.provider_data
                ):
                    content = copy.deepcopy(message.provider_data["content"])
                else:
                    content = [{"type": "text", "text": message.content}] if message.content else []
                    content.extend(
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                        for c in message.tool_calls
                    )
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
                {"max_tokens": "length", "tool_use": "tool_calls", "refusal": "error"}.get(
                    raw.stop_reason, "stop"
                ),
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
    """Require explicit capacities for models without pinned local metadata."""
    if context_window is None or max_output_tokens is None:
        raise ValueError(
            "Explicit context_window and max_output_tokens are required for this model"
        )
    return ModelInfo(
        provider,
        model,
        context_window,
        max_output_tokens,
        supports_reasoning=provider in ("deepseek", "anthropic"),
        supports_cache=provider in ("deepseek", "anthropic", "openai"),
    )

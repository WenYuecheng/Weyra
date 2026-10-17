"""Single-call model execution and provider-neutral response normalization."""

from __future__ import annotations

import asyncio
import random
import copy
import inspect
import json
import os
import time
from dataclasses import asdict
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
    new_id,
)

if TYPE_CHECKING:
    from .runtime_state import RunContext


class ModelError(Exception):
    """A sanitized provider failure suitable for retry policy decisions."""

    def __init__(self, kind: str, message: str = "Model request failed", *, status_code=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code if isinstance(status_code, int) else None


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
    elif any(
        term in code
        for term in ("insufficient_quota", "credit_balance", "billing", "budget_exceeded")
    ):
        kind = "budget"
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
    return ModelError(kind, status_code=status)


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
        self._initial_routes = dict(routes)
        self.inherited_purposes = {
            p for p in ("compression", "memory", "evolution") if p not in routes
        }
        self.fallbacks = {}
        self.route_parameters = {}
        self.fallback_parameters = {}
        self.pricing = {}

    def resolve(self, purpose: str = "main") -> ModelInfo:
        return self.routes.get(purpose, self.routes["main"])

    def resolve_model(self, provider_id, model_id):
        self.registry.get(provider_id)
        candidates = list(self.routes.values()) + [
            model for values in self.fallbacks.values() for model in values
        ]
        for model in candidates:
            if (model.provider_id, model.model_id) == (provider_id, model_id):
                return model
        return default_model(provider_id, model_id)

    def parameters_for(self, purpose="main", model=None):
        source = "main" if purpose in self.inherited_purposes else purpose
        model = model or self.resolve(purpose)
        if model != self._initial_routes.get(source):
            return copy.deepcopy(
                self.fallback_parameters.get((source, model.provider_id, model.model_id), {})
            )
        return copy.deepcopy(self.route_parameters.get(source, {}))

    def next_fallback(self, purpose="main", current=None):
        current = current or self.resolve(purpose)
        chain = self.fallbacks.get(purpose, ())
        for index, model in enumerate(chain):
            if model == current:
                return chain[index + 1] if index + 1 < len(chain) else None
        return chain[0] if chain else None

    def activate_route(self, purpose, model, context=None):
        self.registry.get(model.provider_id)
        self.routes[purpose] = model
        if purpose == "main":
            for inherited in self.inherited_purposes:
                self.routes[inherited] = model
        if context is not None:
            context.session.append(
                "model_route_change", {"purpose": purpose, "model": asdict(model)}, checkpoint=True
            )
            if purpose == "main":
                context.session.append("model_change", asdict(model), checkpoint=True)

    async def stream(self, request, context, *, attempt_budget=None):
        """Coordinate transient retries; callers reprepare overflow and failover requests."""
        configured = self.parameters_for(request.purpose, request.model)
        if configured:
            request = PreparedRequest(
                request.model,
                request.system,
                request.messages,
                request.tools,
                request.purpose,
                request.context_version,
                {**configured, **request.parameters},
                request.id,
            )
        attempts = attempt_budget if attempt_budget is not None else {"attempts": 0, "limit": 3}
        for retry in range(3):
            context.check_cancelled()
            if attempts["attempts"] >= attempts.get("limit", 4):
                raise ModelError("attempt_limit", "Model physical attempt limit reached")
            attempts["attempts"] += 1
            try:
                async for event in self._stream_once(request, context):
                    yield event
                return
            except ModelError as error:
                if (
                    error.kind != "transient"
                    or retry == 2
                    or attempts["attempts"] >= attempts.get("limit", 4)
                ):
                    raise
                emit = getattr(context, "emit", None)
                if emit is not None:
                    await emit("attempt_reset", {"kind": "transient", "retry": retry + 1})
                await asyncio.sleep(min(10.0, 0.5 * 2**retry) * random.uniform(0.9, 1.1))

    async def _stream_once(
        self, request: PreparedRequest, context: RunContext
    ) -> AsyncIterator[StreamEvent]:
        context.check_cancelled()
        provider = self.registry.get(request.model.provider_id)
        _validate_parameters(request, provider)
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
        from .pricing import Price

        price_data = self.pricing.get(f"{request.model.provider_id}/{request.model.model_id}")
        price = Price.from_dict(price_data) if price_data else None
        # Pressure estimates are not monetary upper bounds. Reserve the full
        # declared input capacity plus this request's output, then refund usage.
        if (
            price
            and getattr(context.budget, "cost_limit_cny", None) is not None
            and request.model.supports_cache
            and (
                (price.cache_write_per_million is None and price.cache_write_billing != "included")
                or price.cache_read_per_million is None
            )
        ):
            from .runtime_state import BudgetExceeded

            raise BudgetExceeded("Verified cache read and write tariffs are required")
        estimated_cost = price.estimate(request.model.context_window, output) if price else None
        reserve_kwargs = {"purpose": request.purpose}
        if "estimated_cost_cny" in inspect.signature(context.budget.reserve).parameters:
            reserve_kwargs.update(
                estimated_cost_cny=estimated_cost,
                scope=getattr(context, "budget_scope", None) or getattr(context, "id", None),
            )
        reservation = await context.budget.reserve(estimate + output, **reserve_kwargs)
        actual, usage = None, None
        response = None
        logged = False
        attempt_logged = False
        started_at, first_text = time.monotonic(), None
        state, failure_kind, failure_status = "interrupted", None, None
        attempt = {
            "attempt_id": new_id(),
            "request_id": request.id,
            "turn_id": getattr(context, "id", None),
            "purpose": request.purpose,
            "provider_id": request.model.provider_id,
            "model_id": request.model.model_id,
        }
        try:
            context.session.append("attempt_start", dict(attempt), checkpoint=True)
            attempt_logged = True
            context.session.append(
                "model_request",
                {**request.to_dict(), **attempt},
                checkpoint=True,
            )
            logged = True
            checkpoint = getattr(context, "services", {}).get("on_request_checkpoint")
            if checkpoint:
                checkpoint(request)
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
                    if event.kind == "text" and event.data and first_text is None:
                        first_text = time.monotonic() - started_at
                    if event.kind == "response":
                        response = event.data
                        actual, usage = response.usage.total_tokens, response.usage
                        state = "completed"
                    yield event
                if response is None:
                    raise ModelError("invalid", "Provider returned no final response")
            finally:
                await stream.aclose()
        except BaseException as error:
            state = (
                "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "interrupted"
                if isinstance(error, GeneratorExit)
                else "failed"
            )
            failure_kind = error.kind if isinstance(error, ModelError) else None
            failure_status = getattr(error, "status_code", None)
            raise
        finally:
            actual_cost = price.actual(usage) if price and usage else None
            cost_known = actual_cost is not None
            if actual_cost is None and price and usage:
                actual_cost = price.upper_bound(usage)
            settle_kwargs = {}
            if "actual_cost_cny" in inspect.signature(context.budget.settle).parameters:
                settle_kwargs["actual_cost_cny"] = actual_cost
            if "cost_known" in inspect.signature(context.budget.settle).parameters:
                settle_kwargs["cost_known"] = cost_known
            await context.budget.settle(reservation, actual, **settle_kwargs)
            if attempt_logged:
                context.session.append(
                    "attempt_end",
                    {
                        **attempt,
                        "state": state,
                        "kind": failure_kind,
                        "status_code": failure_status,
                        "usage": asdict(usage) if usage else None,
                        "stop_reason": response.stop_reason if response else None,
                        "baseline_fingerprint": request.baseline_fingerprint,
                        "fingerprint": request.fingerprint,
                    },
                    checkpoint=True,
                )
            if logged:
                context.session.append(
                    "model_usage",
                    {
                        **attempt,
                        "reservation": reservation,
                        "state": state,
                        "first_text_seconds": first_text,
                        "request_duration_seconds": time.monotonic() - started_at,
                        "total_tokens": actual,
                        "input_tokens": usage.input_tokens if usage else None,
                        "output_tokens": usage.output_tokens if usage else None,
                        "cache_read_tokens": usage.cache_read_tokens if usage else None,
                        "cache_write_tokens": usage.cache_write_tokens if usage else None,
                        "provider_id": request.model.provider_id,
                        "model_id": request.model.model_id,
                        "accounted_tokens": actual if actual is not None else estimate + output,
                        "estimated_cost_cny": str(estimated_cost)
                        if estimated_cost is not None
                        else None,
                        "actual_cost_cny": str(actual_cost) if actual_cost is not None else None,
                        "cost_known": cost_known,
                        "cost_method": "reported_usage"
                        if cost_known
                        else "usage_upper_bound"
                        if actual_cost is not None
                        else "retained_reservation",
                    },
                    checkpoint=True,
                )

    async def complete(
        self, request: PreparedRequest, context: RunContext, *, attempt_budget=None
    ) -> ModelResponse:
        response = None
        async for event in self.stream(request, context, attempt_budget=attempt_budget):
            if event.kind == "response":
                response = event.data
        if response is None:
            raise ModelError("invalid", "Provider returned no final response")
        return response

    response = complete

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
    protocol = "openai"

    def __init__(
        self, provider_id="openai", api_key_env="OPENAI_API_KEY", base_url=None, client=None
    ):
        self.provider_id, self.api_key_env, self.base_url = provider_id, api_key_env, base_url
        self.client = client
        self._api_key = None
        self._credential_snapshot = False

    def _client(self):
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
            cache_write = _get(
                usage,
                "cache_creation_input_tokens",
                _get(usage, "cache_write_tokens", _get(details, "cache_write_tokens")),
            )
            result = ModelResponse(
                content,
                calls,
                {
                    "stop": "stop",
                    "length": "length",
                    "tool_calls": "tool_calls",
                    "content_filter": "error",
                }.get(finish, str(finish or "invalid")),
                Usage(
                    _get(usage, "prompt_tokens"),
                    _get(usage, "completion_tokens"),
                    cache,
                    cache_write,
                ),
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
    protocol = "anthropic"

    def __init__(
        self, provider_id="anthropic", api_key_env="ANTHROPIC_API_KEY", base_url=None, client=None
    ):
        super().__init__(provider_id, api_key_env, base_url, client)

    def _client(self):
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
    """Require explicit capacities for models without pinned local metadata."""
    from .model_catalog import MODEL_CATALOG

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


def _reasoning_format(request, provider):
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


def _validate_parameters(request, provider):
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
    """Hermes breakpoints: stable system prefix/end and last two message ends."""
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

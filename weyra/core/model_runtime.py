"""多用途模型路由、故障降级接管与审计日志协调器 (Model Execution Coordinator & Router)。

架构定位与设计哲学:
    本模块是 Weyra 核心内核中管理大模型调用的协调中枢。
    严格区别于底层的无状态协议驱动（`weyra.llm.providers`），本协调器负责管理：
    1. 多用途模型路由表（main 主模型、compression 压缩专用模型、memory 记忆提取模型、evolution 技能演化模型）；
    2. 主备容灾故障转移链（Fallback Routes）；
    3. 金融级预算事前预扣（reserve）与事后核销（settle）生命周期绑定；
    4. 会话持久化日志检查点记录（model_request / model_response）。

开源框架借鉴与设计原则:
    1. DeepSeek-Harness (packages/llm/):
       - 专职用途解耦路由（Purpose-based Routing）：不同子系统按需路由至高性价比模型
         （如主交互使用 DeepSeek-V3/Pro，长文本压缩使用 DeepSeek-Flash，裁判评估使用独立判定模型），
         未显式配置时安全继承主模型（`inherited_purposes`）；
       - 预算事前闭环（Budget Reservation）：在发起流式网络连接前，严格调用 `ctx.budget.reserve()`
         预扣最大可能 Token 与货币额度，杜绝透支；
       - 会话审计落盘（Session Journaling）：完整记录请求信封与返回响应的持久化事件。
    2. PiAgent:
       - 保持传输驱动与运行时的清晰分层，`weyra.model_runtime` 向下兼容映射至本模块，保证历史测试兼容性。
"""

from __future__ import annotations
import asyncio
import random
import copy
import inspect
import time
from dataclasses import asdict
from typing import Any, AsyncIterator, TYPE_CHECKING
from ..llm.types import ModelInfo, ModelResponse, PreparedRequest, StreamEvent, ToolCall, Usage, canonical, new_id
from ..llm.providers import (
    ModelError, Provider, ProviderRegistry, ScriptedProvider, OpenAIProvider,
    AnthropicProvider, default_model, _error, _validate_parameters, _validate_pairs,
    _anthropic_cache, _reasoning_format, _get, _plain, _arguments,
)
from .retry import RetryPolicy
if TYPE_CHECKING:
    from ..runtime_state import RunContext


class ModelRuntime:
    """多供应商模型网关运行时。

    管理提供者注册表、多用途路由调度、容灾回退降级与重试执行。
    """

    def __init__(self, registry: ProviderRegistry, routes: dict[str, ModelInfo]):
        """初始化模型运行时。

        Args:
            registry: 供应商注册表实例 ProviderRegistry。
            routes: 包含必须的 "main" 路由以及可选的专用路由字典。
        """
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
        """根据用途获取对应模型规格，未配置时安全降级至 main 主模型。"""
        return self.routes.get(purpose, self.routes["main"])


    def resolve_model(self, provider_id: str, model_id: str) -> ModelInfo:
        """解析并返回已注册的指定供应商和模型规格对象。

        Args:
            provider_id: 供应商标识符。
            model_id: 模型标识符。

        Returns:
            ModelInfo: 模型规格实体。
        """
        self.registry.get(provider_id)
        candidates = list(self.routes.values()) + [
            model for values in self.fallbacks.values() for model in values
        ]
        for model in candidates:
            if (model.provider_id, model.model_id) == (provider_id, model_id):
                return model
        return default_model(provider_id, model_id)

    def parameters_for(self, purpose: str = "main", model: ModelInfo | None = None) -> dict[str, Any]:
        """提取指定用途或特定降级模型的超参数配置字典。

        Args:
            purpose: 路由用途。
            model: 目标模型（为 None 时推导当前解析模型）。

        Returns:
            dict[str, Any]: 深度拷贝的超参数字典。
        """
        source = "main" if purpose in self.inherited_purposes else purpose
        model = model or self.resolve(purpose)
        if model != self._initial_routes.get(source):
            return copy.deepcopy(
                self.fallback_parameters.get((source, model.provider_id, model.model_id), {})
            )
        return copy.deepcopy(self.route_parameters.get(source, {}))

    def next_fallback(self, purpose: str = "main", current: ModelInfo | None = None) -> ModelInfo | None:
        """在容灾故障转移链中获取下一个备用候选模型。

        Args:
            purpose: 路由用途。
            current: 当前失败的模型规格。

        Returns:
            ModelInfo | None: 下一个候选模型，若降级链已穷尽则返回 None。
        """
        current = current or self.resolve(purpose)
        chain = self.fallbacks.get(purpose, ())
        for index, model in enumerate(chain):
            if model == current:
                return chain[index + 1] if index + 1 < len(chain) else None
        return chain[0] if chain else None

    def activate_route(self, purpose: str, model: ModelInfo, context: RunContext | None = None) -> None:
        """动态切换并激活指定用途的目标模型，并记录会话审计日志。

        Args:
            purpose: 切换用途（"main", "compression" 等）。
            model: 新的目标模型规格。
            context: 可选的运行上下文（用于记录持久化检查点）。
        """
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
        policy = RetryPolicy()
        transient_retries, argument_retries = 0, 0
        response_accepted = False
        while True:
            context.check_cancelled()
            if attempts["attempts"] >= attempts.get("limit", 4):
                raise ModelError("attempt_limit", "Model physical attempt limit reached")
            attempts["attempts"] += 1
            try:
                async for event in self._stream_once(request, context):
                    if event.kind == "response":
                        response_accepted = True
                    yield event
                return
            except ModelError as error:
                if attempts["attempts"] >= attempts.get("limit", 4):
                    raise
                if error.kind == "tool_arguments" and not response_accepted and not argument_retries:
                    argument_retries += 1
                    retry = argument_retries
                elif error.kind == "transient" and transient_retries < policy.max_retries:
                    transient_retries += 1
                    retry = transient_retries
                else:
                    raise
                emit = getattr(context, "emit", None)
                if emit is not None:
                    await emit("attempt_reset", {"kind": error.kind, "retry": retry})
                if error.kind == "transient":
                    await asyncio.sleep(policy.delay(retry - 1, uniform=random.uniform))

    async def _stream_once(
        self, request: PreparedRequest, context: RunContext
    ) -> AsyncIterator[StreamEvent]:
        """执行单次物理流式请求，管理金融级预算事前预扣与事后核销闭环。

        【执行流程】
        1. 检查取消信号；
        2. 校验入参与输出上限；
        3. 计算货币与 Token 输入消耗上限（monetary_input_bound）；
        4. 预算事前预扣（reserve）；
        5. 驱动 Provider.stream 逐块产出事件；
        6. 收到最终 response 时，执行精确结算（settle）并落盘会话记录。

        Args:
            request: 不可变请求信封。
            context: 执行上下文。

        Yields:
            StreamEvent: 流式文本与响应事件。
        """
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
        from ..pricing import Price

        price_data = self.pricing.get(f"{request.model.provider_id}/{request.model.model_id}")
        price = Price.from_dict(price_data) if price_data else None
        # Keep pressure estimates separate from money. Providers can accept more
        # input than the configured context window (e.g. full-source controls).
        # A byte-tokenized text request cannot require more text tokens than its
        # serialized UTF-8 bytes. Include all system/message/tool/provider data,
        # plus conservative framing allowances, without changing the request.
        source = {"system": request.system,
                  "messages": [message.to_dict() for message in request.messages],
                  "tools": request.tools}
        source_bound = len(canonical(source).encode("utf-8")) + 256 + 32 * (
            len(request.messages) + len(request.tools)
        )
        monetary_input_bound = max(request.model.context_window, source_bound)
        if (
            price
            and getattr(context.budget, "cost_limit_cny", None) is not None
            and request.model.supports_cache
            and (
                (price.cache_write_per_million is None and price.cache_write_billing != "included")
                or price.cache_read_per_million is None
            )
        ):
            from ..runtime_state import BudgetExceeded

            raise BudgetExceeded("Verified cache read and write tariffs are required")
        estimated_cost = price.estimate(monetary_input_bound, output) if price else None
        reserve_kwargs = {"purpose": request.purpose}
        if "estimated_cost_cny" in inspect.signature(context.budget.reserve).parameters:
            reserve_kwargs.update(
                estimated_cost_cny=estimated_cost,
                scope=getattr(context, "budget_scope", None) or getattr(context, "id", None),
            )
        reservation = await context.budget.reserve(estimate + output, **reserve_kwargs)
        actual, usage = None, None
        response = None
        stop_reason = None
        logged = False
        attempt_logged = False
        started_at, first_text = time.monotonic(), None
        state, failure_kind, failure_status = "interrupted", None, None
        failure_diagnostic = {}
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
                        stop_reason = response.stop_reason
                        state = "completed"
                    yield event
                if response is None:
                    raise ModelError("invalid", "Provider returned no final response")
            finally:
                await stream.aclose()
        except BaseException as error:
            if isinstance(error, ModelError):
                if error.usage is not None:
                    usage, actual = error.usage, error.usage.total_tokens
                if error.stop_reason is not None:
                    stop_reason = error.stop_reason
            state = (
                "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "interrupted"
                if isinstance(error, GeneratorExit)
                else "failed"
            )
            failure_kind = error.kind if isinstance(error, ModelError) else None
            failure_status = getattr(error, "status_code", None)
            failure_diagnostic = getattr(error, "diagnostic", {
                "exception_type": type(error).__name__[:64], "error_code": None,
            })
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
                        **failure_diagnostic,
                        "usage": asdict(usage) if usage else None,
                        "stop_reason": stop_reason,
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
        """非流式阻塞等待并返回最终模型响应。

        Args:
            request: 模型请求信封。
            context: 运行上下文。
            attempt_budget: 物理重试尝试计数器字典。

        Returns:
            ModelResponse: 最终模型响应结构体。

        Raises:
            ModelError: 模型调用失败或未产出最终响应。
        """
        response = None
        async for event in self.stream(request, context, attempt_budget=attempt_budget):
            if event.kind == "response":
                response = event.data
        if response is None:
            raise ModelError("invalid", "Provider returned no final response")
        return response

    response = complete

    async def close(self) -> None:
        """优雅关闭注册表中所有供应商网络客户端。"""
        for provider in self.registry.providers.values():
            await provider.close()

__all__ = ['ModelInfo', 'ModelResponse', 'PreparedRequest', 'StreamEvent', 'ToolCall', 'Usage', 'canonical', 'new_id', 'ModelError', 'Provider', 'ProviderRegistry', 'ScriptedProvider', 'OpenAIProvider', 'AnthropicProvider', 'default_model', '_error', '_validate_parameters', '_validate_pairs', '_anthropic_cache', '_reasoning_format', '_get', '_plain', '_arguments', 'RetryPolicy', 'ModelRuntime']

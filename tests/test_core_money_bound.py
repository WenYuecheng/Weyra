"""Money reservations must cover full source even when provider capacity is understated."""
import asyncio
from decimal import Decimal

import pytest

from weyra.contracts import Message, ModelInfo, ModelResponse, PreparedRequest, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.runtime_state import Budget, BudgetExceeded, RunContext
from weyra.session import SessionStore


class RecordingBudget(Budget):
    async def reserve(self, amount, purpose='main', *, estimated_cost_cny=None, scope=None):
        self.money_reserved = estimated_cost_cny
        self.pressure_reserved = amount
        return await super().reserve(amount, purpose, estimated_cost_cny=estimated_cost_cny, scope=scope)


def setup_runtime():
    provider = ScriptedProvider([ModelResponse('ok', usage=Usage(10, 1))])
    registry = ProviderRegistry()
    registry.register('local', provider)
    model = ModelInfo('local', 'text', 4096, 10)
    runtime = ModelRuntime(registry, {'main': model})
    runtime.pricing = {'local/text': {'input_per_million': '1', 'output_per_million': '1'}}
    return runtime, provider, model


@pytest.mark.parametrize('field', ['system', 'messages', 'tools'])
def test_large_source_reserves_more_than_declared_capacity_before_call(tmp_path, field):
    async def run():
        runtime, provider, model = setup_runtime()
        huge = '中文证据' * 3000
        request = PreparedRequest(model, huge if field == 'system' else '',
            (Message('user', huge if field == 'messages' else 'short'),),
            tools=({'name': 'tool', 'description': huge},) if field == 'tools' else (),
            parameters={'estimated_input_tokens': 1})
        session = SessionStore(project=tmp_path)
        budget = RecordingBudget(cost_limit_cny='.01')
        ctx = RunContext(tmp_path, session, budget)
        try:
            with pytest.raises(BudgetExceeded):
                await runtime.complete(request, ctx)
            assert budget.money_reserved > Decimal('.01')
            assert budget.pressure_reserved == 11
            assert provider.requests == []
            assert budget.total_cost_cny == 0
        finally:
            await ctx.close()
            session.close()
    asyncio.run(run())


def test_small_request_retains_capacity_reservation_and_exact_settlement(tmp_path):
    async def run():
        runtime, provider, model = setup_runtime()
        request = PreparedRequest(model, 'system', (Message('user', 'small'),),
                                  parameters={'estimated_input_tokens': 1})
        session = SessionStore(project=tmp_path)
        budget = RecordingBudget(cost_limit_cny='1')
        ctx = RunContext(tmp_path, session, budget)
        try:
            response = await runtime.complete(request, ctx)
            assert response.content == 'ok'
            assert budget.money_reserved == Decimal('0.004106')
            assert budget.total_cost_cny == Decimal('0.000011')
            assert budget.pressure_reserved == 11
            assert provider.requests[0].messages[0].content == 'small'
            assert provider.requests[0].parameters['estimated_input_tokens'] == 1
        finally:
            await ctx.close()
            session.close()
    asyncio.run(run())


def test_provider_usage_above_declared_window_settles_within_source_hold(tmp_path):
    async def run():
        registry = ProviderRegistry()
        provider = ScriptedProvider([ModelResponse('ok', usage=Usage(25883, 100))])
        registry.register('local', provider)
        model = ModelInfo('local', 'text', 16384, 128)
        runtime = ModelRuntime(registry, {'main': model})
        runtime.pricing = {'local/text': {'input_per_million': '1', 'output_per_million': '1'}}
        request = PreparedRequest(model, 'system', (Message('user', '中文证据' * 3000,
            provider_data={'replay': '原始数据' * 100}),), parameters={'estimated_input_tokens': 100})
        original = request.to_dict()
        session = SessionStore(project=tmp_path)
        budget = RecordingBudget(cost_limit_cny='1')
        ctx = RunContext(tmp_path, session, budget)
        try:
            await runtime.complete(request, ctx)
            assert budget.total_cost_cny == Decimal('0.025983')
            assert budget.money_reserved >= budget.total_cost_cny
            assert budget.money_reserved > Decimal('0.016512')
            assert budget.pressure_reserved == 228
            assert request.to_dict() == original
            assert provider.requests[0].messages[0].provider_data == original['messages'][0]['provider_data']
        finally:
            await ctx.close()
            session.close()
    asyncio.run(run())

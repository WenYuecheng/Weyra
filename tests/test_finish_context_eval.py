import asyncio
import importlib
from pathlib import Path

from weyra.contracts import ModelInfo, ModelResponse, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.runtime_state import Budget


def module():
    assert Path('weyra/finish_context_eval.py').exists(), 'finish evaluator is missing'
    return importlib.import_module('benchmarks.finish_context_eval')


def test_manifest_has_six_new_long_histories_and_twenty_four_units():
    evaluation = module()
    cases = evaluation.load_cases()
    assert len(cases) == 6
    assert len(evaluation.units(cases)) == 24
    for case in cases:
        task = evaluation.public_case(case)
        assert 'expected' not in task
        assert len(str(task['input']['history'])) > 20000
        assert case['expected']


def test_baseline_loader_only_replaces_context(tmp_path):
    evaluation = module()
    baseline = tmp_path / 'context.py'
    baseline.write_text('from .contracts import Message\nclass ContextManager: pass\n')
    loaded = evaluation.context_class('before', tmp_path)
    from weyra.contracts import Message
    assert loaded.__module__ != 'weyra.context'
    assert importlib.import_module(loaded.__module__).Message is Message


def test_real_context_unit_preserves_sources_and_host_grades(tmp_path):
    evaluation = module()
    case = evaluation.load_cases()[0]
    import json
    responses = [ModelResponse(json.dumps({'facts': case['expected']}), usage=Usage(100, 40)) for _ in range(3)]
    responses.append(ModelResponse(json.dumps(case['expected']), usage=Usage(100, 40)))
    provider = ScriptedProvider(responses)
    registry = ProviderRegistry()
    registry.register('fake', provider)
    runtime = ModelRuntime(registry, {'main': ModelInfo('fake', 'test', 16384, 4096)})
    budget = Budget()
    result = asyncio.run(evaluation.run_finish_context_case(case, 'after', 0, tmp_path, runtime, budget))
    assert result['status'] == 'pass'
    assert result['metrics']['compaction_published']
    assert result['metrics']['raw_history_preserved']
    assert result['metrics']['facts_retained'] == len(case['expected'])
    assert result['metrics']['physical_requests'] == 4
    assert result['metrics']['pressure_triggered']
    assert all(request.parameters['temperature'] == 0 for request in provider.requests)


def test_timeout_is_recorded_without_replay(tmp_path):
    evaluation = module()
    calls = []
    async def operation():
        calls.append(1)
        await asyncio.sleep(1)
    async def run():
        first = await evaluation.run_once(tmp_path, 'unit', {'id': 'sample'}, operation, timeout_seconds=.01)
        second = await evaluation.run_once(tmp_path, 'unit', {'id': 'sample'}, operation, timeout_seconds=.01)
        assert first == second
        assert first['status'] == 'timeout'
    asyncio.run(run())
    assert len(calls) == 1


def test_fixed_runtime_preserves_production_attempt_counter(tmp_path):
    evaluation = module()
    from weyra.contracts import Message, PreparedRequest
    from weyra.runtime_state import RunContext
    from weyra.session import SessionStore
    registry = ProviderRegistry()
    registry.register('fake', ScriptedProvider([ModelResponse('{}', usage=Usage(10, 2))]))
    model = ModelInfo('fake', 'test', 16384, 4096)
    runtime = ModelRuntime(registry, {'main': model})
    fixed = evaluation._FixedRuntime(runtime)
    import inspect
    assert 'attempt_budget' in inspect.signature(fixed.complete).parameters
    attempts = {'attempts': 0, 'limit': 8}
    store = SessionStore(project=tmp_path)
    context = RunContext(tmp_path, store, Budget())
    request = PreparedRequest(model, '', (Message('user', 'test'),))
    asyncio.run(fixed.complete(request, context, attempt_budget=attempts))
    assert attempts['attempts'] == 1
    store.close()


def test_cost_limited_root_budget_receives_price_and_scope(tmp_path):
    evaluation = module()
    from weyra.contracts import Message, PreparedRequest
    from weyra.runtime_state import RunContext
    from weyra.session import SessionStore
    from decimal import Decimal
    registry = ProviderRegistry()
    provider = ScriptedProvider([ModelResponse('{}', usage=Usage(100, 20))])
    registry.register('fake', provider)
    model = ModelInfo('fake', 'test', 16384, 4096)
    runtime = ModelRuntime(registry, {'main': model})
    runtime.pricing['fake/test'] = {'input_per_million': '1', 'output_per_million': '2'}
    budget = Budget(cost_limit_cny=100)
    store = SessionStore(project=tmp_path)
    budget.attach(store)
    unit_budget = evaluation._UnitBudget(budget)
    context = RunContext(tmp_path, store, unit_budget, budget_scope='paid-offline')
    request = PreparedRequest(model, '', (Message('user', 'test'),))
    asyncio.run(evaluation._FixedRuntime(runtime).complete(request, context))
    assert len(provider.requests) == unit_budget.calls == budget.calls == 1
    assert budget.total_cost_cny == Decimal('0.00014')
    assert budget.records[0]['scope'] == 'paid-offline'
    assert budget.records[0]['cost_known']
    assert not budget._pending
    store.close()

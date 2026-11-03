import asyncio
import json
from pathlib import Path

from weyra.contracts import ModelInfo, ModelResponse, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.runtime_state import Budget


def test_none_preserves_raw_and_does_not_leak_grader(tmp_path):
    from benchmarks.core_context import run_context_case
    provider = ScriptedProvider([ModelResponse('{"value":"ok"}', usage=Usage(90, 10))])
    registry = ProviderRegistry()
    registry.register('test', provider)
    runtime = ModelRuntime(registry, {'main': ModelInfo('test', 'm', 4000, 1024)})
    case = {'id': 'sample', 'input': {'history': [{'role': 'user', 'content': 'fact ' * 2000}], 'prompt': 'Answer JSON'}, 'grader': 'SECRET'}
    result = asyncio.run(run_context_case(case, 'none', 0, tmp_path, runtime, Budget()))
    assert result['status'] == 'ok'
    assert result['metrics']['effective_fold_count'] == 0
    assert result['metrics']['raw_history_preserved']
    records = [json.loads(line) for line in Path(result['events_path']).read_text().splitlines()]
    request = next(r['data'] for r in records if r['kind'] == 'model_request')
    assert 'SECRET' not in json.dumps(request)
    assert result['metrics']['main_calls'] == 1


def test_fixed_fold_and_multifold_records_successful_folds(tmp_path):
    from benchmarks.core_context import run_context_case
    provider = ScriptedProvider([ModelResponse('Facts and current task retained.')] * 2 + [ModelResponse('{}')])
    registry = ProviderRegistry()
    registry.register('test', provider)
    runtime = ModelRuntime(registry, {'main': ModelInfo('test', 'm', 16000, 1024)})
    case = {'id': 'multi', 'input': {'history': [{'role': 'user', 'content': 'fact ' * 300} for _ in range(8)], 'prompt': 'Answer', 'fold_points': [4, 8], 'minimum_folds': 2}}
    result = asyncio.run(run_context_case(case, 'dsh_checkpoint', 0, tmp_path, runtime, Budget()))
    assert result['status'] == 'ok'
    assert result['metrics']['effective_fold_count'] == 2
    assert result['metrics']['summary_calls'] == 2
    assert result['metrics']['raw_history_preserved']


def test_fixture_escape_rejected_before_model_call(tmp_path):
    from benchmarks.core_context import run_context_case
    registry = ProviderRegistry()
    registry.register('test', ScriptedProvider([]))
    runtime = ModelRuntime(registry, {'main': ModelInfo('test', 'm', 4000, 1024)})
    case = {'input': {'history': [], 'prompt': 'x', 'fixtures': {'../escape': 'bad'}}}
    import pytest
    with pytest.raises(ValueError, match='fixture'):
        asyncio.run(run_context_case(case, 'none', 0, tmp_path, runtime, Budget()))


def test_pressure_does_not_force_fold_below_threshold(tmp_path):
    from benchmarks.core_context import run_context_case
    registry = ProviderRegistry()
    registry.register('test', ScriptedProvider([ModelResponse('{}')]))
    runtime = ModelRuntime(registry, {'main': ModelInfo('test', 'm', 16000, 1024)})
    case = {'input': {'history': [{'role': 'user', 'content': 'small'}], 'query': 'Actual query'}}
    result = asyncio.run(run_context_case(case, 'dsh_checkpoint', 0, tmp_path, runtime, Budget(), trigger='pressure'))
    assert result['metrics']['effective_fold_count'] == 0
    events = [json.loads(x) for x in Path(result['events_path']).read_text().splitlines()]
    assert any(r['kind'] == 'message' and r['data']['content'] == 'Actual query' for r in events)


def test_pair_semantic_hash_and_variant_survive_runtime_policy(tmp_path):
    from benchmarks.core_context import run_context_case
    registry = ProviderRegistry()
    registry.register('test', ScriptedProvider([ModelResponse('{"episode_memory":{"x":1},"working_memory":{"x":1},"tool_memory":{}}'), ModelResponse('{}'), ModelResponse('checkpoint'), ModelResponse('{}')]))
    runtime = ModelRuntime(registry, {'main': ModelInfo('test', 'm', 16000, 1024)})
    runtime.context_policy = {'summary_mode': 'three_parallel', 'headroom_tokens': 2048}
    case = {'context_regime': 'overflow', 'input': {'history': [{'role': 'user', 'content': 'fact '*300} for _ in range(4)], 'prompt': 'Answer'}}
    left = asyncio.run(run_context_case(case, 'single_json', 0, tmp_path/'a', runtime, Budget()))
    right = asyncio.run(run_context_case(case, 'dsh_no_prune', 0, tmp_path/'b', runtime, Budget()))
    assert left['metrics']['raw_history_hash'] == right['metrics']['raw_history_hash']
    assert left['metrics']['summary_mode'] == 'single_json'
    assert right['metrics']['summary_mode'] == 'dsh_checkpoint'
    assert left['metrics']['regime'] == 'overflow'


def test_model_overflow_is_error_not_graded_failure(tmp_path):
    from benchmarks.core_context import run_context_case
    from weyra.model_runtime import ModelError
    registry = ProviderRegistry()
    registry.register('test', ScriptedProvider([ModelError('overflow')]))
    runtime = ModelRuntime(registry, {'main': ModelInfo('test', 'm', 4000, 1024)})
    result = asyncio.run(run_context_case({'input': {'prompt':'x'}}, 'none', 0, tmp_path, runtime, Budget()))
    assert result['status'] == 'error'
    assert result['error_kind'] == 'overflow'


def test_pressure_folds_and_prune_ablation_uses_same_source(tmp_path):
    from benchmarks.core_context import run_context_case
    case = {'input': {'history': [
        {'role': 'user', 'content': 'task facts ' * 1000},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id':'read1','name':'read_file','arguments':{}}]},
        {'role': 'tool', 'content': 'line\n' * 2000, 'tool_call_id':'read1','name':'read_file'},
        {'role': 'user', 'content': 'continue ' * 300}], 'prompt':'Answer'}}
    results = []
    for variant in ('dsh_checkpoint', 'dsh_no_prune'):
        registry = ProviderRegistry()
        provider = ScriptedProvider([ModelResponse('Preserved facts and next step.')] * 12)
        registry.register('test', provider)
        runtime = ModelRuntime(registry, {'main': ModelInfo('test', 'm', 16000, 1024)})
        results.append(asyncio.run(run_context_case(case, variant, 0, tmp_path/variant, runtime, Budget(), trigger='pressure')))
    assert results[0]['metrics']['prune_projection_count'] == 1
    assert results[1]['metrics']['prune_projection_count'] == 0
    assert results[1]['metrics']['effective_fold_count'] >= 1
    assert results[0]['metrics']['raw_history_hash'] == results[1]['metrics']['raw_history_hash']


def test_shared_budget_metrics_exclude_other_units(tmp_path):
    from benchmarks.core_context import run_context_case
    class ConcurrentProvider(ScriptedProvider):
        async def stream(self, request, context):
            other = await context.budget.reserve(500, scope='other-unit')
            await context.budget.settle(other, 500, actual_cost_cny='1')
            async for event in super().stream(request, context):
                yield event
    registry = ProviderRegistry()
    registry.register('test', ConcurrentProvider([ModelResponse('{}', usage=Usage(20, 3))]))
    runtime = ModelRuntime(registry, {'main': ModelInfo('test', 'm', 4000, 1024)})
    result = asyncio.run(run_context_case({'input':{'prompt':'x'}}, 'none', 0, tmp_path, runtime, Budget()))
    assert result['metrics']['accounted_tokens'] == 23
    assert result['metrics']['cost_cny'] == '0'


def test_overflow_source_uses_bounded_summary_segments(tmp_path):
    from benchmarks.core_context import run_context_case
    registry = ProviderRegistry()
    registry.register('test', ScriptedProvider([ModelResponse('Retained shipment decisions and constraints.')] * 30))
    runtime = ModelRuntime(registry, {'main': ModelInfo('test', 'm', 16384, 1024)})
    case = {'context_regime':'overflow', 'input': {'history': [{'role':'user','content': f'Batch {i}: ' + 'distinct recorded shipment decision. '*90} for i in range(40)], 'prompt':'Answer', 'minimum_folds':1}}
    result = asyncio.run(run_context_case(case, 'dsh_checkpoint', 0, tmp_path, runtime, Budget()))
    assert result['status'] == 'ok'
    assert result['metrics']['summary_segment_count'] > 1
    assert result['metrics']['effective_fold_count'] == 1
    assert result['metrics']['raw_history_preserved']

import asyncio
import json
from weyra.contracts import ModelInfo, ModelResponse, StreamEvent, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry
from weyra.runtime_state import Budget
from benchmarks.core_evaluation import CoreRunner


class DevelopmentProvider:
    async def close(self):
        pass

    async def stream(self, request, context):
        if request.system.startswith('Compile concise'):
            text='Keep x=1 for numeric tasks, and preserve other abilities.'
        elif request.system.startswith('Maintain reusable'):
            target=json.loads(request.messages[0].content)['target']
            text=json.dumps({'action':'apply','name':target['name'],
                'edits':[{'op':'replace','old':'x=0','new':'x=1'}]})
        elif request.system.startswith('Revise the reusable'):
            text='Return x=1 for numeric tasks.'
        else:
            text='{"x":1}' if 'x=1' in request.system else '{"x":0}'
        yield StreamEvent('response',ModelResponse(text,usage=Usage(10,5)))


def test_all_four_arms_execute_experience_gate_and_publish(tmp_path):
    async def check():
        reg=ProviderRegistry()
        reg.register('test',DevelopmentProvider())
        runtime=ModelRuntime(reg,{'main':ModelInfo('test','m',16000,4096)})
        runner=CoreRunner(tmp_path,runtime,Budget(),repeats=1)
        tasks=[{'id':f't{i}','type':'evolution','split':split,'input':{'objective':'Return a numeric field'},
                'grading':{'expected':{'x':1}}} for i,split in enumerate(['train']*4+['validation']*2+['test']*3)]
        runner.manifest={'evolution_families':[{'id':'numeric','initial_body':'Return x=0 for numeric tasks.',
                            'durable_feedback':'Always use x=1 in this task family.','tasks':tasks}]}
        await runner.evolution()
        maintenance=[v for k,v in runner.journal.results.items() if k.startswith('maintenance/')]
        assert len(maintenance)==4
        assert all(r['status']=='pass' for r in maintenance), maintenance
        assert all('x=1' in r['body'] for r in maintenance)
        assert all(any(x.get('result',{}).get('status')=='updated' for x in r['rounds']) for r in maintenance)
        runner.journal.close()
    asyncio.run(check())


def test_validation_provider_failure_rejects_both_methods_and_continues(tmp_path, monkeypatch):
    from weyra import core_evaluation as core
    from weyra.model_runtime import ModelError
    from weyra.skills import SkillStore

    async def check():
        monkeypatch.setattr(core, 'ARMS', ('no_experience_full', 'no_experience_bounded'))
        reg = ProviderRegistry()
        reg.register('test', DevelopmentProvider())
        runtime = ModelRuntime(reg, {'main': ModelInfo('test', 'm', 16000, 4096)})
        runner = CoreRunner(tmp_path, runtime, Budget(), repeats=1)
        tasks = [{'id': f't{i}', 'split': split, 'input': {'objective': 'Return x'},
                  'grading': {'expected': {'x': 1}}}
                 for i, split in enumerate(['train'] * 4 + ['validation'] * 2 + ['test'] * 3)]
        runner.manifest = {'evolution_families': [{'id': 'numeric',
            'initial_body': 'Return x=0 for numeric tasks.', 'durable_feedback': 'Always x=1',
            'tasks': tasks}]}
        train_calls = []
        async def infer(task, body, folder, *, phase='test'):
            if phase == 'validation':
                raise ModelError('transient', 'provider unavailable')
            if phase == 'train':
                train_calls.append(task['id'])
            return {'status': 'pass', 'answer': '{"x":1}',
                    'grade': {'success': True, 'hard_failures': 0}}
        monkeypatch.setattr(runner, 'infer', infer)
        try:
            await runner.evolution()
            for arm in core.ARMS:
                row = runner.journal.results[f'maintenance/numeric/{arm}/0']
                assert row['status'] == 'pass'
                assert len(row['rounds']) == 2
                assert all('gate' not in item for item in row['rounds'])
                for item in row['rounds']:
                    assert item['result']['status'] == 'rejected'
                    assert item['result']['reason'] == 'Validation failed: ModelError'
                    assert not item['result'].get('validated', False)
                active = SkillStore(tmp_path / 'units' / f'numeric-{arm}-0', []).load('numeric')
                assert active.version == '1.0.0'
                assert active.body == 'Return x=0 for numeric tasks.'
            assert len(train_calls) == 8
        finally:
            runner.journal.close()
    asyncio.run(check())


def test_validation_budget_failure_stops_full_and_bounded_without_publish(tmp_path, monkeypatch):
    from weyra import core_evaluation as core
    from weyra.runtime_state import BudgetExceeded
    from weyra.skills import SkillStore

    async def check(arm):
        monkeypatch.setattr(core, 'ARMS', (arm,))
        reg = ProviderRegistry()
        reg.register('test', DevelopmentProvider())
        runtime = ModelRuntime(reg, {'main': ModelInfo('test', 'm', 16000, 4096)})
        runner = CoreRunner(tmp_path / arm, runtime, Budget(), repeats=1)
        tasks = [{'id': f't{i}', 'split': split, 'input': {}, 'grading': {'expected': {'x': 1}}}
                 for i, split in enumerate(['train'] * 4 + ['validation'] * 2)]
        runner.manifest = {'evolution_families': [{'id': 'numeric',
            'initial_body': 'Return x=0 for numeric tasks.', 'durable_feedback': 'Always x=1',
            'tasks': tasks}]}
        async def infer(task, body, folder, *, phase='test'):
            if phase == 'validation':
                raise BudgetExceeded('exhausted validation budget')
            return {'status': 'pass', 'answer': '{"x":1}',
                    'grade': {'success': True, 'hard_failures': 0}}
        monkeypatch.setattr(runner, 'infer', infer)
        try:
            await runner.evolution()
            assert runner.stopped
            row = runner.journal.results[f'maintenance/numeric/{arm}/0']
            assert row['status'] == 'budget_stopped'
            active = SkillStore(tmp_path / arm / 'units' / f'numeric-{arm}-0', []).load('numeric')
            assert active.version == '1.0.0'
            assert active.body == 'Return x=0 for numeric tasks.'
        finally:
            runner.journal.close()
    for arm in ('no_experience_full', 'no_experience_bounded'):
        asyncio.run(check(arm))

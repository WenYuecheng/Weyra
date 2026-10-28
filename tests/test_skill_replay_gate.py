import asyncio
import json
from types import SimpleNamespace

from weyra.contracts import ModelInfo, ModelResponse
from weyra.runtime_state import Budget
from weyra.skill_judge import ConfiguredSkillGate, CRITERIA


class Runtime:
    routes = {'skill_judge': ModelInfo('test', 'judge', 8192, 4096)}
    def __init__(self):
        self.requests = []
    async def complete(self, request, context):
        self.requests.append(request)
        tasks = json.loads(request.messages[0].content)
        cases = {}
        for cid, payload in tasks.items():
            a = payload['A']['final'] == 'good'
            b = payload['B']['final'] == 'good'
            cases[cid] = {'scores': {'A': dict.fromkeys(CRITERIA, a), 'B': dict.fromkeys(CRITERIA, b)},
                          'winner': 'tie' if a == b else 'A' if a else 'B',
                          'evidence': {'A': 'Observed A', 'B': 'Observed B'}}
        return ModelResponse(json.dumps({'cases': cases}))


def case(cid, split='validation'):
    return {'id': cid, 'prompt': 'Answer accurately', 'files': {}, 'rubric': 'accurate answer',
            'output_mode': 'text', 'split': split}


def window(cases):
    return SimpleNamespace(reason='', version='one', cases=cases,
                           validation=[c for c in cases if c['split'] == 'validation'])


def snapshot(body):
    return SimpleNamespace(name='demo', body=body, content_hash=body, resources={})


def gate():
    gate = ConfiguredSkillGate(Runtime(), Budget(), {'cases': [case('a')]})
    gate.executions = []
    async def execute(snap, task, context=None):
        gate.executions.append((snap.body, task['id']))
        answer = 'good' if snap.body == 'good' or task['id'] == 'stable' else 'bad'
        return {'final': answer, 'artifacts': {}, 'passed': True, 'hard_failures': 0, 'hard_checks': []}
    gate._execute = execute
    return gate


def test_two_batched_calls_and_baseline_cache_invalidation_and_saturation():
    g = gate()
    w = window([case('a'), case('b')])
    report = asyncio.run(g.evaluate(snapshot('bad'), snapshot('good'), w))
    assert report['model_approved']
    assert len(g.runtime.requests) == 2
    assert g.saturated(snapshot('good'), w)
    assert not g.saturated(snapshot('bad'), w)
    g.executions.clear()
    asyncio.run(g.evaluate(snapshot('good'), snapshot('next'), w))
    assert g.executions == [('next', 'a'), ('next', 'b')]
    g.executions.clear()
    w.cases[0]['rubric'] = 'new rubric'
    asyncio.run(g.evaluate(snapshot('good'), snapshot('next'), w))
    assert ('good', 'a') in g.executions


def test_development_gain_does_not_approve_and_validation_pass_cannot_regress():
    g = gate()
    w = window([case('a', 'development'), case('stable')])
    report = asyncio.run(g.evaluate(snapshot('bad'), snapshot('good'), w))
    assert report['candidate']['successes'] == report['old']['successes'] == 1
    assert not report['model_approved']
    g = gate()
    async def execute(snap, task, context=None):
        good = (snap.body == 'old') == (task['id'] == 'stable')
        return {'final': 'good' if good else 'bad', 'artifacts': {}, 'passed': True, 'hard_failures': 0, 'hard_checks': []}
    g._execute = execute
    report = asyncio.run(g.evaluate(snapshot('old'), snapshot('new'), window([case('stable'), case('a'), case('b')])))
    assert report['candidate']['successes'] > report['old']['successes']
    assert report['reason'] == 'regression'
    assert not report['model_approved']


def test_unreplayable_window_makes_no_calls():
    g = gate()
    w = window([case('a')])
    w.reason = 'missing_snapshots'
    report = asyncio.run(g.evaluate(snapshot('bad'), snapshot('good'), w))
    assert not report['model_approved']
    assert not g.executions and not g.runtime.requests


def test_actual_text_and_existing_file_edit_replay(tmp_path):
    from weyra.contracts import ToolCall
    from weyra.demos import scripted_runtime
    for text in (True, False):
        responses = [ModelResponse(tool_calls=(ToolCall('s', 'skill', {'name': 'demo'}),))]
        if not text:
            responses.extend([
                ModelResponse(tool_calls=(ToolCall('r', 'read_file', {'path': 'input.txt'}),)),
                ModelResponse(tool_calls=(ToolCall('e', 'edit_file', {'path': 'input.txt', 'old_text': 'old', 'new_text': 'new'}),)),
            ])
        responses.append(ModelResponse('Clear final answer'))
        runtime = scripted_runtime(responses)
        runtime.routes['skill_judge'] = ModelInfo('offline', 'judge', 200000, 4096)
        fixture = case('actual')
        if not text:
            fixture.update(files={'input.txt': 'old'}, output_mode='files',
                           hard_constraints={'writable_input_paths': ['input.txt'],
                                             'protected_input_paths': [], 'required_output_paths': ['input.txt']})
        g = ConfiguredSkillGate(runtime, Budget(), {'cases': [fixture]})
        outcome = asyncio.run(g._execute(snapshot('good'), g.cases[0]))
        assert outcome['passed']
        if not text:
            assert outcome['artifacts']['input.txt'] == 'new'


def test_hard_failure_stops_paid_execution_immediately():
    g = gate()
    calls = []
    async def execute(snap, task, context=None):
        calls.append(task['id'])
        return {'final': '', 'artifacts': {}, 'passed': False, 'hard_failures': 1, 'hard_checks': []}
    g._execute = execute
    report = asyncio.run(g.evaluate(snapshot('bad'), snapshot('good'), window([case('a'), case('b')])))
    assert report['reason'] == 'old_execution_hard_failure'
    assert calls == ['a']
    assert not g.runtime.requests


def test_nonwritable_inputs_remain_protected_with_explicit_empty_protection():
    from weyra.contracts import ToolCall
    from weyra.demos import scripted_runtime
    runtime = scripted_runtime([
        ModelResponse(tool_calls=(ToolCall('s', 'skill', {'name': 'demo'}),)),
        ModelResponse(tool_calls=(ToolCall('r', 'read_file', {'path': 'input.txt'}),)),
        ModelResponse(tool_calls=(ToolCall('e', 'edit_file', {'path': 'input.txt', 'old_text': 'old', 'new_text': 'new'}),)),
        ModelResponse('answer'),
    ])
    runtime.routes['skill_judge'] = ModelInfo('offline', 'judge', 200000, 4096)
    fixture = {**case('protection'), 'files': {'input.txt': 'old'},
               'hard_constraints': {'protected_input_paths': []}}
    g = ConfiguredSkillGate(runtime, Budget(), {'cases': [fixture]})
    outcome = asyncio.run(g._execute(snapshot('good'), g.cases[0]))
    assert not outcome['passed']
    assert any(c['name'] == 'preserve_inputs' and not c['passed'] for c in outcome['hard_checks'])


def test_online_callback_uses_bounded_fixtures_without_invoking_legacy_code():
    from weyra.skill_judge import ReviewedCallbackGate
    calls = []
    async def exercise():
        def callback(old, candidate):
            calls.append(1)
            raise AssertionError("Legacy callback must not run online")
        callback.validation_cases = [case('host')]
        g = ReviewedCallbackGate(Runtime(), Budget(), callback)
        bounded = g._bounded_gate()
        assert bounded.cases[0]['id'] == 'host'
        async def evaluate(*args, **kwargs):
            return {'model_approved': True}
        bounded.evaluate = evaluate
        report = await g.evaluate(snapshot('bad'), snapshot('good'), window([case('host')]))
        assert report['model_approved']
        assert report['callback_mode'] == 'bounded_validation_cases'
        assert not g.runtime.requests
    asyncio.run(exercise())
    assert calls == []


def test_complete_bounded_output_and_binary_or_oversize_rejection(tmp_path):
    from weyra.skill_judge import _capture_outputs
    content = 'long output' * 4000
    (tmp_path / 'out.txt').write_text(content)
    artifacts, final, ok = _capture_outputs(tmp_path, {'out.txt'}, 'answer' * 2000)
    assert ok and artifacts['out.txt'] == content and len(final) == 12000
    (tmp_path / 'out.txt').write_bytes(b'x' * (64 * 1024 + 1))
    assert not _capture_outputs(tmp_path, {'out.txt'}, 'answer')[2]
    (tmp_path / 'out.txt').write_bytes(b'\xff\x00')
    assert not _capture_outputs(tmp_path, {'out.txt'}, 'answer')[2]
    assert not _capture_outputs(tmp_path, set(), 'x' * (64 * 1024 + 1))[2]


def test_runtime_cache_invalidation_includes_fallback_and_safe_endpoint():
    from weyra.skill_replay_gate import _key, _provider_identity
    g = gate()
    provider = SimpleNamespace(base_url='https://user:password@example.com/v1?api_key=secret#private')
    g.runtime.registry = SimpleNamespace(providers={'test': provider})
    identity = _provider_identity(g.runtime)
    assert identity['test']['endpoint'] == 'https://example.com/v1'
    assert all(secret not in json.dumps(identity) for secret in ('password', 'secret', 'private'))
    first = _key(g, snapshot('good'), case('a'))
    g.runtime.fallback_parameters = {('main', 'test', 'model'): {'temperature': .2}}
    assert _key(g, snapshot('good'), case('a')) != first
    second = _key(g, snapshot('good'), case('a'))
    provider.base_url = 'https://other.example.com/v1'
    assert _key(g, snapshot('good'), case('a')) != second


def test_fork_context_rejects_without_execution():
    g = ConfiguredSkillGate(Runtime(), Budget(), {'cases': [case('a')]})
    skill = snapshot('good')
    skill.context = 'fork'
    result = asyncio.run(g._execute(skill, g.cases[0]))
    assert result['reason'] == 'unsupported_fork_skill'
    assert not result['passed'] and result['hard_failures'] == 1
    assert not g.runtime.requests


def test_captured_system_instructions_are_included_in_replay(monkeypatch):
    from weyra.contracts import ToolCall
    from weyra.demos import scripted_runtime
    runtime = scripted_runtime([ModelResponse(tool_calls=(ToolCall('s', 'skill', {'name': 'demo'}),)), ModelResponse('answer')])
    runtime.routes['skill_judge'] = ModelInfo('offline', 'judge', 200000, 4096)
    observed = []
    import weyra.agent as agent_module
    original_agent = agent_module.Agent
    def capture_agent(*args, **kwargs):
        observed.append(kwargs['system'])
        return original_agent(*args, **kwargs)
    monkeypatch.setattr(agent_module, 'Agent', capture_agent)
    fixture = {**case('instructions'), 'system_instructions': 'Use the established house style.'}
    g = ConfiguredSkillGate(runtime, Budget(), {'cases': [fixture]})
    assert asyncio.run(g._execute(snapshot('good'), g.cases[0]))['passed']
    assert any('Use the established house style.' in text and 'First load the installed skill' in text for text in observed)

"""Offline safety and denominator checks for the bounded paid entrypoint."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from scripts.run_skill_replay import ReplayRunner, prepare, report_existing
from weyra.contracts import ModelInfo
from weyra.runtime_state import Budget


def runtime():
    class OfflineRuntime:
        routes = {p: ModelInfo('test', p, 32768, 4096) for p in ('main', 'evolution', 'skill_judge')}
        def parameters_for(self, purpose):
            return {'max_output_tokens': 1536}
        async def complete(self, *args, **kwargs):
            raise AssertionError('offline mode must not make model requests')
    return OfflineRuntime()


def test_prepare_report_offline_and_protocol_frozen(tmp_path):
    output = tmp_path / 'new'
    protocol = prepare(output, tmp_path, runtime=runtime())
    assert protocol['planned_executions'] == 72
    assert protocol['cost_limit_cny'] == '50'
    assert protocol['max_candidates_per_family'] == 1
    assert protocol['max_replay_samples'] == 4
    assert prepare(output, tmp_path, runtime=runtime()) == protocol
    with pytest.raises(ValueError, match='frozen'):
        prepare(output, tmp_path, runtime=SimpleNamespace(routes={'main': ModelInfo('x', 'y', 4096, 2048)}, parameters_for=lambda p: {}))
    summary = report_existing(output)
    assert summary['planned_final_tasks'] == 72
    assert summary['unstarted_final_tasks'] and summary['recorded_final_tasks'] == 0
    assert summary['budget_limit_cny'] == '50'
    serialized = json.dumps(protocol)
    assert 'expected_artifacts' not in serialized
    assert 'base_url' not in serialized and 'api_key' not in serialized


def test_user_pause_stops_before_runtime_configuration_even_for_new_output(tmp_path, monkeypatch):
    import scripts.run_skill_replay as campaign
    marker = tmp_path / '.weyra/benchmarks/evaluation-paused.json'
    marker.parent.mkdir(parents=True)
    marker.write_text('{"reason":"user_requested"}')
    def forbidden(_):
        raise AssertionError('Paused evaluation must not initialize a paid runtime')
    monkeypatch.setattr(campaign, 'configure_runtime', forbidden)
    output = tmp_path / 'fresh-output'
    result = asyncio.run(campaign.run(output, tmp_path))
    assert result['status'] == 'paused_by_user'
    assert result['paid_requests'] == 0
    assert not output.exists()


def test_pause_keeps_offline_prepare_and_report_available(tmp_path):
    marker = tmp_path / '.weyra/benchmarks/evaluation-paused.json'
    marker.parent.mkdir(parents=True)
    marker.write_text('{}')
    output = tmp_path / 'offline'
    assert prepare(output, tmp_path, runtime=runtime())['planned_executions'] == 72
    assert report_existing(output)['recorded_final_tasks'] == 0


def test_fresh_root_ledger_and_unchanged_adapted(tmp_path):
    runner = ReplayRunner(tmp_path / 'run', runtime(), Budget(cost_limit_cny='50'))
    try:
        assert runner.budget._journal.session_id == 'skill-replay-cny50'
        assert runner.budget._journal.project_path == (tmp_path / 'run').resolve()
        for family_id, family in runner.manifest['families'].items():
            runner.maintenance[family_id] = {'committed_body': family['initial_skill'], 'status': 'completed'}
        result = runner.report()
        assert result['actual_updated_families'] == 0
        assert result['not_adapted_families'] == 3
        assert sum(g['n'] for g in result['groups'].values()) == 72
    finally:
        runner.close()


def test_root_budget_increment_and_resume(tmp_path):
    output = tmp_path / 'run'
    runner = ReplayRunner(output, runtime(), Budget(cost_limit_cny='50'))
    async def spend():
        reservation = await runner.budget.reserve(10, estimated_cost_cny='2', scope='maintenance/evidence')
        await runner.budget.settle(reservation, 7, actual_cost_cny='1')
    try:
        asyncio.run(spend())
        assert str(runner.budget.total_cost_cny) == '1'
    finally:
        runner.close()
    runner = ReplayRunner(output, runtime(), Budget(cost_limit_cny='50'))
    try:
        assert str(runner.budget.total_cost_cny) == '1'
    finally:
        runner.close()


def test_maintenance_has_real_replay_but_no_gold_in_proposal(tmp_path, monkeypatch):
    from scripts.run_skill_replay import maintain_family, gate_factory
    from benchmarks.final_skill_eval import load_manifest, gate_manifest, write_skill
    from weyra.contracts import ModelResponse
    import weyra.agent
    import weyra.skill_evolution

    family_id = 'evidence'
    manifest = load_manifest()
    family = manifest['families'][family_id]
    workspace = tmp_path / 'maintenance'
    write_skill(workspace, family_id, family['initial_skill'])
    seen, cleaned = [], []
    class FakeAgent:
        def __init__(self, project, runtime, **kwargs):
            assert kwargs['enable_evolution'] is False
            self.tools = SimpleNamespace(specs={})
            self.session = SimpleNamespace()
            self._pending_window = None
        async def run_once(self, prompt):
            assert self.enable_evolution
            assert self._delegation_task_id == 'maintenance/evidence'
            assert budget._scopes[self._delegation_task_id]['limit'] == 20000
            self._pending_window = {'user': prompt, 'assistant': 'Attempted development task',
                'replay': {'turn_id': 'actual-turn', 'complete': True,
                           'files': family['development'][0]['files'], 'context': [],
                           'written_paths': ['answer.json']}}
        async def close(self, **kwargs):
            pass
    class FakeEvolution:
        def __init__(self, *args, **kwargs):
            pass
        def knowledge_prompt(self, name):
            return ''
        async def maintain(self, evidence, propose, **kwargs):
            window = kwargs['validation_context']
            assert len(window.development) == 2 and len(window.validation) == 2
            assert window.development[0]['source_window_id'] == 'actual-turn'
            assert kwargs['run_context'].budget_scope == 'maintenance/evidence'
            assert kwargs['run_context'].budget is budget
            reservation = await budget.reserve(9, estimated_cost_cny='0', scope=kwargs['run_context'].budget_scope)
            await budget.settle(reservation, 7, actual_cost_cny='0')
            kwargs['run_context'].register_resource(lambda: cleaned.append(True))
            await propose(None)
            return {'status': 'discarded', 'validation_report': {'cache': {'hits': 0, 'misses': 0}}}
    rt = runtime()
    async def complete(request, ctx):
        seen.append(request)
        return ModelResponse('{"action":"discard","reason":"no improvement"}')
    rt.complete = complete
    monkeypatch.setattr(weyra.agent, 'Agent', FakeAgent)
    monkeypatch.setattr(weyra.skill_evolution, 'EvolutionStore', FakeEvolution)
    budget = Budget(cost_limit_cny='50')
    gate = gate_factory(rt, budget)(family_id, gate_manifest(family_id, manifest), workspace)
    result = asyncio.run(maintain_family(family_id=family_id, family=family, workspace=workspace,
                                       runtime=rt, budget=budget, gate=gate))
    assert len(seen) == 1 and seen[0].purpose == 'evolution'
    payload = json.loads(seen[0].messages[0].content)
    assert 'expected_artifacts' not in json.dumps(payload)
    assert 'replay' not in payload['evidence']
    assert cleaned == [True]
    assert result['evidence']['metrics']['tokens'] == 7
    assert budget._scopes['maintenance/evidence']['tokens'] == 7
    for case in family['development'] + family['validation']:
        for gold in case['expected_artifacts'].values():
            assert gold not in json.dumps(payload)
    assert result['evidence']['actual_updated'] is False
    assert len(result['rounds']) == 1


def test_finished_units_stay_frozen_and_errors_remain_in_72(tmp_path):
    from benchmarks.final_skill_eval import planned_units
    from weyra.runtime_state import BudgetExceeded
    runner = ReplayRunner(tmp_path / 'run', runtime(), Budget(cost_limit_cny='50'))
    key, identity = next(iter(planned_units(runner.manifest).items()))
    async def check():
        async def error():
            raise ValueError('fixture failure')
        row = await runner._unit(key, identity, error)
        assert row['status'] == 'error'
        async def forbidden():
            raise AssertionError('completed unit must never be rerun')
        assert await runner._unit(key, identity, forbidden) == row
        with pytest.raises(BudgetExceeded):
            await runner.budget.reserve(1, estimated_cost_cny='50.01')
    try:
        asyncio.run(check())
        report = runner.report()
        assert report['statuses'] == {'error': 1, 'not_run': 71}
        assert sum(g['n'] for g in report['groups'].values()) == 72
    finally:
        runner.close()


def test_local_maintenance_scope_stop_returns_snapshot_and_heldout_continues(tmp_path, monkeypatch):
    from scripts.run_skill_replay import maintain_family, gate_factory
    from benchmarks.final_skill_eval import gate_manifest
    from weyra.runtime_state import BudgetExceeded
    import weyra.agent

    class ScopeExhaustedAgent:
        def __init__(self, project, runtime, **kwargs):
            self.tools = SimpleNamespace(specs={})
            self.session = SimpleNamespace()
        async def run_once(self, prompt):
            assert budget._scopes[self._delegation_task_id]['limit'] == 0
            raise BudgetExceeded('Task scope token budget exhausted')
        async def close(self, **kwargs):
            pass
    monkeypatch.setattr(weyra.agent, 'Agent', ScopeExhaustedAgent)
    budget = Budget(cost_limit_cny='50', maintenance_limit=0)
    rt = runtime()
    runner = ReplayRunner(tmp_path / 'run', rt, budget, max_maintenance_rounds=1,
                          gate_factory=gate_factory(rt, budget), maintenance_callback=maintain_family)
    executed = []
    async def heldout(case, old_body, arm, folder, **kwargs):
        executed.append((case['id'], arm))
        return {'status': 'fail'}
    runner.task = heldout
    try:
        report = asyncio.run(runner.evaluate())
        assert len(executed) == 72
        assert report['recorded_final_tasks'] == 72
        assert not runner.stopped
        assert report['actual_updated_families'] == 0
        for family_id, snapshot in runner.maintenance.items():
            assert snapshot['status'] == 'budget_stopped'
            assert snapshot['committed_body'].strip() == runner.manifest['families'][family_id]['initial_skill'].strip()
            assert len(snapshot['rounds']) == 1
            assert snapshot['maintenance_evidence']['metrics']['tokens'] == 0
            assert (runner.root / 'maintenance' / (family_id + '-snapshot.json')).is_file()
            assert budget._scopes['maintenance/' + family_id]['limit'] == 0
            assert len(gate_manifest(family_id)['cases']) == 4
    finally:
        runner.close()

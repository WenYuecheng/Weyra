import asyncio
import pytest
from weyra.skills import SkillStore
from weyra.skill_evolution import EvolutionStore


def store(tmp_path):
    for name, aliases in [('alpha', '中文方法'), ('beta', '另一方法')]:
        p = tmp_path / '.weyra' / 'skills' / name / 'SKILL.md'
        p.parent.mkdir(parents=True)
        p.write_text(f'---\nname: {name}\naliases: {aliases}\nversion: 1.0.0\n---\nshared method {name}\n')
    return SkillStore(tmp_path, [])


def test_resolution_priority_and_abstention(tmp_path):
    skills = store(tmp_path)
    assert skills.resolve_target('中文方法', used_ids=['beta']).target.name == 'beta'
    assert skills.resolve_target('中文方法').target.name == 'alpha'
    assert skills.resolve_target('shared method').target is None
    assert skills.resolve_target('alpha', explicit_id='missing').target is None
    assert skills.resolve_target('中文方法', explicit_id='beta', used_ids=['alpha']).source == 'explicit_id'
    assert skills.resolve_target('中文方法', used_ids=['alpha', 'beta']).target is None


@pytest.mark.parametrize('outcome', ['tie', 'degrade', 'exception', 'improve'])
def test_gate_preserves_active_until_strict_improvement(tmp_path, outcome):
    skills = store(tmp_path)
    evolution = EvolutionStore(skills)
    old = skills.load('alpha')
    original = old.path.read_bytes()
    async def propose(_):
        return {'action': 'apply', 'name': 'alpha', 'edits': [{'op': 'append', 'text': 'new rule'}]}
    async def gate(before, candidate):
        assert before == old
        assert candidate.body.endswith('new rule')
        assert old.path.read_bytes() == original
        if outcome == 'exception':
            raise RuntimeError('validation failed')
        return {'old': {'successes': 1, 'hard_failures': 0}, 'candidate': {'successes': 2 if outcome != 'tie' else 1, 'hard_failures': 1 if outcome == 'degrade' else 0}}
    result = asyncio.run(evolution.maintain({}, propose, permission_mode='acceptEdits', target_snapshot=old, validation_gate=gate))
    if outcome == 'improve':
        assert result['status'] == 'updated'
        assert result['validation'] == 'validated'
    else:
        assert result['status'] == 'rejected'
        assert old.path.read_bytes() == original
        assert skills.load('alpha').version == old.version


def test_ambiguous_resolution_cannot_create_duplicate(tmp_path):
    skills = store(tmp_path)
    evolution = EvolutionStore(skills)
    async def propose(_):
        return {'action': 'create', 'name': 'duplicate', 'description': 'duplicate', 'body': 'shared method'}
    result = asyncio.run(evolution.maintain({}, propose, authorized=True,
        resolution={'source': 'lexical', 'reason': 'ambiguous', 'candidates': ['alpha', 'beta']}))
    assert result['status'] == 'rejected'
    assert 'duplicate' not in skills.capture_catalog()


def test_gate_resource_drift_rejects_publication(tmp_path):
    skills = store(tmp_path)
    resource = skills.load('alpha').path.parent / 'reference.txt'
    resource.write_text('old')
    old = skills.load('alpha')
    async def propose(_):
        return {'action': 'apply', 'name': 'alpha', 'edits': [{'op': 'append', 'text': 'new'}]}
    async def gate(before, candidate):
        resource.write_text('changed')
        return {'old': {'successes': 0, 'hard_failures': 0}, 'candidate': {'successes': 1, 'hard_failures': 0}}
    result = asyncio.run(EvolutionStore(skills).maintain({}, propose, authorized=True, target_snapshot=old, validation_gate=gate))
    assert result['status'] == 'rejected'
    assert skills.load('alpha').content_hash == old.content_hash


def test_ungated_creation_records_unvalidated(tmp_path):
    evolution = EvolutionStore(store(tmp_path))
    result = evolution.create('new-method', 'new body', authorized=True)
    assert result['validation'] == 'unvalidated'


def test_gated_create_abstains(tmp_path):
    evolution = EvolutionStore(store(tmp_path))
    async def propose(_):
        return {'action': 'create', 'name': 'new', 'body': 'new reusable method'}
    async def gate(old, candidate):
        raise AssertionError('creation has no comparison baseline')
    result = asyncio.run(evolution.maintain({}, propose, authorized=True, validation_gate=gate))
    assert result['status'] == 'rejected'
    assert 'baseline' in result['reason']


def test_pending_window_tracks_only_current_successful_skills(tmp_path, monkeypatch):
    from weyra.agent import Agent
    from weyra.demos import scripted_runtime
    async def run():
        agent = Agent(tmp_path, scripted_runtime([]), enable_memory=False, enable_evolution=False)
        agent.session.append('tool_result', {'name': 'skill', 'status': 'success', 'structured': {'name': 'previous'}})
        async def turn(*args, **kwargs):
            agent.session.append('tool_result', {'name': 'skill', 'status': 'error', 'structured': {'name': 'failed'}})
            agent.session.append('tool_result', {'name': 'skill', 'status': 'success', 'structured': {'name': 'current'}})
            return 'done'
        monkeypatch.setattr(agent, '_run_turn', turn)
        await agent.run_once('task')
        assert agent._pending_window['used_skill_ids'] == ['current']
        await agent.close()
    asyncio.run(run())


def test_online_validation_gate_is_explicitly_configurable(tmp_path):
    from weyra.agent import Agent
    from weyra.demos import scripted_runtime
    async def gate(old, candidate):
        return {}
    async def run():
        agent = Agent(tmp_path, scripted_runtime([]), evolution_validation_gate=gate)
        assert agent.evolution_validation_gate is gate
        await agent.close()
    asyncio.run(run())


def test_chinese_alias_in_natural_feedback(tmp_path):
    assert store(tmp_path).resolve_target('请修正中文方法的规则').target.name == 'alpha'


def test_validation_budget_exhaustion_propagates_without_publication(tmp_path):
    from weyra.runtime_state import BudgetExceeded
    skills = store(tmp_path)
    old = skills.load('alpha')
    original = old.path.read_bytes()
    async def propose(_):
        return {'action': 'apply', 'name': 'alpha', 'edits': [{'op': 'append', 'text': 'new'}]}
    async def gate(before, candidate):
        raise BudgetExceeded('test budget exhausted')
    with pytest.raises(BudgetExceeded):
        asyncio.run(EvolutionStore(skills).maintain({}, propose, authorized=True,
            target_snapshot=old, validation_gate=gate))
    assert old.path.read_bytes() == original
    assert skills.load('alpha').version == old.version


def test_model_cannot_forge_validation_label(tmp_path):
    skills = store(tmp_path)
    async def propose(_):
        return {'action': 'apply', 'name': 'alpha', '_validation': 'validated',
                'edits': [{'op': 'append', 'text': 'new'}]}
    result = asyncio.run(EvolutionStore(skills).maintain({}, propose, authorized=True))
    assert result['status'] == 'updated'
    assert result['validation'] == 'unvalidated'


def test_hyphenated_skill_name_does_not_match_shorter_prefix(tmp_path):
    for name in ('report', 'report-style'):
        path = tmp_path / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: {name}\n---\nbody')
    skills = SkillStore(tmp_path, [])
    assert skills.resolve_target('Please use $report-style').target.name == 'report-style'
    assert skills.resolve_target('Please use report, then report-style').reason == 'ambiguous'


def test_resource_change_after_gate_check_cannot_publish(tmp_path, monkeypatch):
    skills = store(tmp_path)
    resource = skills.load('alpha').path.parent / 'reference.txt'
    resource.write_text('validated')
    before = skills.load('alpha')
    original = before.path.read_bytes()
    async def propose(_):
        return {'action': 'apply', 'name': 'alpha', 'edits': [{'op': 'append', 'text': 'new'}]}
    async def gate(old, candidate):
        original_load = skills.load
        once = True
        def interleaved_load(*args, **kwargs):
            nonlocal once
            snapshot = original_load(*args, **kwargs)
            if once:
                once = False
                resource.write_text('not validated')
            return snapshot
        monkeypatch.setattr(skills, 'load', interleaved_load)
        return {'old': {'successes': 0, 'hard_failures': 0}, 'candidate': {'successes': 1, 'hard_failures': 0}}
    result = asyncio.run(EvolutionStore(skills).maintain({}, propose, authorized=True,
        target_snapshot=before, validation_gate=gate))
    assert result['status'] == 'rejected'
    assert before.path.read_bytes() == original

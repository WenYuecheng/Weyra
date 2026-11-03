import asyncio
import importlib.util
import json


def module():
    spec = importlib.util.find_spec('benchmarks.finish_evaluation')
    assert spec is not None, 'finish evaluation runner must exist'
    from weyra import finish_evaluation
    return finish_evaluation


def test_manifest_has_frozen_balanced_splits_and_fixed_denominators():
    m = module().load_manifest()
    assert len(m['localization_cases']) == 24
    assert sorted([sum(c['category'] == k for c in m['localization_cases']) for k in {c['category'] for c in m['localization_cases']}]) == [4] * 6
    assert len(module().planned_units(m, final_only=True)) == 144
    for family in m['families']:
        assert [sum(t['split'] == s for t in family['tasks']) for s in ('dev','validation','test')] == [4,4,4]
        assert len({t['id'] for t in family['tasks']}) == 12


def test_host_grader_checks_real_artifacts_and_original_integrity(tmp_path):
    m = module()
    case = m.load_manifest()['families'][0]['tasks'][0]
    m.materialize(case, tmp_path)
    expected = m.expected_output(case)
    (tmp_path/'result.json').write_text(json.dumps(expected))
    assert m.grade_artifacts(case, tmp_path)['success']
    (tmp_path/'input.json').write_text('{}')
    assert m.grade_artifacts(case, tmp_path)['hard_failures'] == 1


def test_resume_never_replays_interrupted_unit(tmp_path):
    module()
    from benchmarks.core_evaluation import EvaluationJournal
    journal = EvaluationJournal(tmp_path)
    journal.start('once', {'id':'x'})
    journal.close()
    journal = EvaluationJournal(tmp_path)
    assert journal.existing('once')['status'] == 'interrupted'
    journal.close()


def test_report_keeps_unexecuted_failures_in_denominator():
    m = module()
    report = m.fixed_report(m.load_manifest(), {})
    assert report['planned_final_units'] == 144
    assert report['groups']['localization/new']['n'] == 48
    assert report['groups']['skill_test/adapted']['n'] == 24
    assert report['groups']['skill_test/adapted']['success_rate'] == 0


def test_fixture_paths_cannot_escape_workspace(tmp_path):
    m = module()
    import pytest
    with pytest.raises(ValueError):
        m.materialize({'files':{'../escape':'bad'}}, tmp_path)


def test_normal_agent_uses_skill_and_real_file_tools(tmp_path):
    m = module()
    from weyra.contracts import ModelInfo, ModelResponse, ToolCall, Usage
    from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
    from weyra.runtime_state import Budget
    case = m.load_manifest()['families'][0]['tasks'][0]
    responses = [
        ModelResponse(tool_calls=(ToolCall('s','skill',{'name':'evidence'}),ToolCall('r','read_file',{'path':'input.json'})),usage=Usage(10,1)),
        ModelResponse(tool_calls=(ToolCall('w','write_file',{'path':'result.json','content':json.dumps(m.expected_output(case))}),),usage=Usage(10,1)),
        ModelResponse('done',usage=Usage(10,1)),
    ]
    registry=ProviderRegistry()
    provider=ScriptedProvider(responses)
    registry.register('test',provider)
    runtime=ModelRuntime(registry,{'main':ModelInfo('test','m',20000,1000,input_price_per_million='1',output_price_per_million='1')})
    runtime.pricing={'test/m':{'currency':'CNY','input_per_million':'1','output_per_million':'1'}}
    runner=m.FinishRunner(tmp_path/'run',runtime,Budget(cost_limit_cny='100'),ledger_root=tmp_path/'ledger')
    try:
        result=asyncio.run(runner.task(case,'Read input.json; write result.json',tmp_path/'task'))
        assert 'evidence' in provider.requests[0].system and 'aliases' in provider.requests[0].system
        assert result['status']=='pass'
        assert result['skill_loaded'] and result['file_read'] and result['file_write']
        assert runner.ledger.session_id=='finish-live-cny100'
    finally:
        runner.close()


def test_locator_override_is_local_and_new_has_no_manual_gold(tmp_path):
    m=module()
    m.write_skill(tmp_path,'a','common shared',aliases=['Alpha'])
    m.write_skill(tmp_path,'b','common shared')
    old=m.CohortSkillStore(tmp_path,'old')
    new=m.CohortSkillStore(tmp_path,'new')
    assert old.resolve_target('common shared',used_ids=['b']).source=='lexical_top1'
    assert new.resolve_target('common shared',used_ids=['b']).target.name=='b'
    assert new.resolutions[0]['explicit_id'] is None


def test_localization_validation_and_final_followup_are_separate():
    for c in module().load_manifest()['localization_cases']:
        assert c['validation']['files']!=c['followup']['files']
        assert c['validation']['id']!=c['followup']['id']


def test_unsupported_evidence_fabrication_is_a_hard_failure(tmp_path):
    m=module()
    case=m.load_manifest()['families'][0]['tasks'][0]
    m.materialize(case,tmp_path)
    answer=m.expected_output(case)
    answer[-1]['value']='fabricated'
    (tmp_path/'result.json').write_text(json.dumps(answer))
    assert m.grade_artifacts(case,tmp_path)['hard_failures']>0


def test_final_tasks_preserve_old_abilities_and_scope_exceptions():
    m=module()
    for f in m.load_manifest()['families']:
        tests=[t for t in f['tasks'] if t['split']=='test']
        assert [t['coverage'] for t in tests]==['new_requirement','old_ability','exception','mixed']
    f=next(f for f in m.load_manifest()['families'] if f['id']=='expression')
    case=next(t for t in f['tasks'] if t['split']=='test' and t['coverage']=='exception')
    assert 'It is important to note that ' in m.expected_output(case)[0]


def test_concurrent_unit_costs_remain_scoped(tmp_path):
    m=module()
    from weyra.runtime_state import Budget
    runner=m.FinishRunner(tmp_path/'run',object(),Budget(cost_limit_cny='100'),ledger_root=tmp_path/'ledger')
    async def run():
        async def op():
            key=m.UNIT_SCOPE.get()
            runner.budget.records.append({'scope':key,'cost_cny':'0.25'})
            await asyncio.sleep(0)
            return {'status':'pass'}
        return await asyncio.gather(*(runner.unit(k,{'id':k},op) for k in ('a','b')))
    try:
        results=asyncio.run(run())
        assert [r['cost_cny'] for r in results]==['0.25','0.25']
        assert [r['physical_requests'] for r in results]==[1,1]
    finally:
        runner.close()


def test_comparisons_only_include_pairs_with_both_sides_executed():
    m=module()
    manifest=m.load_manifest()
    assert all(x['paired_units']==0 for x in m.fixed_report(manifest,{})['comparisons'])
    planned=m.planned_units(manifest,final_only=True)
    old=next(k for k,v in planned.items() if v['group']=='localization' and v['variant']=='old')
    new=old.replace('/old/','/new/')
    assert m.fixed_report(manifest,{old:{'status':'pass'}})['comparisons'][0]['paired_units']==0
    assert m.fixed_report(manifest,{old:{'status':'pass'},new:{'status':'interrupted'}})['comparisons'][0]['paired_units']==0
    assert m.fixed_report(manifest,{old:{'status':'pass'},new:{'status':'error'}})['comparisons'][0]['paired_units']==1


def test_scoped_runtime_preserves_explicit_attempt_budget_api():
    import inspect
    assert 'attempt_budget' in inspect.signature(module().ScopedRuntime.complete).parameters


def test_scoped_runtime_forwards_physical_attempt_counter(tmp_path):
    import pytest
    m=module()
    from weyra.contracts import ModelInfo, Message, PreparedRequest
    from weyra.model_runtime import ModelError, ModelRuntime, ProviderRegistry, ScriptedProvider
    from weyra.runtime_state import Budget, RunContext
    from weyra.session import SessionStore
    provider=ScriptedProvider([ModelError('transient')]*5)
    registry=ProviderRegistry()
    registry.register('test',provider)
    model=ModelInfo('test','m',20000,1000)
    wrapped=m.ScopedRuntime(ModelRuntime(registry,{'main':model}))
    session=SessionStore(project=tmp_path)
    budget=Budget()
    context=RunContext(tmp_path,session,budget)
    attempt_budget={'attempts':0,'limit':1}
    request=PreparedRequest(model,'',(Message('user','test'),))
    async def check():
        with pytest.raises(ModelError):
            await wrapped.complete(request,context,attempt_budget=attempt_budget)
        assert attempt_budget['attempts']==1
        assert len(provider.requests)==1
        with pytest.raises(ModelError) as error:
            await wrapped.complete(request,context,attempt_budget=attempt_budget)
        assert error.value.kind=='attempt_limit'
        assert len(provider.requests)==1
        await context.close()
    try:
        asyncio.run(check())
    finally:
        session.close()

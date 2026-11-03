import pytest
from weyra import core_evaluation as core


def test_freeze_binds_code_data_and_config(tmp_path):
    p = tmp_path / 'freeze.json'
    assert core.freeze_protocol(p, {'manifest':'a','model':'m'})['manifest']=='a'
    assert core.freeze_protocol(p, {'manifest':'a','model':'m'})['model']=='m'
    with pytest.raises(ValueError, match='frozen'):
        core.freeze_protocol(p, {'manifest':'b','model':'m'})


def test_ledger_binding_restores_shared_budget_and_rejects_changes(tmp_path):
    from weyra.runtime_state import Budget
    output, ledger = tmp_path/'output', tmp_path/'ledger'
    runner = core.CoreRunner(output, None, Budget(cost_limit_cny='50'), ledger_root=ledger)
    runner.budget.total_cost_cny = __import__('decimal').Decimal('17')
    runner.budget._checkpoint()
    runner.journal.close()
    runner.ledger.close()
    restored = core.CoreRunner(output, None, Budget(cost_limit_cny='50'))
    assert restored.ledger.root == ledger.resolve()
    assert restored.budget.total_cost_cny == 17
    restored.journal.close()
    restored.ledger.close()
    with pytest.raises(ValueError, match='ledger'):
        core.CoreRunner(output, None, Budget(cost_limit_cny='50'), ledger_root=tmp_path/'other')


def test_protocol_binds_provider_backend_without_disclosing_credentials():
    from weyra.contracts import ModelInfo
    from weyra.model_runtime import ModelRuntime, ProviderRegistry
    from weyra.model_runtime import OpenAIProvider
    def runtime(endpoint):
        registry=ProviderRegistry()
        registry.register('p',OpenAIProvider(api_key_env='PRIVATE_KEY',base_url=endpoint))
        return ModelRuntime(registry,{'main':ModelInfo('p','m',10000,1000)})
    a=core.protocol(runtime('https://one.example/v1?key=private'),{})
    b=core.protocol(runtime('https://two.example/v1?key=private'),{})
    assert a != b
    encoded=__import__('json').dumps(a)
    assert 'private' not in encoded and 'secret-value' not in encoded


def test_paired_stats_cluster_repeats_and_keep_failed_denominator():
    rows=[]
    for family,good in [('f1',True),('f2',False)]:
        for repeat in range(3):
            rows += [{'id':family,'cluster':family,'variant':'old','repeat':repeat,'status':'fail'},
                     {'id':family,'cluster':family,'variant':'new','repeat':repeat,'status':'pass' if good else 'error'}]
    result=core.paired_comparison(rows,'old','new')
    assert result['paired_units']==6
    assert result['clusters']==2
    assert result['difference']==0.5
    assert result['only_new_success']==3
    assert result['ci95'][1]==1


def test_journal_resume_does_not_repeat_started_unit(tmp_path):
    journal=core.EvaluationJournal(tmp_path)
    journal.start('case/1',{'id':'x'})
    assert journal.existing('case/1')['status']=='interrupted'
    journal.finish('case/1',{'status':'fail','id':'x'})
    journal.close()
    journal=core.EvaluationJournal(tmp_path)
    assert journal.existing('case/1')['status']=='fail'
    journal.close()


def test_per_unit_accounting_does_not_include_other_concurrent_units(tmp_path):
    import asyncio
    from weyra.runtime_state import Budget
    async def check():
        runner=object.__new__(core.CoreRunner)
        runner.journal=core.EvaluationJournal(tmp_path)
        runner.budget=Budget()
        runner.stopped=False
        async def operation():
            a=await runner.budget.reserve(10,scope='one')
            b=await runner.budget.reserve(20,scope='other')
            await runner.budget.settle(a,10,actual_cost_cny='1')
            await runner.budget.settle(b,20,actual_cost_cny='2')
            return {'status':'pass'}
        result=await runner.unit('one',{'id':'one'},operation)
        assert result['cost_cny']=='1'
        assert result['physical_requests']==1
        runner.journal.close()
    asyncio.run(check())


def test_response_recording_keeps_unparseable_proposals(tmp_path):
    import asyncio
    from weyra.contracts import ModelInfo, ModelResponse, PreparedRequest, Message
    from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
    from weyra.runtime_state import Budget, RunContext
    from weyra.session import SessionStore
    async def check():
        reg=ProviderRegistry()
        reg.register('x',ScriptedProvider([ModelResponse('not-json')]))
        runtime=ModelRuntime(reg,{'main':ModelInfo('x','m',10000,1000)})
        core.instrument_runtime(runtime)
        store=SessionStore(project=tmp_path)
        ctx=RunContext(tmp_path,store,Budget())
        await runtime.complete(PreparedRequest(runtime.resolve(),'',(Message('user','proposal'),)),ctx)
        assert any(r['kind']=='evaluation_response' and r['data']['content']=='not-json' for r in store.records)
        store.close()
    asyncio.run(check())


def test_rejected_before_gate_has_no_fabricated_score():
    assert core.latest_gate([{'step':0,'result':{'status':'rejected'}}],0)=={}
    assert core.latest_gate([{'step':0,'gate':{'old':1}},{'step':0,'result':{}}],0)=={'old':1}


def test_context_units_parallel_bounded_and_resume_exactly_once(tmp_path, monkeypatch):
    import asyncio
    from weyra import core_context
    from weyra.runtime_state import Budget
    async def check():
        active=peak=0
        async def fake(case,variant,repeat,workspace,runtime,budget,**kwargs):
            nonlocal active,peak
            active+=1
            peak=max(peak,active)
            await asyncio.sleep(.01)
            active-=1
            return {'status':'ok','answer':'{"x":1}'}
        monkeypatch.setattr(core_context,'run_context_case',fake)
        runner=object.__new__(core.CoreRunner)
        runner.root=tmp_path
        runner.runtime=None
        runner.budget=Budget()
        runner.journal=core.EvaluationJournal(tmp_path)
        runner.repeats=1
        runner.stopped=False
        runner.manifest={'long_history_cases':[{'id':f'h-{i}','grading':{'expected':{'x':1}}} for i in range(2)]}
        await runner.compression()
        assert 1 < peak <=4
        assert len(runner.journal.results)==10
        await runner.compression()
        assert len(runner.journal.results)==10
        runner.journal.close()
    asyncio.run(check())

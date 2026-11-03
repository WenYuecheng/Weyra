"""Host-only finish reporting: fixed denominators, scope isolation and real ledger."""
import importlib
import json
from pathlib import Path
import sqlite3


def report_module():
    assert Path('scripts/reports/finish_report.py').exists(), 'finish reporter is missing'
    return importlib.import_module('scripts.reports.finish_report')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def ledger(project):
    path = project / '.weyra/state.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE events(seq INTEGER PRIMARY KEY, session_id TEXT, kind TEXT, data TEXT)')
        for session, cost in [('finish-live-cny100', '12.34'), ('unrelated-old', '999')]:
            state = {'calls': 17, 'total_tokens': 1234, 'unknown_calls': 2,
                     'total_cost_cny': cost, 'pending': {}}
            db.execute('INSERT INTO events(session_id,kind,data) VALUES(?,?,?)',
                       (session, 'budget_checkpoint', json.dumps(state)))
    return path


def test_missing_runs_keep_144_and_24_denominators(tmp_path):
    report = report_module()
    evidence = report.collect(tmp_path / 'absent-main', tmp_path / 'absent-context', tmp_path)
    assert evidence['main']['planned'] == 144
    assert evidence['main']['unexecuted'] == 144
    assert evidence['context']['planned'] == 24
    assert evidence['context']['unexecuted'] == 24
    assert not evidence['execution_complete']
    assert evidence['ledger']['available'] is False


def test_scope_metrics_pairs_and_root_cost_are_separate(tmp_path):
    report = report_module()
    main_plan, context_plan = report.plans()
    old_key = next(k for k, v in main_plan.items() if v['group'] == 'localization' and v['variant'] == 'old')
    new_key = old_key.replace('/old/', '/new/')
    skill_key = next(k for k, v in main_plan.items() if v['group'] == 'skill_test')
    rows = {
        old_key: {**main_plan[old_key], 'status': 'fail', 'resolution_success': True,
                  'followup_success': False, 'wrong_target': False, 'cost_cny': '1'},
        new_key: {**main_plan[new_key], 'status': 'pass', 'resolution_success': True,
                  'followup_success': True, 'wrong_target': False, 'cost_cny': '1'},
        skill_key: {**main_plan[skill_key], 'status': 'pass', 'cost_cny': '1'},
        'maintenance/not-final': {'status': 'pass', 'cost_cny': '900'},
        'old/foreign-success': {'status': 'pass', 'cost_cny': '900'},
    }
    write_json(tmp_path / 'formal/results.json', rows)
    context_rows = {}
    for index, case_id in enumerate(dict.fromkeys(v['id'] for v in context_plan.values())):
        if index > 1:
            break
        for variant in ('before', 'after'):
            key = f'{case_id}/{variant}/0'
            context_rows[key] = {**context_plan[key], 'status': 'timeout' if index == 1 and variant == 'after' else 'pass',
                'metrics': {'physical_requests': 4, 'cost_cny': '.2', 'facts_retained': 4,
                            'facts_total': 4, 'compaction_published': True}}
    write_json(tmp_path / 'context-v2/results.json', context_rows)
    write_json(tmp_path / 'context/results.json', {'contamination': {'status': 'pass'}})
    ledger(tmp_path)
    evidence = report.collect(tmp_path / 'formal', tmp_path / 'context-v2', tmp_path)
    assert evidence['ledger']['total_cost_cny'] == '12.34'
    assert evidence['main']['recorded'] == 3
    assert evidence['main']['localization']['old']['resolution_success'] == 1
    assert evidence['main']['localization']['old']['followup_success'] == 0
    assert evidence['main']['localization']['new']['pass'] == 1
    assert evidence['main']['localization_pairs']['completed_pairs'] == 1
    assert evidence['context']['pairs']['completed_pairs'] == 1
    assert evidence['context']['variants']['before']['physical_requests'] == 8
    assert evidence['context']['variants']['before']['facts_retained'] == 8
    assert len(evidence['main']['skills']) == 3


def test_render_outputs_chinese_and_keeps_incomplete_claims(tmp_path):
    report = report_module()
    path = ledger(tmp_path)
    before = path.read_bytes()
    outputs = report.render(tmp_path / 'formal', tmp_path / 'context-v2', tmp_path)
    assert path.read_bytes() == before
    text = Path(outputs['report']).read_text()
    resume = Path(outputs['resume']).read_text()
    assert '144' in text and '24' in text and '未执行' in text
    assert '552/822' in text and '20/20' in text
    assert '34/60→54/60' in text and '23/60→59/60' in text
    assert '不同信息量' in text and '保守预留' in text and '不是发票' in text
    assert '暂未完成' in resume
    assert '+08:00' in json.loads(Path(outputs['evidence']).read_text())['generated_at']


def test_started_rows_without_final_summary_do_not_claim_completion(tmp_path):
    report = report_module()
    main, context = report.plans()
    write_json(tmp_path/'formal/results.json', {k:{**v,'status':'interrupted'} for k,v in main.items()})
    write_json(tmp_path/'context-v2/results.json', {k:{**v,'status':'pass'} for k,v in context.items()})
    assert not report.collect(tmp_path/'formal',tmp_path/'context-v2',tmp_path)['execution_complete']


def test_maintenance_errors_are_explicit_process_evidence(tmp_path):
    report = report_module()
    write_json(tmp_path/'formal/results.json', {'maintenance/evidence/1': {
        'status':'error','reason':'ContextOverflow','id':'evidence','repeat':1}})
    evidence = report.collect(tmp_path/'formal',tmp_path/'context',tmp_path)
    assert evidence['main']['maintenance']['statuses']['error'] == 1
    assert evidence['main']['maintenance']['errors'][0]['reason'] == 'ContextOverflow'

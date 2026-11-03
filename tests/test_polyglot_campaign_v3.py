"""Read-only historical catalog audit and rebuildable campaign reports."""
import csv
import importlib
import json
from pathlib import Path

import pytest


def api():
    assert importlib.util.find_spec('benchmarks.polyglot.campaign_v3'), 'campaign module is missing'
    return importlib.import_module('benchmarks.polyglot.campaign_v3')


def fixture_catalog():
    tasks = [dict(id=f'{lang}/task-{i}', language=lang, retained=i < (16 if lang == 'python' else 13), evidence={'result_path': '/history/result.json'} if i < (16 if lang == 'python' else 13) else None) for lang in ('python', 'javascript') for i in range(32)]
    return dict(tasks=tasks, pending_ids=[t['id'] for t in tasks if not t['retained']], previous_old_cost_cny='9.5190181642435', previous_v2_cost_cny='5.8554306629531')


def test_report_keeps_64_denominator_and_unknown_counts(tmp_path):
    cat = fixture_catalog()
    rows = [dict(id='python/task-16', status='passed', cost_cny='.5', model_steps=None, physical_requests=4, rounds=[dict(index=0, verification={'verdict': 'fail'}), dict(index=1, verification={'verdict': 'pass'})]), dict(id='javascript/task-13', status='resource_exhausted', model_steps=3, physical_requests=None, rounds=[])]
    report = api().write_report(tmp_path, cat, rows, '.75')
    assert report['denominator'] == 64
    assert report['passed'] == 30
    assert report['newly_passed'] == 1
    assert report['rerun_denominator'] == 35
    assert report['repair_added_passed'] == 1
    assert report['languages']['python'] == {'denominator': 32, 'passed': 17}
    assert report['model_steps'] == {'known_sum': 3, 'unknown_tasks': 1}
    assert report['physical_requests'] == {'known_sum': 4, 'unknown_tasks': 1}
    assert report['cost_cny']['cumulative'] == '16.1244488271966'
    assert report['round_curve'][-1]['passed'] == 30
    assert json.loads((tmp_path/'campaign-report.json').read_text()) == report
    with (tmp_path/'results.csv').open() as stream:
        exported = list(csv.DictReader(stream))
    assert len(exported) == 64
    assert sum(row['status'] == 'retained_pass' for row in exported) == 29
    assert len(api().summarize(cat, [], '0')['tasks']) == 64


def test_report_rejects_duplicate_or_retained_rerun_rows():
    cat = fixture_catalog()
    with pytest.raises(ValueError, match='duplicate'):
        api().summarize(cat, [{'id': 'python/task-16'}, {'id': 'python/task-16'}], '0')
    with pytest.raises(ValueError, match='pending'):
        api().summarize(cat, [{'id': 'python/task-0'}], '0')


def test_actual_catalog_has_audited_retained_evidence():
    old = Path('.benchmarks/results/polyglot-40-20261023')
    new = Path('.benchmarks/results/polyglot-v2-20261023')
    if not old.exists() or not new.exists():
        pytest.skip('historical campaign artifacts are local')
    cat = api().build_catalog(old, new)
    assert len(cat['tasks']) == 64
    assert len(set(t['id'] for t in cat['tasks'])) == 64
    assert len(cat['retained']) == 29
    assert len(cat['pending_ids']) == 35
    assert cat['previous_old_cost_cny'] == '9.5190181642435'
    assert cat['previous_v2_cost_cny'] == '5.8554306629531'
    assert sum(t['retained'] for t in cat['tasks'] if t['language']=='python') == 16
    for task in cat['retained']:
        proof = task['evidence']
        assert Path(proof['result_path']).is_file()
        assert len(proof['result_sha256']) == 64
        assert proof['artifact_sha256']
        assert task['case_ids']


def test_retained_proof_requires_persisted_verification_agreement(tmp_path):
    module = api()
    folder = tmp_path/'tasks/python/example/first'
    (folder/'artifacts').mkdir(parents=True)
    (folder/'artifacts/example.py').write_text('answer = 1\n')
    import hashlib
    hashes = {'example.py': hashlib.sha256(b'answer = 1\n').hexdigest()}
    result = dict(artifact_sha256=hashes, verification={'verdict': 'pass'})
    (folder/'result.json').write_text(json.dumps(dict(result, verification={'verdict': 'fail'})))
    metadata = dict(id='python/example', editable=['example.py'])
    with pytest.raises(ValueError, match='evidence disagrees'):
        module._proof(tmp_path, metadata, 'first', result)


def test_report_refuses_historical_destination(tmp_path):
    cat = fixture_catalog()
    cat['historical_audit'] = {'old': {'protocol_path': str(tmp_path/'history/protocol.json')}}
    with pytest.raises(ValueError, match='historical'):
        api().write_report(tmp_path/'history', cat, [], '0')
    assert not (tmp_path/'history').exists()


def test_missing_verification_is_explicit_and_report_rebuilds(tmp_path):
    cat = fixture_catalog()
    rows = [dict(id='python/task-16', status='infrastructure_error', rounds=[{'verification': None}])]
    report = api().write_report(tmp_path, cat, rows, '0')
    assert report['unknown_verification_rounds'] == 1
    assert report['tasks'][16]['evidence_path'] == str(tmp_path/'tasks/python/task-16/result.json')
    again = api().write_report(tmp_path, cat, [], '0')
    assert again['status_counts']['pending'] == 35


def test_retained_proof_checks_stored_verifier_and_case_coverage(tmp_path):
    module = api()
    folder = tmp_path/'tasks/python/example'
    (folder/'artifacts').mkdir(parents=True)
    (folder/'verification').mkdir()
    (folder/'artifacts/example.py').write_text('answer = 1\n')
    import hashlib
    result = dict(verifier='pass', artifact_sha256={'example.py': hashlib.sha256(b'answer = 1\n').hexdigest()})
    (folder/'result.json').write_text(json.dumps(result))
    (folder/'verification/verification.json').write_text(json.dumps({'verdict': 'pass', 'stats': {'case_ids': ['case-a']}}))
    metadata = dict(id='python/example', editable=['example.py'], case_ids=['case-a', 'case-b'])
    with pytest.raises(ValueError, match='case coverage'):
        module._proof(tmp_path, metadata, 'old', result)

import copy
from benchmarks.core_corpus import load_core_manifest, public_task, load_public_cases
from benchmarks.core_scoring import grade_task

def test_counts_and_splits():
    m = load_core_manifest()
    assert len(m['localization_cases']) == 60
    assert len(m['long_history_cases']) == 12
    assert len(load_public_cases()) == 12
    for f in m['evolution_families']:
        assert [sum(t['split']==s for t in f['tasks']) for s in ['train','validation','test']] == [4,2,3]

def test_host_grading_never_public():
    m = load_core_manifest()
    for case in m['localization_cases'] + m['long_history_cases'] + load_public_cases():
        p = public_task(case)
        assert 'grading' not in p and 'expected' not in p
        assert set(p) <= {'id','type','split','input','family','group_id'}

def test_grader_checks_values_and_rejects_embedded_contract():
    case = load_core_manifest()['evolution_families'][0]['tasks'][0]
    expected = copy.deepcopy(case['grading']['expected'])
    assert grade_task(case, expected)['success']
    assert not grade_task(case, {'grading':case['grading']})['success']
    expected[next(iter(expected))] = 'incorrect'
    assert not grade_task(case, expected)['success']

def test_malformed_is_failure():
    case=load_core_manifest()['long_history_cases'][0]
    assert not grade_task(case,'not json')['success']

def test_balanced_localization_and_unique_groups():
    from collections import Counter
    manifest=load_core_manifest()
    counts=Counter(c['id'].split('-')[1] for c in manifest['localization_cases'])
    assert set(counts.values()) == {10}
    groups=[]
    for family in manifest['evolution_families']:
        groups.extend(t['group_id'] for t in family['tasks'])
        assert family['initial_body'] and family['durable_feedback']
        for task in family['tasks']:
            assert 'grading' not in public_task(task)
            assert grade_task(task,task['grading']['expected'])['success']
    assert len(groups)==len(set(groups))

def test_public_audit_provenance_and_no_artificial_history():
    import json
    from pathlib import Path
    audit=json.loads((Path(__file__).resolve().parents[1]/'benchmarks/data/core-public-audit-v1.json').read_text())
    selected=[r for r in audit['sources'] if r['selected']]
    assert len(selected)==12
    assert {r['dataset'] for r in audit['sources']} == {'GAIA','HLE'}
    assert all(len(r['record_sha256'])==64 and r['reason'] for r in audit['sources'])
    for case in load_public_cases():
        assert set(public_task(case)['input']) == {'query'}
        assert any(r['source_id']==case['source']['source_id'] and r['record_sha256']==case['source']['record_sha256'] for r in selected)

def test_public_nonexact_is_pending_not_success():
    case=load_public_cases()[0]
    grade=grade_task(case,'a plausible but nonidentical answer')
    assert not grade['success'] and grade['pending_review']
    assert not grade_task(case,'')['pending_review']
    assert grade_task(case,case['grading']['expected'])['success']
    assert not grade_task(case,case['grading']['expected'])['pending_review']

def test_context_has_real_tool_groups_and_balanced_controls():
    cases=load_core_manifest()['long_history_cases']
    assert sum(c['trigger']=='fixed' for c in cases)==6
    assert sum(c['context_regime']=='fit' for c in cases)==6
    for case in cases:
        history=case['input']['history']
        for index,msg in enumerate(history):
            if msg['role']=='tool':
                assert len(msg['content'])>8192
                assert history[index-1]['tool_calls'][0]['id']==msg['tool_call_id']
                assert isinstance(history[index-1]['tool_calls'][0]['arguments'],dict)

def test_context_regime_lengths_match_material():
    for case in load_core_manifest()['long_history_cases']:
        size=sum(len(m['content']) for m in case['input']['history'])
        if case['context_regime']=='fit':
            assert 32000 <= size < 45000
        else:
            assert size >= 100000
        points=case['input']['fold_points']
        assert all(0 < p <= len(case['input']['history']) for p in points)
        if 'multi_compression' in case['id']:
            assert len(points)>=2

import importlib.util
import json
from pathlib import Path

import pytest


def load_renderer(monkeypatch):
    examples=Path(__file__).parents[1]/'scripts'/'reports'
    monkeypatch.syspath_prepend(str(examples))
    spec=importlib.util.spec_from_file_location('core_report',examples/'core_report.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_requires_closed_snapshot_and_versions_audits(tmp_path,monkeypatch):
    module=load_renderer(monkeypatch)
    root=tmp_path/'run'
    data=root/'frozen-source/data'
    data.mkdir(parents=True)
    case={'id':'h','input':{'output_contract':{'diagnosis':{'type':'str'}}},
          'grading':{'expected':{'diagnosis':'reference wording'}}}
    (data/'core-eval-v1.json').write_text(json.dumps({'long_history_cases':[case],
        'public_cases':[],'evolution_families':[],'localization_cases':[]}))
    (root/'protocol.json').write_text('{}')
    result={'group':'compression','id':'h','cluster':'h','variant':'none','repeat':0,
            'status':'fail','answer':'{"diagnosis":"another wording"}',
            'grade':{'success':False,'hard_failures':1,'pending_review':False}}
    rows={'compression/h/none/0':result}
    (root/'results.json').write_text(json.dumps(rows))
    summary={'recorded_units':1,'execution_closed':False,'comparisons':[
        {'group':'compression','old':'none','new':'dsh_checkpoint','paired_units':0,
         'clusters':0,'difference':None,'ci95':None}]}
    (root/'summary.json').write_text(json.dumps(summary))
    dest=tmp_path/'report.md'
    with pytest.raises(ValueError,match='closed'):
        module.render(root,dest)
    summary['execution_closed']=True
    (root/'summary.json').write_text(json.dumps(summary))
    module.render(root,dest)
    paired=json.loads((tmp_path/'core-paired-details.json').read_text())['rows']
    assert len(paired)==3
    assert paired[0]['old']['status']=='fail' and paired[0]['new']['status']=='unstarted'
    assert paired[0]['confirmed_success_difference'] is None
    first=next(tmp_path.glob('core-scoring-validity-audit-*.json')).read_bytes()
    rows['compression/h/none/1']={**result,'repeat':1}
    (root/'results.json').write_text(json.dumps(rows))
    summary['recorded_units']=2
    (root/'summary.json').write_text(json.dumps(summary))
    module.render(root,dest)
    assert len(list(tmp_path.glob('core-scoring-validity-audit-*.json')))==2
    assert any(p.read_bytes()==first for p in tmp_path.glob('core-scoring-validity-audit-*.json'))
    assert (tmp_path/'core-evaluation-results.json').read_bytes()==(root/'results.json').read_bytes()

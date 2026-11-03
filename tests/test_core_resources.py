import importlib.util
import json
from pathlib import Path
import sqlite3


def test_resources_attribute_nested_test_calls_once_and_preserve_unknown(tmp_path):
    spec = importlib.util.spec_from_file_location('core_resources', Path(__file__).parents[1] / 'scripts/reports/core_resources.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / 'run'
    parent = root / 'units/family-experience_bounded-0'
    child = parent / 'test/task'
    rows = {
        'maint': {'group': 'maintenance', 'id': 'family', 'variant': 'experience_bounded', 'repeat': 0},
        'test': {'group': 'skill_test', 'id': 'task', 'cluster': 'family',
                 'variant': 'experience_bounded', 'repeat': 0, 'workspace': str(child)},
    }
    root.mkdir(parents=True)
    (root / 'results.json').write_text(json.dumps(rows))
    for folder, record in [(parent, {'request_id': 'a', 'attempt_id': '1', 'input_tokens': 10, 'output_tokens': 2}),
                           (child, {'request_id': 'b', 'attempt_id': '2', 'input_tokens': None, 'output_tokens': None})]:
        path = folder / '.weyra/state.db'
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('create table events (kind text, data text)')
            db.executemany('insert into events values (?,?)', [('model_usage', json.dumps(record))] * 2)
    result = module.collect(root)
    assert result['units']['maint']['input_tokens'] == 10
    assert result['units']['maint']['usage_records'] == 1
    assert result['units']['test']['unknown_usage_records'] == 1
    assert result['groups']['skill_test/experience_bounded']['usage_records'] == 1
    assert not result['unassigned']

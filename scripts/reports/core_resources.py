"""Read-only resource accounting over a frozen cohort's local model traces."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sqlite3


def unit_folder(root, row):
    group, name, arm, repeat = (row[k] for k in ('group', 'id', 'variant', 'repeat'))
    if group == 'localization':
        return None
    if row.get('workspace'):
        return Path(row['workspace'])
    if group == 'skill_test' and arm not in ('no_skill', 'frozen_old'):
        return root / 'units' / f"{row['cluster']}-{arm}-{repeat}" / 'test' / name
    return root / 'units' / f'{name}-{arm}-{repeat}'


def collect(root, rows=None):
    root = Path(root).resolve()
    if rows is None:
        rows = json.loads((root / 'results.json').read_text())
    prefixes = sorted(((unit_folder(root, r), k) for k, r in rows.items()
                       if r['group'] != 'localization'),
                      key=lambda pair: len(pair[0].parts), reverse=True)
    units = defaultdict(lambda: {'usage_records': 0, 'input_tokens': 0, 'output_tokens': 0,
                                'accounted_tokens': 0, 'unknown_usage_records': 0,
                                'provider_duration_seconds': 0.0})
    seen = set()
    unassigned = []
    for path in sorted((root / 'units').rglob('state.db')):
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            records = db.execute("select data from events where kind='model_usage'").fetchall()
        for (raw,) in records:
            record = json.loads(raw)
            identity = (record.get('request_id'), record.get('attempt_id'))
            if identity in seen:
                continue
            seen.add(identity)
            key = next((k for prefix, k in prefixes if path.is_relative_to(prefix)), None)
            if key is None:
                unassigned.append({'database': str(path), 'request_id': identity[0]})
                continue
            value = units[key]
            value['usage_records'] += 1
            for field in ('input_tokens', 'output_tokens', 'accounted_tokens'):
                value[field] += record.get(field) or 0
            value['unknown_usage_records'] += record.get('input_tokens') is None or record.get('output_tokens') is None
            value['provider_duration_seconds'] += record.get('request_duration_seconds') or 0
    groups = defaultdict(lambda: defaultdict(float))
    for key, value in units.items():
        row = rows[key]
        for field, number in value.items():
            groups[row['group'] + '/' + row['variant']][field] += number
    return {'policy': 'Read-only, deduplicated by request and attempt. Missing usage is unknown, not zero cost. '
                      'Longest workspace prefix prevents test calls being assigned to maintenance. '
                      'Provider durations may overlap; they are not wall-clock duration.',
            'root': str(root), 'units': dict(units), 'groups': dict(groups),
            'unassigned': unassigned}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(collect(args.root), ensure_ascii=False, indent=2) + '\n')

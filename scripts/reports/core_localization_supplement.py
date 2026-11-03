"""独立、无模型的冻结定位补充评测；已有结果永不覆盖。"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def load_frozen_manifest(root=ROOT):
    root = Path(root)
    freeze = json.loads((root / 'wiki/core-localization-v2-freeze.json').read_text())
    # Fixed local path: the freeze record cannot redirect reads outside the repo.
    path = root / 'benchmarks/data/core-localization-v2.json'
    if hashlib.sha256(path.read_bytes()).hexdigest() != freeze['sha256']:
        raise ValueError('Frozen manifest hash mismatch')
    manifest = json.loads(path.read_text())
    inputs = {json.dumps(c['input'], sort_keys=True) for c in manifest['cases']}
    counts = Counter(c['category'] for c in manifest['cases'])
    if len(inputs) != 60 or len(manifest['cases']) != 60 or len(counts) != 6 or set(counts.values()) != {10}:
        raise ValueError('Frozen corpus must have 60 unique inputs and six balanced categories')
    return manifest, freeze


def execute(root=ROOT):
    root = Path(root).resolve()
    manifest, freeze = load_frozen_manifest(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from weyra.skills import SkillStore

    rows = []
    with tempfile.TemporaryDirectory(prefix='weyra-localization-v2-') as tmp:
        for skill in manifest['skill_library']:
            name = skill['name']
            if Path(name).name != name or name in ('.', '..'):
                raise ValueError('Invalid fixture skill name')
            path = Path(tmp) / '.weyra/skills' / name / 'SKILL.md'
            path.parent.mkdir(parents=True)
            metadata = {k: v for k, v in skill.items() if k != 'body'}
            path.write_text('---\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False)
                            for k, v in metadata.items()) + '\n---\n' + skill['body'])
        store = SkillStore(tmp, [])
        catalog = store.capture_catalog()
        for case in manifest['cases']:
            inputs = case['input']
            old = store.search(inputs['query'], catalog=catalog)
            new = store.resolve_target(inputs['query'], explicit_id=inputs.get('explicit_id'),
                                       used_ids=inputs.get('used_ids', ()), catalog=catalog)
            old_name = old[0].name if old else None
            new_name = new.target.name if new.target else None
            rows.append({'id': case['id'], 'category': case['category'],
                         'expected_target': case['expected_target'], 'old_selected': old_name,
                         'new_selected': new_name, 'old_success': old_name == case['expected_target'],
                         'new_success': new_name == case['expected_target'],
                         'new_source': new.source, 'new_reason': new.reason})
    by_category = {}
    for category in sorted({r['category'] for r in rows}):
        subset = [r for r in rows if r['category'] == category]
        by_category[category] = {'cases': len(subset),
                                'old_successes': sum(r['old_success'] for r in subset),
                                'new_successes': sum(r['new_success'] for r in subset)}
    return {'version': 'core-localization-supplement-v2',
            'manifest_sha256': freeze['sha256'],
            'code_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                            for name in ('weyra/skills.py', 'weyra/frontmatter.py',
                                         'scripts/reports/core_localization_supplement.py')},
            'protocol': '独立补充复跑。先核验冻结输入；旧版使用query，新版另使用已给定explicit_id/used_ids。无模型调用；不替换v1或既有补充结果。',
            'counts': {'rows': len(rows), 'unique_inputs': len(rows),
                       'old_successes': sum(r['old_success'] for r in rows),
                       'new_successes': sum(r['new_success'] for r in rows)},
            'by_category': by_category, 'results': rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=ROOT / '.benchmarks/results/core-localization-supplement-v2-rerun.json',
                        help='新结果文件路径；文件已存在时拒绝执行')
    args = parser.parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve exclusively before executing either variant; never overwrite results.
    with args.output.open('x', encoding='utf-8') as handle:
        artifact = execute()
        json.dump(artifact, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps(artifact['counts'], ensure_ascii=False))


if __name__ == '__main__':
    main()

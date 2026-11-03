"""Read-only historical evidence catalog and rebuildable 64-task reports.

Catalog metadata contains hashes and evidence paths, never verifier or gold text.
The current upstream task projection must be used when staging pending work.
"""
from collections import Counter
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from .dataset import discover
from .sandbox import safe_path


def _read(path):
    return json.loads(Path(path).read_text())


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _money(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError('cost must be finite and nonnegative')
    return number


def _cost(summary):
    return sum((_money(v) for v in summary['cost_cny'].values()), Decimal(0))


def _task_path(root, task_id):
    return root / 'tasks' / safe_path(task_id)


def _proof(root, metadata, stage, result):
    folder = _task_path(root, metadata['id'])
    if stage != 'old':
        folder /= stage
    result_path = folder / 'result.json'
    persisted = _read(result_path)
    persisted_pass = (persisted.get('verifier') == 'pass' if stage == 'old'
                      else (persisted.get('verification') or {}).get('verdict') == 'pass')
    if not persisted_pass or persisted.get('artifact_sha256') != result.get('artifact_sha256'):
        raise ValueError(f"retained evidence disagrees: {metadata['id']}")
    verification_path = folder / 'verification' / 'verification.json'
    verification = _read(verification_path)
    if verification.get('verdict') != 'pass':
        raise ValueError(f"retained verifier disagrees: {metadata['id']}")
    if sorted((verification.get('stats') or {}).get('case_ids', [])) != sorted(metadata.get('case_ids', [])):
        raise ValueError(f"retained case coverage disagrees: {metadata['id']}")
    hashes = result.get('artifact_sha256', {})
    if set(hashes) != set(metadata['editable']):
        raise ValueError(f"incomplete retained artifact hashes: {metadata['id']}")
    artifact_paths = {}
    for name, expected in hashes.items():
        artifact = folder / 'artifacts' / safe_path(name)
        if _sha(artifact) != expected:
            raise ValueError(f'retained artifact changed: {artifact}')
        artifact_paths[name] = str(artifact)
    return dict(source=str(root), final_passing_stage=stage,
                result_path=str(result_path), result_sha256=_sha(result_path),
                artifact_sha256=hashes, artifact_paths=artifact_paths,
                verification_path=str(verification_path), verification_sha256=_sha(verification_path))


def build_catalog(old_root, new_root):
    """Audit the two frozen protocols and retain independently evidenced passes."""
    old_root, new_root = Path(old_root).resolve(), Path(new_root).resolve()
    upstream = old_root / 'upstream' / 'polyglot'
    current = {task.id: task for task in discover(upstream)}
    tasks, origins, audit = [], {}, {}
    for origin, root in [('old', old_root), ('v2', new_root)]:
        protocol_path = root / 'protocol.json'
        protocol = _read(protocol_path)
        summary = _read(root / 'summary.json')
        audit[origin] = dict(protocol_path=str(protocol_path), protocol_sha256=_sha(protocol_path),
                             summary_path=str(root / 'summary.json'), summary_sha256=_sha(root / 'summary.json'),
                             cost_cny=str(_cost(summary)))
        if origin == 'v2' and summary.get('total_added_cost_cny') is not None:
            if _money(summary['total_added_cost_cny']) != _cost(summary):
                raise ValueError('historical v2 cost summary disagrees')
        pass_count = 0
        for historical in protocol['tasks']:
            task_id = historical['id']
            if task_id in origins:
                raise ValueError(f'duplicate historical task: {task_id}')
            origins[task_id] = origin
            task = current[task_id]
            metadata = dict(task.metadata(), case_ids=protocol['reference_cases'][task_id])
            if metadata['verifier_sha256'] != historical['verifier_sha256']:
                raise ValueError(f'upstream verifier changed: {task_id}')
            result = _read(_task_path(root, task_id) / 'result.json')
            stage, passed = 'old', result.get('verifier') == 'pass'
            selected = result
            if origin == 'v2':
                stage = 'repair' if result.get('repair') is not None else 'first'
                selected = result.get(stage) or {}
                passed = (selected.get('verification') or {}).get('verdict') == 'pass'
            evidence = _proof(root, metadata, stage, selected) if passed else None
            pass_count += passed
            tasks.append(dict(metadata, origin=origin, retained=passed, evidence=evidence,
                              case_ids=protocol['reference_cases'][task_id],
                              historical_agent_files_sha256=historical['agent_files_sha256']))
        expected = summary['passed'] if origin == 'old' else summary['final_passed']
        if pass_count != expected:
            raise ValueError(f'{origin} result passes disagree with summary')
    if len(tasks) != 64 or Counter(t['language'] for t in tasks) != {'python': 32, 'javascript': 32}:
        raise ValueError('historical protocols must contain 64 unique tasks, 32 per language')
    retained = [task for task in tasks if task['retained']]
    if len(retained) != 29:
        raise ValueError('historical retained pass count must be 29')
    return dict(version='weyra-polyglot-campaign-v3', tasks=tasks, retained=retained,
                pending_ids=[t['id'] for t in tasks if not t['retained']],
                reference_cases={t['id']: t['case_ids'] for t in tasks},
                upstream_root=str(upstream), historical_audit=audit,
                previous_old_cost_cny=audit['old']['cost_cny'],
                previous_v2_cost_cny=audit['v2']['cost_cny'])


def _counts(rows, key):
    return dict(known_sum=sum(row[key] for row in rows if row.get(key) is not None),
                unknown_tasks=sum(row.get(key) is None for row in rows))


def _verdict(round_row):
    return (round_row.get('verification') or {}).get('verdict')


def summarize(catalog, rerun_rows, spend_cny):
    """Recompute the cumulative solved rate, retaining all 64 planned tasks."""
    rows = list(rerun_rows)
    by_id = {}
    pending = set(catalog['pending_ids'])
    for row in rows:
        if row['id'] in by_id:
            raise ValueError(f"duplicate rerun task: {row['id']}")
        if row['id'] not in pending:
            raise ValueError(f"rerun task is not pending: {row['id']}")
        by_id[row['id']] = row
    retained = sum(task['retained'] for task in catalog['tasks'])
    newly = sum(row.get('status') == 'passed' for row in rows)
    tasks = []
    for metadata in catalog['tasks']:
        row = by_id.get(metadata['id'])
        proof = metadata.get('evidence') or {}
        tasks.append(dict(id=metadata['id'], language=metadata['language'],
                          status='retained_pass' if metadata['retained'] else (row or {}).get('status', 'pending'),
                          evidence_path=proof.get('result_path', (row or {}).get('result_path')),
                          rounds=len((row or {}).get('rounds') or []),
                          cost_cny='0' if metadata['retained'] else (row or {}).get('cost_cny'),
                          model_steps=(row or {}).get('model_steps'),
                          physical_requests=(row or {}).get('physical_requests')))
    languages = {lang: dict(denominator=sum(t['language'] == lang for t in tasks),
                            passed=sum(t['language'] == lang and t['status'] in ('passed', 'retained_pass') for t in tasks))
                 for lang in ('python', 'javascript')}
    round_count = max((len(row.get('rounds') or []) for row in rows), default=0)
    curve = []
    for index in range(round_count):
        solved = sum(any(_verdict(r) == 'pass' for r in (row.get('rounds') or [])[:index+1]) for row in rows)
        curve.append(dict(round=index, newly_passed=solved, passed=retained+solved,
                          cumulative_solved_rate=(retained+solved)/len(tasks)))
    prior = _money(catalog['previous_old_cost_cny']) + _money(catalog['previous_v2_cost_cny'])
    spend = _money(spend_cny)
    return dict(denominator=len(tasks), retained_passed=retained, newly_passed=newly,
                rerun_denominator=len(pending), newly_passed_rate=newly/len(pending) if pending else 0,
                passed=retained+newly, cumulative_solved_rate=(retained+newly)/len(tasks),
                repair_added_passed=sum(row.get('status') == 'passed' and bool(row.get('rounds')) and _verdict(row['rounds'][0]) == 'fail' for row in rows),
                languages=languages, status_counts=dict(Counter(t['status'] for t in tasks)),
                model_steps=_counts(rows, 'model_steps'), physical_requests=_counts(rows, 'physical_requests'),
                unknown_verification_rounds=sum(_verdict(r) is None for row in rows for r in (row.get('rounds') or [])),
                cost_cny=dict(previous_old=catalog['previous_old_cost_cny'], previous_v2=catalog['previous_v2_cost_cny'],
                              previous_total=str(prior), current=str(spend), cumulative=str(prior+spend)),
                cost_basis='conservative_ceiling_not_provider_invoice', round_curve=curve, tasks=tasks)


def write_report(root, catalog, rerun_rows, spend_cny):
    """Write reports only to the new campaign root; safe to rebuild at any time."""
    root = Path(root).resolve()
    for source in catalog.get('historical_audit', {}).values():
        historical = Path(source['protocol_path']).parent.resolve()
        if root == historical or historical in root.parents:
            raise ValueError('report destination must not modify historical results')
    report = summarize(catalog, rerun_rows, spend_cny)
    for row in report['tasks']:
        if row['status'] not in ('retained_pass', 'pending') and not row['evidence_path']:
            row['evidence_path'] = str(_task_path(root, row['id']) / 'result.json')
    root.mkdir(parents=True, exist_ok=True)
    target = root / 'campaign-report.json'
    temporary = root / '.campaign-report.json.tmp'
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(target)
    temporary = root / '.results.csv.tmp'
    with temporary.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(report['tasks'][0]))
        writer.writeheader()
        writer.writerows(report['tasks'])
    temporary.replace(root / 'results.csv')
    return report

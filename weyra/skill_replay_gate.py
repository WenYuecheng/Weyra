"""Bounded replay execution, immutable-key output reuse, and validation decisions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from .frontmatter import reject_symlinks

EXECUTOR_VERSION = "bounded-replay-v1.2"
MAX_CACHE_ENTRIES = 64
MAX_CACHE_BYTES = 2_000_000


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=lambda o: vars(o) if hasattr(o, '__dict__') else str(o)).encode()).hexdigest()


def _provider_identity(runtime):
    providers = getattr(getattr(runtime, "registry", None), "providers", {})
    result = {}
    for name, provider in providers.items():
        endpoint = getattr(provider, "base_url", None)
        if endpoint is None:
            endpoint = getattr(getattr(provider, "client", None), "base_url", None)
        safe_endpoint = None
        if endpoint:
            try:
                url = urlsplit(str(endpoint))
                # Configured endpoints normally forbid credentials/query. Strip them
                # defensively; the fingerprint never records secret values.
                authority = url.hostname or ""
                if url.port:
                    authority += ":" + str(url.port)
                safe_endpoint = urlunsplit((url.scheme, authority, url.path, "", ""))
            except ValueError:
                safe_endpoint = "invalid_endpoint"
        result[name] = {"implementation": type(provider).__module__ + "." + type(provider).__qualname__,
                        "endpoint": safe_endpoint}
    return result


def _key(gate, snapshot, case):
    runtime = gate.runtime
    return _digest({"executor": EXECUTOR_VERSION, "skill": snapshot.content_hash,
                    "context": getattr(snapshot, "context", ""), "name": snapshot.name, "path": str(getattr(snapshot, "path", "")), "body": snapshot.body, "resources": dict(getattr(snapshot, "resources", {})),
                    "case": case, "routes": getattr(runtime, "routes", {}),
                    "parameters": getattr(runtime, "route_parameters", {}),
                    "fallbacks": getattr(runtime, "fallbacks", {}),
                    "fallback_parameters": sorted((str(k), v) for k, v in getattr(runtime, "fallback_parameters", {}).items()),
                    "providers": _provider_identity(runtime)})


def _cache(gate, name):
    session = gate.session or getattr(gate.budget, '_journal', None)
    project = getattr(session, 'project_path', None)
    path = None
    if project:
        path = Path(project) / '.weyra' / 'internal' / 'skill-replay-cache' / (_digest(name) + '.json')
    if path:
        reject_symlinks(path)
    memory = getattr(gate, '_replay_cache', {})
    entries = memory.get(name, {})
    if path and path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_CACHE_BYTES:
        try:
            value = json.loads(path.read_text())
            if isinstance(value, dict) and value.get('version') == EXECUTOR_VERSION and isinstance(value.get('entries'), dict):
                entries = value['entries']
        except (ValueError, OSError):
            pass
    memory[name] = entries
    gate._replay_cache = memory
    return path, entries


def _save(path, entries):
    while len(entries) > MAX_CACHE_ENTRIES:
        entries.pop(next(iter(entries)))
    data = json.dumps({'version': EXECUTOR_VERSION, 'entries': entries}, ensure_ascii=False)
    while len(data.encode()) > MAX_CACHE_BYTES and entries:
        entries.pop(next(iter(entries)))
        data = json.dumps({'version': EXECUTOR_VERSION, 'entries': entries}, ensure_ascii=False)
    if path:
        reject_symlinks(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Replay cache cannot use symlinks')
        import tempfile
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.replay-')
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(data)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def saturated(gate, old, window):
    if window.reason or not window.validation:
        return False
    _, entries = _cache(gate, old.name)
    from .skill_judge import _manifest_cases
    try:
        validation = _manifest_cases({"cases": window.validation})
    except ValueError:
        return False
    for case in validation:
        row = entries.get(_key(gate, old, case), {})
        if not (row.get('result', {}).get('passed') and row.get('result', {}).get('hard_failures') == 0
                and row.get('score') == 4 and row.get('consistent') is True):
            return False
    return True


async def evaluate(gate, old, candidate, window, context=None):
    report = {'old': {'successes': 0, 'hard_failures': 0},
              'candidate': {'successes': 0, 'hard_failures': 0}, 'model_approved': False,
              'validation_policy': 'bounded-replay-v1', 'window_version': window.version,
              'reason': window.reason, 'per_case': [], 'cache': {'hits': 0, 'misses': 0}}
    cases = window.cases
    if window.reason or not window.validation or len(cases) > 4:
        report['reason'] = window.reason or 'insufficient_independent_validation'
        return report
    from .skill_judge import _manifest_cases
    try:
        cases = _manifest_cases({'cases': cases})
        if len({case['id'] for case in cases}) != len(cases):
            raise ValueError('Duplicate replay case ID')
    except ValueError:
        report['reason'] = 'unreplayable_window'
        return report
    path, entries = _cache(gate, old.name)
    outputs = {'old': {}, 'candidate': {}}
    keys = {}
    for case in cases:
        for arm, snapshot in (('old', old), ('candidate', candidate)):
            key = _key(gate, snapshot, case)
            keys[arm, case['id']] = key
            cached = entries.get(key, {}).get('result')
            if isinstance(cached, dict) and all(k in cached for k in ('passed', 'hard_failures', 'final', 'artifacts')):
                result = cached
                report['cache']['hits'] += 1
            else:
                report['cache']['misses'] += 1
                try:
                    result = await gate._execute(snapshot, case, context=context)
                except Exception as exc:
                    from .core.state import BudgetExceeded
                    if isinstance(exc, BudgetExceeded):
                        raise
                    result = {'final': '', 'artifacts': {}, 'hard_checks': [], 'hard_failures': 1,
                              'passed': False, 'error': type(exc).__name__}
                entries[key] = {'result': result}
            outputs[arm][case['id']] = result
            if result['hard_failures']:
                report['reason'] = arm + '_execution_hard_failure'
                if case['split'] == 'validation':
                    report[arm]['hard_failures'] += result['hard_failures']
                report['per_case'].append({'id': case['id'], 'role': case['split'], 'split': case['split'],
                                           **{a: values[case['id']] for a, values in outputs.items() if case['id'] in values}})
                _save(path, entries)
                return report
    # Never spend judge calls after a hard execution failure.
    hard_failed = any(row['hard_failures'] for arm in outputs.values() for row in arm.values())
    judgments = {} if hard_failed else await gate.judge.compare_many(cases, outputs['old'], outputs['candidate'], context=context)
    regression = hard_failed
    consistent = not hard_failed
    for case in cases:
        cid = case['id']
        judgment = judgments.get(cid, {'scores': {'old': 0, 'candidate': 0}, 'preferred': 'inconsistent'})
        success = {}
        for arm in outputs:
            result = outputs[arm][cid]
            score = judgment['scores'][arm]
            success[arm] = result['passed'] and score == 4 and judgment['preferred'] != 'inconsistent'
            entries[keys[arm, cid]].update(score=score, consistent=judgment['preferred'] != 'inconsistent')
            if case['split'] == 'validation':
                report[arm]['successes'] += int(success[arm])
                report[arm]['hard_failures'] += result['hard_failures']
        regression |= success['old'] and not success['candidate']
        regression |= judgment['scores']['candidate'] < judgment['scores']['old']
        regression |= outputs['candidate'][cid]['hard_failures'] > outputs['old'][cid]['hard_failures']
        consistent &= judgment['preferred'] != 'inconsistent'
        report['per_case'].append({'id': cid, 'role': case['split'], 'split': case['split'],
                                   'old': outputs['old'][cid], 'candidate': outputs['candidate'][cid], 'judge': judgment})
    report['model_approved'] = bool(consistent and not regression and report['candidate']['successes'] > report['old']['successes'])
    report['reason'] = 'approved' if report['model_approved'] else 'hard_failure' if hard_failed else 'regression' if regression else 'no_validation_gain'
    _save(path, entries)
    return report

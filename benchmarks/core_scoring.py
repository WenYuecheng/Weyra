"""Deterministic checks over parsed task outputs, with no execution of model text."""
from __future__ import annotations
import json
import re
import copy
from pathlib import Path

def _strict_equal(actual, expected):
    """Compare JSON-shaped values recursively without Python numeric coercion."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        actual_keys = {(type(key), key) for key in actual}
        expected_keys = {(type(key), key) for key in expected}
        return actual_keys == expected_keys and all(
            _strict_equal(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(
            _strict_equal(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def grade_task(case: dict, answer, workspace=None) -> dict:
    grading = case.get('grading', {})
    checks = {}
    if grading.get('mode') == 'public_exact':
        def normalize(value):
            return re.sub(r'\s+', ' ', str(value).strip()).casefold()
        checks['reference_answer'] = normalize(answer) == normalize(grading['expected'])
    else:
        if isinstance(answer, str):
            try:
                answer = json.loads(answer)
            except (ValueError, TypeError):
                answer = None
        checks['structured_object'] = isinstance(answer, dict)
        expected = grading.get('expected', {})
        for key, value in expected.items():
            checks['field:' + key] = isinstance(answer, dict) and key in answer and _strict_equal(answer[key], value)
        checks['no_extra_fields'] = isinstance(answer, dict) and set(answer) == set(expected)
    failed = sum(not passed for passed in checks.values())
    disputed = []
    if grading.get('mode') != 'public_exact' and isinstance(answer, dict):
        contract = case.get('input', {}).get('output_contract', {})
        for key, value in grading.get('expected', {}).items():
            actual = answer.get(key)
            schema = contract.get(key, {}) if isinstance(contract, dict) else {}
            bounded = isinstance(schema, dict) and any(k in schema for k in ('allowed_values', 'enum', 'const'))
            reference = bool(re.search(r'(^|_)(id|ids|source|sources|path|paths|owner|responder|actor|input|output|file|files)($|_)', key))
            if (not checks.get('field:' + key, True) and isinstance(value, str)
                    and isinstance(actual, str) and actual.strip() and not bounded and not reference):
                disputed.append('field:' + key)
    hard = failed - len(disputed)
    return {'success': failed == 0, 'hard_failures': hard, 'checks': checks, 'disputed_checks': disputed, 'pending_review': (bool(disputed) and hard == 0) or (grading.get('mode') == 'public_exact' and failed > 0 and bool(str(answer).strip()))}


def build_scoring_validity_audit(cases, records):
    """Audit supplied answers separately; never rewrite original grades or award passes.

    records contain case_id, answer, original_grade and optionally record_id.
    This contract-based audit does not compare natural-language meanings. Unknown
    wording is routed to blinded review only when no structural/value conflict exists.
    """
    catalog = dict(cases) if isinstance(cases, dict) else {case['id']: case for case in cases}
    if isinstance(records, dict):
        records = [dict(row, record_id=key) for key, row in records.items()]
    output = []
    for record in records:
        original = copy.deepcopy(record.get('original_grade', record.get('grade', {})))
        case_id = record.get('case_id', record.get('id'))
        case = catalog.get(case_id)
        audited = grade_task(case, record.get('answer')) if case else None
        demote = bool(case and not original.get('success') and audited['pending_review']
                      and (audited['hard_failures'] == 0 or case.get('grading', {}).get('mode') == 'public_exact'))
        # An audit must never upgrade a frozen failure to a pass, even when its
        # supplied answer differs from the recorded original grading.
        preserved = copy.deepcopy(original)
        if demote:
            preserved.update(success=False, pending_review=True)
        output.append({'record_id': record.get('record_id'), 'case_id': case_id,
                       'original_grade': original, 'audit_grade': preserved,
                       'review_status': 'pending_review' if demote else 'original_preserved',
                       'disputed_checks': audited.get('disputed_checks', []) if audited else [],
                       'reason': 'unbounded wording requires blinded review' if demote else
                                 'unknown case; no reassessment' if case is None else
                                 'original result retained; no permissible downgrade'})
    return {'version': 'core-scoring-validity-v1',
            'policy': 'Original results retained. Only disputed failures may become pending review. No automatic promotions.',
            'summary': {'records': len(output), 'pending_review': sum(r['review_status'] == 'pending_review' for r in output),
                        'promoted_to_success': 0}, 'records': output}


def write_scoring_validity_audit(path, cases, records):
    """Create a new audit artifact exclusively, refusing to overwrite any result."""
    artifact = build_scoring_validity_audit(cases, records)
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(artifact, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return artifact

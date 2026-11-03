import importlib.util
import json

import pytest


def module():
    spec = importlib.util.find_spec('benchmarks.final_skill_eval')
    assert spec is not None, 'final skill evaluation cohort must exist'
    from weyra import final_skill_eval
    return final_skill_eval


def test_manifest_freezes_24_heldout_tasks_and_balanced_maintenance_splits():
    m = module()
    manifest = m.load_manifest()
    assert set(manifest['families']) == {'evidence', 'multi_file', 'expression'}
    assert len(manifest['heldout']) == 24
    assert len(m.planned_units(manifest)) == 72
    for family in manifest['families'].values():
        assert len(family['development']) == 4
        assert len(family['validation']) == 4
    for family_id in ('evidence', 'multi_file', 'expression'):
        cases = [t for t in manifest['heldout'] if t['family'] == family_id]
        assert len(cases) == 8
        assert {t['coverage']: sum(c['coverage'] == t['coverage'] for c in cases)
                for t in cases} == {'new_requirement': 2, 'old_ability': 2,
                                    'exception': 2, 'mixed': 2}


def test_heldout_fixtures_are_varied_and_expected_outputs_stay_host_side():
    m = module()
    manifest = m.load_manifest()
    for family in ('evidence', 'multi_file', 'expression'):
        cases = [t for t in manifest['heldout'] if t['family'] == family]
        signatures = {tuple(sorted(t['files'])) for t in cases}
        assert len(signatures) >= 4
        assert all('expected' not in t and 'gold' not in t for t in cases)
        assert all(t['prompt'] and t['files'] for t in cases)
    assert all(c['correction'] == manifest['families'][c['family']]['feedback']
               for c in manifest['heldout'])
    assert all(c.get('checks', {}).get('required_substrings')
               for c in manifest['heldout'] if c['family'] == 'expression')


def test_planned_units_are_three_arms_and_exclude_development_and_validation():
    m = module()
    manifest = m.load_manifest()
    planned = m.planned_units(manifest)
    assert len(planned) == 72
    assert {row['variant'] for row in planned.values()} == {
        'old_no_feedback', 'old_direct_feedback', 'adapted_skill'}
    assert all(row['group'] == 'heldout' for row in planned.values())
    assert not any('development' in key or 'validation' in key for key in planned)


def test_host_grader_checks_multi_file_outputs_and_original_integrity(tmp_path):
    m = module()
    case = next(c for c in m.load_manifest()['heldout'] if c['family'] == 'multi_file')
    m.materialize(case, tmp_path)
    m.write_expected_artifacts(case, tmp_path)
    assert m.grade_artifacts(case, tmp_path)['success']
    input_path = tmp_path / next(name for name in case['files'] if name.endswith('.csv'))
    input_path.write_text('corrupted')
    assert m.grade_artifacts(case, tmp_path)['hard_failures'] > 0


def test_json_artifacts_allow_open_prose_and_formatting_but_enforce_file_set(tmp_path):
    m = module()
    case = next(c for c in m.load_manifest()['heldout'] if c['family'] == 'evidence')
    m.materialize(case, tmp_path)
    m.write_expected_artifacts(case, tmp_path)
    path = tmp_path / 'result.json'
    rows = json.loads(path.read_text())
    rows[0]['note'] = 'I selected this source because the document gives it higher authority.'
    path.write_text(json.dumps(rows, separators=(',', ':')))
    grade = m.grade_artifacts(case, tmp_path)
    assert grade['success']
    (tmp_path / 'unrequested.txt').write_text('extra')
    grade = m.grade_artifacts(case, tmp_path)
    assert grade['hard_failures'] == 1
    assert grade['extra_outputs'] == ['unrequested.txt']


def test_expression_grader_accepts_new_prose_with_required_facts_and_quote(tmp_path):
    m = module()
    case = next(c for c in m.load_manifest()['heldout'] if c['id'] == 'ex-quote_reply-1')
    m.materialize(case, tmp_path)
    text = ('Hi Lena Ortiz, you wrote, “The seal was broken when the parcel arrived.” '
            'A replacement review is underway for order OR-441. No refund decision has been made yet.')
    (tmp_path / 'result.json').write_text(json.dumps({'text': text}, ensure_ascii=False))
    grade = m.grade_artifacts(case, tmp_path)
    assert grade['success']


def test_fixture_paths_cannot_escape_workspace(tmp_path):
    m = module()
    with pytest.raises(ValueError):
        m.materialize({'files': {'../outside.txt': 'bad'}}, tmp_path)


def test_task_agent_must_use_skill_read_and_write_tools(tmp_path):
    m = module()
    case = m.load_manifest()['heldout'][0]
    task_folder = tmp_path / 'task'

    class Session:
        session_id = 'fake-session'
        records = []

    class Agent:
        session = Session()

        async def run_once(self, prompt):
            m.write_expected_artifacts(case, task_folder)
            self.session.records.extend(
                {'kind': 'tool_result', 'data': {'name': name, 'status': 'success'}}
                for name in ('skill', 'read_file', 'write_file')
            )
            return 'finished'

        async def close(self):
            pass

    from weyra.runtime_state import Budget
    runner = m.FinalSkillRunner(tmp_path / 'run', object(), Budget(cost_limit_cny='20'),
                                agent_factory=lambda **kwargs: Agent())
    try:
        import asyncio
        result = asyncio.run(runner.task(case, 'sample skill', 'old_no_feedback', task_folder))
        assert result['skill_loaded'] and result['file_read'] and result['file_write']
        assert result['artifact_evidence']
    finally:
        runner.close()


def test_direct_feedback_arm_adds_same_durable_correction_without_mutating_old_skill():
    m = module()
    case = m.load_manifest()['heldout'][0]
    prompt = m.prompt_for(case, 'old_direct_feedback')
    assert case['correction'] in prompt
    assert m.skill_body_for(case, 'old_direct_feedback', 'original') == 'original'
    assert case['correction'] not in m.prompt_for(case, 'old_no_feedback')


def test_report_preserves_fixed_72_task_denominator(tmp_path):
    m = module()
    from weyra.runtime_state import Budget
    runner = m.FinalSkillRunner(tmp_path / 'run', object(), Budget(cost_limit_cny='20'))
    try:
        report = runner.report()
        assert report['planned_final_tasks'] == 72
        assert report['recorded_final_tasks'] == 0
        assert report['groups']['heldout/adapted_skill']['n'] == 24
        assert report['groups']['heldout/adapted_skill']['success_rate'] == 0
    finally:
        runner.close()


def test_adapted_snapshot_must_be_committed_and_validation_manifest_is_separate(tmp_path):
    m = module()
    calls = []

    def gate_factory(family_id, validation_manifest, workspace):
        assert len(validation_manifest['cases']) == 4
        assert all('expected_artifacts' in case for case in validation_manifest['cases'])
        assert all('expected_artifacts' not in m.public_case(case) for case in m.load_manifest()['heldout'])
        return object()

    def maintenance_callback(**kwargs):
        calls.append(kwargs)
        assert len(kwargs['development']) == len(kwargs['validation']) == 4
        committed = kwargs['family']['initial_skill'] + '\nUse the complete evidence trail.'
        m.write_skill(kwargs['workspace'], kwargs['family_id'], committed)
        return {'committed_body': committed, 'rounds': [{'accepted': True}]}

    from weyra.runtime_state import Budget
    runner = m.FinalSkillRunner(tmp_path / 'run', object(), Budget(cost_limit_cny='20'),
                                gate_factory=gate_factory,
                                maintenance_callback=maintenance_callback,
                                max_maintenance_rounds=2)
    try:
        import asyncio
        snapshots = asyncio.run(runner.prepare_adapted_skills())
        assert set(snapshots) == {'evidence', 'multi_file', 'expression'}
        assert len(calls) == 3
        assert all(len(s['rounds']) <= 2 for s in snapshots.values())
    finally:
        runner.close()


def _all_cases(manifest):
    cases = list(manifest['heldout'])
    for family in manifest['families'].values():
        cases.extend(family['development'])
        cases.extend(family['validation'])
    return cases


def _derive_multifile_expected(case):
    import csv
    from decimal import Decimal
    from io import StringIO

    files = case['files']
    if 'outputs/active.txt' in case['expected_artifacts']:
        rows = []
        for name, text in files.items():
            if name.endswith('.csv'):
                rows.extend(csv.DictReader(StringIO(text)))
        active = [row for row in rows if row['status'] == 'active']
        names = ''.join(f"{row['name']}\n" for row in sorted(active, key=lambda row: row['name']))
        counts = {}
        for row in active:
            counts[row['region']] = counts.get(row['region'], 0) + 1
        return {'outputs/active.txt': names, 'outputs/counts.json': counts}
    if 'reconciled.csv' in case['expected_artifacts']:
        invoices = next(csv.DictReader(StringIO(text)) for name, text in files.items()
                        if name.endswith('.csv') and 'invoice' in text.splitlines()[0]
                        and 'total' in text.splitlines()[0])
        payment_rows = list(csv.DictReader(StringIO(files['payments.csv'])))
        paid = {row['invoice']: Decimal(row['paid']) for row in payment_rows}
        lines = ['invoice,total,paid,balance']
        for row in invoices:
            total = Decimal(row['total'])
            amount_paid = paid.get(row['invoice'], Decimal('0'))
            lines.append(f"{row['invoice']},{total:.2f},{amount_paid:.2f},{total - amount_paid:.2f}")
        return {'reconciled.csv': '\n'.join(lines) + '\n'}
    if 'export/keys.txt' in case['expected_artifacts']:
        records = json.loads(files['data/records.json'])['rows']
        retained = sorted((row for row in records if row['keep']),
                          key=lambda row: (row['rank'], row['key']))
        return {
            'export/keys.txt': ''.join(row['key'] + '\n' for row in retained),
            'export/summary.json': {'retained': len(retained), 'omitted': len(records) - len(retained)},
        }
    if 'catalog.json' in case['expected_artifacts']:
        rows = list(csv.DictReader(StringIO(files['source/items.tsv']), delimiter='\t'))
        active = sorted((row for row in rows if row['active'] == '1'),
                        key=lambda row: row['sku'].strip().lower())
        catalog = [{'sku': row['sku'].strip().lower(), 'name': row['name'].strip(),
                    'price': float(Decimal(row['price']))} for row in active]
        return {'catalog.json': catalog, 'count.txt': f'{len(catalog)}\n'}
    if 'selected.txt' in case['expected_artifacts']:
        rows = list(csv.DictReader(StringIO(files['input/rows.csv'])))
        return {'selected.txt': ''.join(row['code'] + '\n' for row in rows if row['include'] == 'yes')}
    raise AssertionError(f"No independent multi-file audit for {case['id']}")


def test_all_48_evidence_facts_are_supported_by_their_fixture_sources():
    manifest = module().load_manifest()
    cases = [case for case in _all_cases(manifest) if case['family'] == 'evidence']
    assert len(cases) == 16
    for case in cases:
        expected = json.loads(case['expected_artifacts']['result.json'])
        rows = (expected.get('rows', []) if isinstance(expected, dict) and 'rows' in expected
                else [expected] if isinstance(expected, dict) and 'claim' in expected
                else expected)
        source_text = '\n'.join(case['files'].values())
        assert rows, case['id']
        for row in rows:
            assert {'claim', 'value', 'source'} <= row.keys(), case['id']
            if row['source'] is None:
                assert row['value'] == 'unknown', (case['id'], row)
                assert row['claim'] not in source_text, (case['id'], row)
            else:
                assert row['source'] in source_text, (case['id'], row)
                assert str(row['value']) in source_text, (case['id'], row)
    corrected = next(c for c in cases if c['id'] == 'ev-unreported_measurement-2')
    first = json.loads(corrected['expected_artifacts']['result.json'])[0]
    assert first['value'] == '14:00–15:00'
    assert first['value'] in corrected['files']['memo.md']
    assert first['value'] in corrected['files']['records.tsv']


def test_all_48_multi_file_facts_are_independently_derived_from_inputs():
    manifest = module().load_manifest()
    cases = [case for case in _all_cases(manifest) if case['family'] == 'multi_file']
    assert len(cases) == 16
    for case in cases:
        expected = case['expected_artifacts']
        derived = _derive_multifile_expected(case)
        assert set(expected) == set(derived), case['id']
        for name, wanted in derived.items():
            actual = expected[name]
            if name.endswith('.json'):
                assert json.loads(actual) == wanted, case['id']
            else:
                assert actual == wanted, case['id']


def test_all_48_expression_writing_checks_match_expected_prose_and_source_quotes():
    manifest = module().load_manifest()
    cases = [case for case in _all_cases(manifest) if case['family'] == 'expression']
    assert len(cases) == 16
    for case in cases:
        checks = case.get('checks')
        assert checks and checks['required_substrings'], case['id']
        expected = json.loads(case['expected_artifacts']['result.json'])['text']
        source_text = '\n'.join(case['files'].values())
        for fact in checks['required_substrings']:
            # Checks are atomic names, IDs, dates, or numbers, not sentence-level paraphrases.
            assert fact.casefold() in expected.casefold(), (case['id'], fact)
            assert fact.casefold() in source_text.casefold(), (case['id'], fact)
        for quote in checks['exact_quotes']:
            assert quote in expected, (case['id'], quote)
            assert quote in source_text, (case['id'], quote)
        for phrase in checks['prohibited_phrases']:
            if phrase.casefold() not in source_text.casefold():
                assert phrase.casefold() not in expected.casefold(), (case['id'], phrase)


def test_quoted_comma_csv_fixture_is_parsed_as_one_name():
    manifest = module().load_manifest()
    case = next(c for c in manifest['heldout'] if c['id'] == 'mf-regional_roster-1')
    assert '"Ari, Jr.",north,active' in case['files']['input/north.csv']
    derived = _derive_multifile_expected(case)
    assert derived['outputs/active.txt'].splitlines() == ['Ari, Jr.', 'Cy', 'De']
    assert derived['outputs/active.txt'] == case['expected_artifacts']['outputs/active.txt']


def test_every_public_task_prompt_names_all_outputs_and_their_format_contracts():
    manifest = module().load_manifest()
    cases = _all_cases(manifest)
    assert len(cases) == 48
    for case in cases:
        prompt = case['prompt']
        for path in case['expected_artifacts']:
            assert path in prompt, (case['id'], path)
        if case['family'] == 'evidence':
            assert 'JSON' in prompt or 'json' in prompt, case['id']
            assert all(field in prompt for field in ('claim', 'value', 'source')), case['id']
            assert any(word in prompt.casefold() for word in ('array', 'object')), case['id']
        elif case['family'] == 'expression':
            assert 'JSON object' in prompt and 'text' in prompt, case['id']
        elif case['family'] == 'multi_file':
            for path in case['expected_artifacts']:
                assert path in prompt, (case['id'], path)
        if case['id'].startswith('ev-field_notes-'):
            assert 'arrival' in prompt and 'operator' in prompt, case['id']
        if case['id'].startswith(('ev-dev-', 'ev-val-')):
            assert '`code`' in prompt, case['id']


def test_expression_grader_accepts_fact_preserving_paraphrase_without_gold_phrasing(tmp_path):
    m = module()
    case = next(c for c in m.load_manifest()['families']['expression']['development']
                if c['id'] == 'ex-dev-1')
    m.materialize(case, tmp_path)
    paraphrase = ('Batch D-1: We checked 11 crates and accounted for 10. One crate is still missing. '
                  'The team has not investigated why yet.')
    (tmp_path / 'result.json').write_text(json.dumps({'text': paraphrase}))
    grade = m.grade_artifacts(case, tmp_path)
    assert grade['success']

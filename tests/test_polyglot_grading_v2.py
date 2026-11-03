import base64
import json
from types import SimpleNamespace
import pytest


def task(language='python'):
    name = 'sample.js' if language == 'javascript' else 'sample.py'
    return SimpleNamespace(language=language, editable=(name,),
        verifier_files={name: 'pass', 'sample_test.py': 'original tests'}, command=('pytest',))


class FakeSandbox:
    report = b'<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="sample" name="test_ok"/></testsuite>'
    code = 0
    output = ''
    modified = False
    seen = None

    def __init__(self, image):
        self.data = {}
    def create(self, files):
        self.data = files
        type(self).seen = files
    def inspect(self):
        return {}
    def execute(self, argv, timeout=60):
        if argv[-1] == '__verify_run__':
            return {'returncode': self.code, 'stdout': '', 'stderr': ''}
        path, offset = argv[-2], int(argv[-1])
        raw = {'/tmp/junit.xml': self.report, '/tmp/jest-result.json': self.report,
               '/tmp/verify-stdout.txt': self.output.encode(), '/tmp/verify-stderr.txt': b''}[path]
        chunk = raw[offset:offset + 262144]
        return {'returncode': 0, 'stdout': base64.b64encode(chunk).decode(), 'stderr': ''}
    def export(self, paths):
        return {p: (b'modified' if self.modified else self.data[p].encode()) for p in paths}
    def close(self):
        pass


@pytest.fixture
def grading(monkeypatch):
    import benchmarks.polyglot.grading_v2 as module
    monkeypatch.setattr(module, 'Sandbox', FakeSandbox)
    monkeypatch.setattr(module, 'isolation', lambda _: {'network': 'none'})
    for key, value in {'code': 0, 'output': '', 'modified': False}.items():
        monkeypatch.setattr(FakeSandbox, key, value)
    monkeypatch.setattr(FakeSandbox, 'report', b'<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="sample" name="test_ok"/></testsuite>')
    return module


def test_pass_persists_raw_and_hashes(grading, tmp_path):
    row = grading.verify(task(), 'image', {'sample.py': b'correct'}, tmp_path, ['sample::test_ok'])
    assert row['verdict'] == 'pass' and row['classification'] == 'passed'
    assert row['raw_verdict'] == 'pass' and not row['eligible_feedback']
    assert (tmp_path / 'junit.xml').read_bytes() == FakeSandbox.report
    saved = json.loads((tmp_path / 'verification.json').read_text())
    assert saved['stats']['immutable_sha256_before'] == saved['stats']['immutable_sha256_after']
    assert saved['stats']['expected_case_ids'] == ['sample::test_ok']


def test_assertion_feedback_prioritizes_case_details(grading, tmp_path):
    FakeSandbox.code = 1
    FakeSandbox.output = 'noise' * 2000
    FakeSandbox.report = b'<testsuite tests="1" failures="1"><testcase classname="sample" name="test_bad"><failure message="expected 2">actual 1</failure></testcase></testsuite>'
    row = grading.verify(task(), 'image', {}, tmp_path, ['sample::test_bad'])
    assert (row['verdict'], row['classification'], row['eligible_feedback']) == ('fail', 'assertion_failure', True)
    assert row['feedback'].startswith('sample::test_bad') and 'actual 1' in row['feedback']
    assert len(row['feedback']) <= 6000
    assert (tmp_path / 'stdout.txt').read_text() == FakeSandbox.output


@pytest.mark.parametrize('error,classification', [('ImportError: cannot import name sample', 'candidate_collection_error'), ('SyntaxError: invalid syntax', 'candidate_syntax_error')])
def test_candidate_collection_failure_is_eligible_despite_case_mismatch(grading, tmp_path, error, classification):
    FakeSandbox.code = 2
    FakeSandbox.output = error
    FakeSandbox.report = ('<testsuite tests="1" errors="1"><testcase name="collection"><error>' + error + '</error></testcase></testsuite>').encode()
    row = grading.verify(task(), 'image', {}, tmp_path, ['sample::test_ok'])
    assert (row['verdict'], row['classification'], row['eligible_feedback']) == ('fail', classification, True)
    assert row['stats']['case_identity_mismatch']


@pytest.mark.parametrize('kind', ['modified', 'mismatch', 'timeout', 'missing'])
def test_verification_errors_never_offer_feedback(grading, tmp_path, kind):
    if kind == 'modified':
        FakeSandbox.modified = True
    if kind == 'timeout':
        FakeSandbox.code = 124
    if kind == 'missing':
        FakeSandbox.report = b''
    row = grading.verify(task(), 'image', {}, tmp_path, ['wrong'] if kind == 'mismatch' else None)
    assert row['verdict'] == 'environment_error' and not row['eligible_feedback']
    assert row['classification'] == {'modified': 'test_integrity_error', 'mismatch': 'test_integrity_error', 'timeout': 'verification_timeout', 'missing': 'verification_error'}[kind]


def test_rejects_candidate_verifier_overwrite(grading, tmp_path):
    row = grading.verify(task(), 'image', {'sample_test.py': b'forged'}, tmp_path)
    assert row['classification'] == 'test_integrity_error'
    assert not row['eligible_feedback']


def test_jest_compile_error_without_assertion_cases(grading, tmp_path):
    FakeSandbox.code = 1
    FakeSandbox.report = json.dumps({'numTotalTests': 0, 'testResults': [{'message': 'SyntaxError: Unexpected token', 'assertionResults': []}]}).encode()
    row = grading.verify(task('javascript'), 'image', {}, tmp_path, ['some test'])
    assert row['classification'] == 'candidate_syntax_error' and row['eligible_feedback']


def test_assertion_mentioning_syntaxerror_is_still_assertion(grading, tmp_path):
    FakeSandbox.code = 1
    FakeSandbox.report = b'<testsuite tests="1" failures="1"><testcase classname="sample" name="test_bad"><failure message="Expected SyntaxError">assert False</failure></testcase></testsuite>'
    row = grading.verify(task(), 'image', {}, tmp_path, ['sample::test_bad'])
    assert row['classification'] == 'assertion_failure'


def test_skipped_report_is_integrity_failure(grading, tmp_path):
    FakeSandbox.report = b'<testsuite tests="1" skipped="1"><testcase classname="sample" name="test_ok"><skipped/></testcase></testsuite>'
    row = grading.verify(task(), 'image', {}, tmp_path)
    assert row['classification'] == 'test_integrity_error' and not row['eligible_feedback']


def test_jest_counts_and_raw_report(grading, tmp_path):
    FakeSandbox.report = json.dumps({'numTotalTests': 1, 'numPassedTests': 1, 'numFailedTests': 0,
        'testResults': [{'assertionResults': [{'fullName': 'sample works', 'status': 'passed'}]}]}).encode()
    row = grading.verify(task('javascript'), 'image', {}, tmp_path, ['sample works'])
    assert row['verdict'] == 'pass' and row['stats']['parsed_case_count'] == 1
    assert (tmp_path / 'jest-result.json').read_bytes() == FakeSandbox.report


def test_chunked_capture_preserves_more_than_display_limit(grading, tmp_path):
    FakeSandbox.output = 'large output\n' * 100000
    row = grading.verify(task(), 'image', {}, tmp_path)
    assert row['verdict'] == 'pass'
    assert (tmp_path / 'stdout.txt').read_text() == FakeSandbox.output


def test_hidden_skip_cannot_pass_with_inconsistent_summary(grading, tmp_path):
    FakeSandbox.report = b'<testsuite tests="1" skipped="0"><testcase classname="sample" name="test_ok"><skipped/></testcase></testsuite>'
    row = grading.verify(task(), 'image', {}, tmp_path)
    assert row['classification'] == 'test_integrity_error'


def test_jest_nonpassing_case_cannot_pass_with_inconsistent_summary(grading, tmp_path):
    FakeSandbox.report = json.dumps({'numTotalTests': 1, 'numPassedTests': 1, 'numFailedTests': 0,
        'testResults': [{'assertionResults': [{'fullName': 'sample works', 'status': 'pending'}]}]}).encode()
    row = grading.verify(task('javascript'), 'image', {}, tmp_path)
    assert row['classification'] == 'test_integrity_error'


def test_top_level_candidate_exception_is_collection_error(grading, tmp_path):
    FakeSandbox.code = 2
    FakeSandbox.output = 'ERROR collecting sample_test.py\nsample.py:1: NameError: name missing is not defined'
    FakeSandbox.report = b'<testsuite tests="1" errors="1"><testcase name="sample_test"><error message="collection failure">NameError: missing</error></testcase></testsuite>'
    row = grading.verify(task(), 'image', {}, tmp_path, ['sample::test_ok'])
    assert row['classification'] == 'candidate_collection_error' and row['eligible_feedback']


def test_jest_top_level_candidate_exception_is_collection_error(grading, tmp_path):
    FakeSandbox.code = 1
    FakeSandbox.report = json.dumps({'numTotalTests': 0, 'testResults': [{'message': 'ReferenceError: missing is not defined\n at sample.js:1',
        'testExecError': {'message': 'ReferenceError: missing is not defined'}, 'assertionResults': []}]}).encode()
    row = grading.verify(task('javascript'), 'image', {}, tmp_path, ['sample works'])
    assert row['classification'] == 'candidate_collection_error' and row['eligible_feedback']

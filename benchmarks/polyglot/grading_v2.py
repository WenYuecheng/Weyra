"""Auditable v2 grading in a fresh, isolated verifier for every call."""

import base64
import hashlib
import json
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

from .runner import atomic, isolation, validate_verdict
from .sandbox import Sandbox

MAX_RAW_BYTES = 16 * 1024 * 1024
CHUNK = 256 * 1024
# Capture inside the container so Sandbox.command's display truncation cannot lose logs.
_RUN = '''import subprocess,sys
with open('/tmp/verify-stdout.txt','wb') as out, open('/tmp/verify-stderr.txt','wb') as err:
 try:
  result=subprocess.run(sys.argv[1:-1],stdout=out,stderr=err,timeout=110)
  code=result.returncode
 except subprocess.TimeoutExpired:
  code=124
sys.exit(code)
'''
# Fixed paths only, no symlinks/devices, and each transfer is below Sandbox's output cap.
_READ = '''import os,stat,sys,base64
path,offset=sys.argv[1],int(sys.argv[2])
fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
with os.fdopen(fd,'rb') as f:
 if not stat.S_ISREG(os.fstat(f.fileno()).st_mode): raise ValueError('not regular')
 f.seek(offset)
 sys.stdout.write(base64.b64encode(f.read(262144)).decode())
'''


def _read(box, path):
    chunks = []
    for offset in range(0, MAX_RAW_BYTES + CHUNK, CHUNK):
        result = box.execute(['python', '-c', _READ, path, str(offset)])
        if result['returncode']:
            raise RuntimeError('Cannot retrieve ' + path + ': ' + result['stderr'])
        chunk = base64.b64decode(result['stdout'], validate=True)
        if offset + len(chunk) > MAX_RAW_BYTES:
            raise RuntimeError('Raw artifact exceeds 16 MiB: ' + path)
        chunks.append(chunk)
        if len(chunk) < CHUNK:
            return b''.join(chunks)
    raise RuntimeError('Raw artifact size exceeded')


def _parse(language, raw):
    details = []
    if language == 'javascript':
        stats = json.loads(raw)
        cases = [case for suite in stats.get('testResults', [])
                 for case in suite.get('assertionResults', [])]
        stats['case_ids'] = sorted(case['fullName'] for case in cases)
        for suite in stats.get('testResults', []):
            for case in suite.get('assertionResults', []):
                if case.get('status') == 'failed':
                    details.append(case['fullName'] + '\n' + '\n'.join(case.get('failureMessages', [])))
            if suite.get('message'):
                details.append(suite['message'])
        stats['parsed_case_count'] = len(cases)
        stats['parsed_counts'] = {status: sum(c.get('status') == status for c in cases)
                                  for status in ('passed', 'failed', 'pending', 'todo')}
        stats['case_status_inconsistent'] = (
            any(c.get('status') not in ('passed', 'failed') for c in cases)
            or stats['parsed_counts']['passed'] != stats.get('numPassedTests', 0)
            or stats['parsed_counts']['failed'] != stats.get('numFailedTests', 0))
    else:
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('XML declarations are not allowed')
        root = ET.fromstring(raw)
        suites = [root] if root.tag == 'testsuite' else list(root.iter('testsuite'))
        stats = {key: sum(int(s.get(key, 0)) for s in suites)
                 for key in ('tests', 'failures', 'errors', 'skipped')}
        cases = list(root.iter('testcase'))
        stats['case_ids'] = sorted(c.get('classname', '') + '::' + c.get('name', '') for c in cases)
        stats['parsed_case_count'] = len(cases)
        stats['parsed_counts'] = {tag: sum(c.find(tag) is not None for c in cases)
                                  for tag in ('failure', 'error', 'skipped')}
        stats['case_status_inconsistent'] = any(
            stats['parsed_counts'][tag] != stats[key]
            for tag, key in (('failure', 'failures'), ('error', 'errors'), ('skipped', 'skipped')))
        for case in cases:
            for failure in list(case):
                if failure.tag in ('failure', 'error'):
                    details.append(case.get('classname', '') + '::' + case.get('name', '') + '\n'
                                   + failure.get('message', '') + '\n' + ''.join(failure.itertext()))
    return stats, '\n\n'.join(details)


def _sha(files):
    return {p: hashlib.sha256(v.encode() if isinstance(v, str) else v).hexdigest()
            for p, v in files.items()}


def verify(task, image, files, folder, expected_cases=None, deadline=None):
    """Return verdict/classification/stats/feedback/eligible_feedback/raw_verdict.

    ``expected_cases`` uses sorted JUnit ``classname::name`` or Jest ``fullName`` IDs.
    All persistent evidence lives in ``folder``; feedback alone is limited to 6000 chars.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    immutable = {p: v for p, v in task.verifier_files.items() if p not in task.editable}
    stats = {'immutable_sha256_before': _sha(immutable),
             'immutable_sha256_after': {},
             'expected_case_ids': sorted(expected_cases) if expected_cases is not None else None}
    row = {'verdict': 'environment_error', 'classification': 'verification_error',
           'stats': stats, 'feedback': '', 'eligible_feedback': False,
           'raw_verdict': 'environment_error', 'returncode': None}
    # Preserve legacy callers; v3 supplies one shared deadline for all proof work.
    deadline = min(time.time() + 120, deadline) if deadline is not None else None
    box = Sandbox(image, deadline=deadline) if deadline is not None else Sandbox(image)
    details = ''
    stdout = stderr = ''
    try:
        if deadline is not None and time.time() >= deadline:
            raise TimeoutError("Verification deadline expired")
        if set(files) - set(task.editable):
            row['classification'] = 'test_integrity_error'
            raise ValueError('Candidate contains paths outside editable allowlist')
        box.create({**task.verifier_files, **files})
        atomic(folder / 'isolation.json', isolation(box.inspect()))
        result = box.execute(['python', '-c', _RUN, *task.command, '__verify_run__'], timeout=120)
        code = row['returncode'] = result['returncode']
        for name in ('stdout', 'stderr'):
            try:
                raw = _read(box, '/tmp/verify-' + name + '.txt')
            except Exception as exc:
                raw = result.get(name, '').encode()
                stats[name + '_capture_error'] = str(exc)
            (folder / (name + '.txt')).write_bytes(raw)
            if name == 'stdout':
                stdout = raw.decode(errors='replace')
            else:
                stderr = raw.decode(errors='replace')
        report_name = 'jest-result.json' if task.language == 'javascript' else 'junit.xml'
        try:
            raw = _read(box, '/tmp/' + report_name)
            (folder / report_name).write_bytes(raw)
            parsed, details = _parse(task.language, raw)
            stats.update(parsed)
        except Exception as exc:
            stats['report_error'] = type(exc).__name__ + ': ' + str(exc)
        parsed_stats = stats if 'case_ids' in stats else {}
        row['raw_verdict'] = validate_verdict(task.language, code, parsed_stats)
        integrity_error = False
        try:
            after = box.export(list(immutable))
            stats['immutable_sha256_after'] = _sha(after)
            integrity_error = stats['immutable_sha256_before'] != stats['immutable_sha256_after']
            if integrity_error:
                stats['test_files_modified'] = True
        except Exception as exc:
            stats['test_files_unreadable'] = str(exc)
            integrity_error = True
        mismatch = expected_cases is not None and stats.get('case_ids') != sorted(expected_cases)
        if mismatch:
            stats['case_identity_mismatch'] = True
        evidence = details + '\n' + stdout + '\n' + stderr
        # Missing verifier dependencies and resource exhaustion never become candidate feedback.
        infrastructure = code in (137, 143, -9) or bool(re.search(
            r'No module named (?:pytest|unittest)|Cannot find module [\'\"](?:jest|babel)|ENOSPC|out of memory|Cannot allocate memory', evidence, re.I))
        candidate_context = (bool(stats.get('errors')) if task.language == 'python'
                             else not stats.get('numFailedTests', 0)) or not stats.get('case_ids')
        syntax = candidate_context and bool(re.search(r'SyntaxError|IndentationError|TabError|could not compile|Unexpected token', evidence))
        collection = candidate_context and bool(re.search(r'ImportError|ModuleNotFoundError|Cannot find module|does not provide an export named|is not exported from|has no exported member', evidence))
        candidate_source = any(path in evidence for path in task.editable)
        collection = collection or (candidate_context and candidate_source
            and bool(re.search(r'NameError|ReferenceError|TypeError|ValueError|RuntimeError', evidence))
            and (('ERROR collecting' in evidence and code == 2)
                 or (task.language == 'javascript' and not stats.get('numTotalTests'))))
        total = stats.get('numTotalTests', 0) if task.language == 'javascript' else stats.get('tests', 0)
        skipped = (stats.get('numPendingTests', 0) + stats.get('numTodoTests', 0)
                   if task.language == 'javascript' else stats.get('skipped', 0))
        if code == 124 or (deadline is not None and time.time() >= deadline):
            row['classification'] = 'verification_timeout'
        elif integrity_error:
            row['classification'] = 'test_integrity_error'
        elif infrastructure or stats.get('stdout_capture_error') or stats.get('stderr_capture_error'):
            row['classification'] = 'verification_error'
        elif code and (syntax or collection):
            row.update(verdict='fail', classification='candidate_syntax_error' if syntax else 'candidate_collection_error', eligible_feedback=True)
        elif mismatch or skipped or stats.get('case_status_inconsistent') or (total and total != stats.get('parsed_case_count')):
            row['classification'] = 'test_integrity_error'
        elif stats.get('report_error') or not total:
            row['classification'] = 'verification_error'
        elif row['raw_verdict'] == 'pass':
            row.update(verdict='pass', classification='passed')
        elif row['raw_verdict'] == 'fail' and code in (0, 1):
            row.update(verdict='fail', classification='assertion_failure', eligible_feedback=True)
    except Exception as exc:
        stats['exception'] = type(exc).__name__ + ': ' + str(exc)
        if isinstance(exc, TimeoutError) or (deadline is not None and time.time() >= deadline):
            row.update(verdict='environment_error', classification='verification_timeout',
                       eligible_feedback=False, feedback='')
    finally:
        full_feedback = '\n\n'.join(s for s in (details, stdout, stderr) if s)
        (folder / 'feedback-full.txt').write_text(full_feedback)
        if row['eligible_feedback']:
            row['feedback'] = full_feedback[:6000]
        try:
            box.close()
            atomic(folder / 'cleanup.json', {'removed': True})
        except Exception as exc:
            stats['cleanup_error'] = str(exc)
            row.update(verdict='environment_error', classification='verification_error', eligible_feedback=False, feedback='')
            atomic(folder / 'cleanup.json', {'removed': False, 'error': str(exc)})
        atomic(folder / 'verification.json', row)
    return row

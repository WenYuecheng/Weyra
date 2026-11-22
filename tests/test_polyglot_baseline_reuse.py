"""Shared baselines require complete, identical A conditions and immutable evidence."""

import copy
import hashlib
import json
import shutil

import pytest

from benchmarks.polyglot_full import baseline_reuse


@pytest.fixture
def baseline(tmp_path):
    source = tmp_path / "source"
    tasks = [f"python/case-{i}" for i in range(225)]
    expected = {
        "dataset": "dataset", "model": "model", "endpoint": "https://model.example/",
        "images": {"python": "image"},
        "source": "source-digest",
        "tool_names": ["read_file", "write_file"],
        "tasks": {key: {"id": key, "verifier_sha256": {"test.py": "fixture-digest"}} for key in tasks},
        "checks": {key: {"image": "image", "cases": ["test"]} for key in tasks},
        "task_tokens": 65536, "task_calls": 2, "rounds": 2,
        "baseline_skill": "base", "system": "system",
        "profile": {"output_tokens": 8192, "reasoning_effort": "high", "feedback_mode": "test_output",
                    "repair_context": "contract", "repair_review": "audit"},
    }
    source.mkdir()
    old = copy.deepcopy(expected)
    old["profile"].update(repair_context="fresh", repair_review="")
    (source / "full-protocol.json").write_text(json.dumps(old))
    rows = []
    for task in tasks:
        folder = source / "formal" / task / "A" / "attempt-1"
        folder.mkdir(parents=True)
        stage = folder / "1"
        (stage / "implementation").mkdir(parents=True)
        code = b"answer = 42"
        (stage / "implementation/answer.py").write_bytes(code)
        (stage / "verification.json").write_text(json.dumps({
            "verdict": "pass", "stats": {"case_ids": ["test"]},
        }))
        result = {"status": "pass", "accounted_tokens": 10, "rounds": [{
            "round": 1, "verdict": "pass", "file_hashes": {"answer.py": hashlib.sha256(code).hexdigest()},
        }]}
        (folder / "result.json").write_text(json.dumps(result))
        (folder / "history.json").write_text("[]")
        (folder / "trajectory.jsonl").write_text("")
        rows.append({"id": task, "A": result, "R": result})
    (source / "full-report.json").write_text(json.dumps({
        "campaign_status": "completed", "coverage_complete": True, "rows": rows,
    }))
    return source, expected, tasks


def test_reuse_is_explicit_and_keeps_original_outcomes(baseline, tmp_path):
    source, expected, tasks = baseline
    snapshot = baseline_reuse.snapshot(source, expected, tasks)
    assert snapshot["reused_arms"] == ["A"]
    assert len(snapshot["outcomes"]) == 225
    output = tmp_path / "candidate"
    baseline_reuse.restore(source, output, snapshot)
    result = output / "formal" / tasks[0] / "A/attempt-1/result.json"
    assert json.loads(result.read_text())["accounted_tokens"] == 10
    assert not (output / "formal" / tasks[0] / "R").exists()


@pytest.mark.parametrize(
    "field", ["profile", "checks", "system", "task_calls", "source", "tool_names", "tasks"]
)
def test_reuse_rejects_changed_baseline_conditions(baseline, field):
    source, expected, tasks = baseline
    wrong = copy.deepcopy(expected)
    if field == "profile":
        wrong[field]["output_tokens"] = 16384
    elif field == "tool_names":
        wrong[field].reverse()
    elif field == "tasks":
        wrong[field][tasks[0]]["verifier_sha256"]["test.py"] = "changed-digest"
    else:
        wrong[field] = "different"
    with pytest.raises(ValueError, match="Baseline conditions"):
        baseline_reuse.snapshot(source, wrong, tasks)


def test_reuse_rejects_incomplete_campaign_and_changed_evidence(baseline, tmp_path):
    source, expected, tasks = baseline
    snapshot = baseline_reuse.snapshot(source, expected, tasks)
    result = source / "formal" / tasks[0] / "A/attempt-1/result.json"
    result.write_text('{"status":"fail"}')
    with pytest.raises(ValueError, match="evidence changed"):
        baseline_reuse.restore(source, tmp_path / "output", snapshot)
    (source / "full-report.json").write_text('{"coverage_complete":false,"campaign_status":"running"}')
    with pytest.raises(ValueError, match="completed 225"):
        baseline_reuse.snapshot(source, expected, tasks)


def test_reuse_rejects_symlinked_evidence_parent(baseline, tmp_path):
    source, expected, tasks = baseline
    original = source / "formal/python"
    outside = tmp_path / "outside"
    shutil.move(original, outside)
    original.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="root"):
        baseline_reuse.snapshot(source, expected, tasks)


def test_reuse_rejects_stale_report_before_first_snapshot(baseline):
    source, expected, tasks = baseline
    result = source / "formal" / tasks[0] / "A/attempt-1/result.json"
    result.write_text('{"status":"fail"}')
    with pytest.raises(ValueError, match="report"):
        baseline_reuse.snapshot(source, expected, tasks)


def test_reuse_rejects_linked_protocol_and_missing_implementation(baseline, tmp_path):
    source, expected, tasks = baseline
    protocol = source / "full-protocol.json"
    outside = tmp_path / "outside.json"
    shutil.move(protocol, outside)
    protocol.symlink_to(outside)
    with pytest.raises(ValueError, match="regular"):
        baseline_reuse.snapshot(source, expected, tasks)
    protocol.unlink()
    shutil.move(outside, protocol)
    implementation = source / "formal" / tasks[0] / "A/attempt-1/1/implementation/answer.py"
    implementation.unlink()
    with pytest.raises(ValueError, match="implementation"):
        baseline_reuse.snapshot(source, expected, tasks)


@pytest.mark.parametrize("field", ["source", "tool_names", "tasks"])
@pytest.mark.parametrize("missing", ["previous", "expected", "both"])
def test_reuse_requires_fingerprint_keys(baseline, field, missing):
    source, expected, tasks = baseline
    protocol = source / "full-protocol.json"
    if missing in {"previous", "both"}:
        previous = json.loads(protocol.read_text())
        del previous[field]
        protocol.write_text(json.dumps(previous))
    if missing in {"expected", "both"}:
        del expected[field]
    with pytest.raises(ValueError, match="Baseline conditions"):
        baseline_reuse.snapshot(source, expected, tasks)


@pytest.mark.parametrize(
    "status, final_verdict, valid",
    [
        ("fail", None, False),
        ("fail", "pass", False),
        ("fail", "environment_error", False),
        ("fail", "fail", True),
        ("resource_exhausted", None, True),
    ],
)
def test_reuse_checks_terminal_fail_verification(baseline, status, final_verdict, valid):
    source, expected, tasks = baseline
    attempt = source / "formal" / tasks[0] / "A/attempt-1"
    result_path = attempt / "result.json"
    outcome = json.loads(result_path.read_text())
    outcome["status"] = status
    if final_verdict is None:
        outcome["rounds"] = []
    else:
        outcome["rounds"][-1]["verdict"] = final_verdict
        (attempt / "1/verification.json").write_text(json.dumps({
            "verdict": final_verdict, "stats": {"case_ids": ["test"]},
        }))
    result_path.write_text(json.dumps(outcome))
    report_path = source / "full-report.json"
    report = json.loads(report_path.read_text())
    report["rows"][0]["A"] = outcome
    report_path.write_text(json.dumps(report))

    if valid:
        assert baseline_reuse.snapshot(source, expected, tasks)["reused_arms"] == ["A"]
    else:
        with pytest.raises(ValueError, match="Baseline .*verification"):
            baseline_reuse.snapshot(source, expected, tasks)


def test_reuse_hashes_the_documents_it_validated(baseline, monkeypatch, tmp_path):
    source, expected, tasks = baseline
    protocol, report = (source / name for name in ("full-protocol.json", "full-report.json"))
    protocol_digest, report_digest = (
        hashlib.sha256(path.read_bytes()).hexdigest() for path in (protocol, report)
    )
    validate = baseline_reuse._validate_artifacts

    def change_documents_during_validation(*args):
        protocol.write_text("{}")
        report.write_text("{}")
        validate(*args)

    monkeypatch.setattr(baseline_reuse, "_validate_artifacts", change_documents_during_validation)
    frozen = baseline_reuse.snapshot(source, expected, tasks)
    assert frozen["protocol_sha256"] == protocol_digest
    assert frozen["report_sha256"] == report_digest
    with pytest.raises(ValueError, match="evidence changed"):
        baseline_reuse.restore(source, tmp_path / "output", frozen)

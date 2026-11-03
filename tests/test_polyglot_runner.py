import pytest
from benchmarks.polyglot.runner import validate_verdict, save_once, load_once


def test_verdict_requires_executed_tests_and_no_skips():
    assert (
        validate_verdict(
            "javascript",
            0,
            {
                "numTotalTests": 3,
                "numPassedTests": 3,
                "numPendingTests": 0,
                "numFailedTests": 0,
                "testResults": [{"status": "passed"}],
            },
        )
        == "pass"
    )
    assert (
        validate_verdict("javascript", 0, {"numTotalTests": 0, "numPassedTests": 0})
        == "environment_error"
    )
    assert (
        validate_verdict("python", 0, {"tests": 4, "failures": 0, "errors": 0, "skipped": 1})
        == "environment_error"
    )
    assert (
        validate_verdict("python", 1, {"tests": 4, "failures": 1, "errors": 0, "skipped": 0})
        == "fail"
    )


def test_result_is_create_only_and_resume_fingerprint_locked(tmp_path):
    p = tmp_path / "state.json"
    save_once(p, {"protocol": "abc", "started": True})
    with pytest.raises(FileExistsError):
        save_once(p, {"protocol": "abc"})
    assert load_once(p, "abc")["started"]
    with pytest.raises(ValueError):
        load_once(p, "def")

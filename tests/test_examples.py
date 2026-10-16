import json
import subprocess
import sys
from pathlib import Path


def test_project_analysis_demo():
    result = subprocess.run(
        [sys.executable, "examples/project_analysis.py"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["child_state"] == "completed"
    assert report["actual_tool_results"] >= 3
    assert report["paid_model_calls"] == 0


def test_personalized_writing_demo():
    result = subprocess.run(
        [sys.executable, "examples/personalized_writing.py"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["updated_version"] == "1.0.1"
    assert report["rollback_version"] == "1.0.2"
    assert report["boundary_experience"]
    assert report["paid_model_calls"] == 0


def test_fixed_scenario_manifest_has_twenty_cases():
    cases = json.loads(Path("examples/scenarios.json").read_text())
    assert len(cases) == 20
    assert len({case["id"] for case in cases}) == 20
    assert sum(case["family"] == "project" for case in cases) == 10
    assert sum(case["family"] == "writing" for case in cases) == 10

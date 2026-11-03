"""Rebuild inspectable engineering evidence. This script never calls a model API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def run(name, command, *, cwd=ROOT, timeout=180, env=None):
    started = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
    )
    output = result.stdout + result.stderr
    return {
        "name": name,
        "command": command,
        "started_at": started,
        "exit_code": result.returncode,
        "output": output,
        "passed": result.returncode == 0,
    }


def _verify(root):
    python = sys.executable
    results = []
    commands = [
        ("tests", [python, "-m", "pytest", "-q"]),
        ("lint", [python, "-m", "ruff", "check", "weyra", "benchmarks", "tests", "examples", "scripts"]),
        (
            "analysis_demo",
            [
                python,
                "-m",
                "weyra.main",
                "demo",
                "--offline",
                "--scenario",
                "analysis",
                "--project",
                str(root / "demos"),
            ],
        ),
        (
            "writing_demo",
            [
                python,
                "-m",
                "weyra.main",
                "demo",
                "--offline",
                "--scenario",
                "writing",
                "--project",
                str(root / "demos"),
            ],
        ),
        (
            "offline_evaluation",
            [
                python,
                "-m",
                "weyra.main",
                "eval",
                "--offline",
                "--project",
                str(root / "evaluation"),
            ],
        ),
        ("build", [python, "-m", "build", "--no-isolation"]),
    ]
    for name, command in commands:
        result = run(name, command)
        results.append(result)
        print(f"{name}: {'PASS' if result['passed'] else 'FAIL'}", flush=True)
        if not result["passed"]:
            print(result["output"][-4000:])
    if results[-1]["passed"]:
        result = run("package_verification", [python, str(ROOT / "scripts/verify_package.py")])
        results.append(result)
        print(f"package_verification: {'PASS' if result['passed'] else 'FAIL'}", flush=True)
        if not result["passed"]:
            print(result["output"][-4000:])
    manifest = {}
    for pattern in ("weyra/**/*.py", "benchmarks/**/*", "tests/*.py", "data/**/*", "examples/*.py", "scripts/**/*.py"):
        for path in ROOT.glob(pattern):
            if path.is_file() and "__pycache__" not in path.parts:
                manifest[str(path.relative_to(ROOT))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    tests = re.search(r"(\d+) passed", results[0]["output"])
    evaluation = next(result for result in results if result["name"] == "offline_evaluation")
    evaluation_report = json.loads(evaluation["output"]) if evaluation["passed"] else None
    destination = ROOT / ".benchmarks/results/engineering"
    destination.mkdir(parents=True, exist_ok=True)
    if evaluation_report:
        (destination / "offline-evaluation.json").write_text(
            json.dumps(evaluation_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    report = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "engineering_checks_passed": all(item["passed"] for item in results),
        "test_count": int(tests.group(1)) if tests else None,
        "commands": results,
        "source_sha256": manifest,
        "scope": "Offline engineering evidence; not model quality or live provider verification.",
    }
    (destination / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if report["engineering_checks_passed"] else 1


def main():
    with tempfile.TemporaryDirectory(prefix="weyra-verification-") as directory:
        return _verify(Path(directory))


if __name__ == "__main__":
    raise SystemExit(main())

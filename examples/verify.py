"""Rebuild inspectable engineering evidence. This script never calls a model API."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
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


def main():
    root = ROOT / ".weyra" / "verification"
    root.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    results = []
    commands = [
        ("tests", [python, "-m", "pytest", "-q"]),
        ("lint", [python, "-m", "ruff", "check", "agents", "tests", "examples"]),
        (
            "analysis_demo",
            [
                python,
                "-m",
                "agents.main",
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
                "agents.main",
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
                "agents.main",
                "eval",
                "--offline",
                "--project",
                str(root / "evaluation"),
            ],
        ),
        ("build", [python, "-m", "build"]),
    ]
    for name, command in commands:
        result = run(name, command)
        results.append(result)
        print(f"{name}: {'PASS' if result['passed'] else 'FAIL'}", flush=True)
        if not result["passed"]:
            print(result["output"][-4000:])
    if results[-1]["passed"]:
        package_env = root / "package-env"
        isolated_work = root / "installed-work"
        isolated_work.mkdir(exist_ok=True)
        wheel = max((ROOT / "dist").glob("weyra-*.whl"), key=lambda path: path.stat().st_mtime)
        installed_python = str(package_env / "bin" / "python")
        # Reuse SDK dependencies, while importing Weyra only from the fresh wheel.
        # PYTHONPATH directories do not execute the editable install's .pth file.
        env = {**os.environ, "PYTHONPATH": sysconfig.get_path("purelib")}
        package_commands = [
            ("isolated_env", [python, "-m", "venv", str(package_env)]),
            (
                "wheel_install",
                [
                    installed_python,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--force-reinstall",
                    str(wheel),
                ],
            ),
            (
                "installed_resources",
                [
                    installed_python,
                    "-c",
                    "import agents; from agents.evaluation import load_cases; assert 'package-env/lib' in agents.__file__; assert len(load_cases()) == 20; print('wheel import and twenty packaged scenarios: PASS')",
                ],
            ),
            (
                "installed_demo",
                [
                    str(package_env / "bin" / "weyra"),
                    "demo",
                    "--offline",
                    "--scenario",
                    "analysis",
                    "--project",
                    str(isolated_work),
                ],
            ),
        ]
        for name, command in package_commands:
            result = run(name, command, cwd=isolated_work, env=env)
            results.append(result)
            print(f"{name}: {'PASS' if result['passed'] else 'FAIL'}", flush=True)
            if not result["passed"]:
                print(result["output"][-4000:])
                break
    manifest = {}
    for pattern in ("agents/*.py", "tests/*.py", "data/**/*", "examples/*.py"):
        for path in ROOT.glob(pattern):
            if path.is_file() and "__pycache__" not in path.parts:
                manifest[str(path.relative_to(ROOT))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    tests = re.search(r"(\d+) passed", results[0]["output"])
    evaluation = next(result for result in results if result["name"] == "offline_evaluation")
    evaluation_report = json.loads(evaluation["output"]) if evaluation["passed"] else None
    if evaluation_report:
        (ROOT / "wiki" / "offline-evaluation.json").write_text(
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
    (ROOT / "wiki" / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if report["engineering_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

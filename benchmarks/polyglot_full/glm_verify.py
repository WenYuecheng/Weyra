"""Container-side verifier driver plus host-side interpretation."""

import json
from pathlib import Path
import re

from .runner import atomic, isolation, verify
from .sandbox import Sandbox

VERIFIER_VERSION = 4

FAILURE_LABELS = {
    "compilation": "Compilation failed.",
    "tests": "Tests failed.",
    "execution": "Test execution failed.",
}
DIAGNOSTIC_CODE = re.compile(
    r"\b(?:AssertionError|SyntaxError|TypeError|ValueError|IndexError|KeyError|"
    r"NameError|ReferenceError|ImportError|NotImplementedError|"
    r"NullPointerException|IllegalArgumentException)\b|\berror\[E\d{4}\]"
)
CASE_LABEL = re.compile(r"[\w .:/(),\[\]+&'=-]{1,160}")

DRIVER = r"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

def run(*argv):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(result.stdout, end="")
    return result.returncode, result.stdout

def xml_cases(paths):
    tests, failures = [], []
    for path in paths:
        for test in ET.parse(path).iter("testcase"):
            name = test.attrib.get("classname", "") + "::" + test.attrib["name"]
            tests.append(name)
            if any(test.find(tag) is not None for tag in ("failure", "error", "skipped")):
                failures.append(name)
    return tests, failures

language = sys.argv[1]
tests, failures, stage = [], [], "tests"
if language == "go":
    Path("/work/.build").mkdir()
    os.environ["GOTMPDIR"] = "/work/.build"
    os.environ["GOCACHE"] = "/work/.build/cache"
    code, output = run("go", "test", "-json", "-count=1", "./...")
    rows = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    tests = [row["Test"] for row in rows if row.get("Test") and row["Action"] == "run"]
    failures = [row["Test"] for row in rows if row.get("Test") and row["Action"] in {"fail", "skip"}]
    failed = any(row["Action"] in {"fail", "skip"} for row in rows)
    if any(row.get("FailedBuild") or row["Action"] == "build-fail" for row in rows):
        stage = "compilation"
elif language == "go-counter":
    build = Path(".build").resolve()
    build.mkdir()
    os.environ["GOTMPDIR"] = str(build)
    os.environ["GOCACHE"] = str(build / "cache")
    for index in (4, 1, 2, 3):
        os.environ["COUNTER_IMPL"] = str(index)
        code, output = run("go", "test", "-json", "-count=1", "-timeout=15s", ".")
        name = "counter-implementation-" + str(index)
        tests.append(name)
        rows = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        test_rows = [row for row in rows if row.get("Test")]
        test_failure = any(row["Action"] == "fail" for row in test_rows)
        test_pass = any(row["Action"] == "pass" for row in test_rows)
        test_skip = any(row["Action"] == "skip" for row in test_rows)
        panic = any("panic:" in row.get("Output", "") for row in rows)
        accepted = code == 0 and test_pass and not test_failure
        rejected = code != 0 and test_failure and not panic
        if test_skip or not (accepted if index == 4 else rejected):
            failures.append(name)
    failed = bool(failures)
    code = int(failed)
elif language == "rust":
    cache = Path("/opt/rust/cargo/registry")
    # Keep immutable downloaded crates in the image, with a writable Cargo lock in /tmp.
    Path("/tmp/cargo").mkdir(exist_ok=True)
    if cache.exists():
        Path("/tmp/cargo/registry").symlink_to(cache, target_is_directory=True)
    seed = Path("/opt/rust/dependencies.lock")
    if seed.exists() and not Path("Cargo.lock").exists():
        shutil.copyfile(seed, "Cargo.lock")
    code, output = run("cargo", "test", "--offline", "--all-features", "--", "--test-threads=1")
    tests = re.findall(r"(?m)^test (.+?) \.\.\. (?:ok|FAILED|ignored)", output)
    failures = re.findall(r"(?m)^test (.+?) \.\.\. (?:FAILED|ignored)", output)
    failed = bool(failures)
    if re.search(r"(?m)^error(?:\[E\d{4}\]:|: could not compile)", output):
        stage = "compilation"
elif language == "cpp":
    source = sorted(str(p) for p in Path(".").glob("*.cpp"))
    source.extend(str(p) for p in Path("test").glob("*.cpp"))
    code, _ = run("g++", "-std=c++17", "-O0", "-I.", "-DEXERCISM_RUN_ALL_TESTS",
                  "-pthread", *source, "-lboost_date_time", "-ltbb", "-o", "/work/.test-bin")
    if code == 0:
        code, _ = run("/work/.test-bin", "-r", "junit", "-o", "/tmp/cases.xml")
        if Path("/tmp/cases.xml").is_file():
            tests, failures = xml_cases([Path("/tmp/cases.xml")])
    else:
        stage = "compilation"
    failed = bool(failures)
else:
    jars = sorted(Path("/opt/java-libs").glob("*.jar"))
    cp = ":".join(map(str, jars))
    src = sorted(str(p) for p in Path("src").rglob("*.java"))
    code, _ = run("javac", "-cp", cp, "-d", "/tmp/classes", *src)
    if code == 0:
        console = next(jar for jar in jars if "standalone" in jar.name)
        code, _ = run("java", "-jar", str(console), "execute",
                      "--class-path", "/tmp/classes:" + cp,
                      "--scan-classpath", "--reports-dir", "/tmp/junit", "--fail-if-no-tests")
        tests, failures = xml_cases(sorted(Path("/tmp/junit").glob("*.xml")))
    else:
        stage = "compilation"
    failed = bool(failures)
print("WEYRA_VERDICT " + json.dumps({
    "code": code, "case_ids": sorted(tests), "failed": failed,
    "failed_case_ids": sorted(set(failures)), "failure_stage": stage if code or failed else None,
}))
"""


def feedback(folder: Path, expected_cases: list[str] | None = None) -> str:
    """Render bounded failure facts without passing test output or expected data to the model."""
    verification = folder / "verification.json"
    stats = json.loads(verification.read_text()).get("stats", {}) if verification.exists() else {}
    stage = stats.get("failure_stage")
    allowed = set(expected_cases) if expected_cases is not None else None
    cases = list(stats.get("failed_case_ids", ()))
    if "failed_case_ids" not in stats:
        cases.extend(
            case["fullName"]
            for suite in stats.get("testResults", ())
            for case in suite.get("assertionResults", ())
            if case.get("status") == "failed"
        )
    codes = []
    for file in ("stdout.txt", "stderr.txt"):
        for line in (folder / file).read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("FailedBuild") or event.get("Action") == "build-fail":
                    stage = stage or "compilation"
                if event.get("Action") == "fail" and isinstance(event.get("Test"), str):
                    cases.append(event["Test"])
                continue
            if match := re.match(r"^FAILED (\S+::\S+)", line):
                filename, *scopes, name = match[1].split("::")
                namespace = ".".join([filename.removesuffix(".py").replace("/", "."), *scopes])
                cases.append(f"{namespace}::{name}")
            elif match := re.fullmatch(r"test (.+?) \.\.\. FAILED", line):
                cases.append(match[1])
            if match := DIAGNOSTIC_CODE.search(line):
                codes.append(match[0])
            if re.match(r"^\S+:\d+:(?:\d+:)? (?:fatal )?error:", line) or line.startswith(
                "error[E"
            ):
                stage = stage or "compilation"
    cases = list(
        dict.fromkeys(
            name
            for name in cases
            if CASE_LABEL.fullmatch(name) and (allowed is None or name in allowed)
        )
    )
    stage = stage or ("tests" if cases or stats.get("case_ids") else "execution")
    lines = [FAILURE_LABELS.get(stage, FAILURE_LABELS["execution"])]
    lines.extend(f"Failed test: {name}" for name in cases)
    lines.extend(f"Diagnostic: {code}" for code in dict.fromkeys(codes))
    return "\n".join(lines)[:1500]


def verify_task(task, image, files, folder, expected=None):
    if task.language in {"python", "javascript"}:
        outcome, stats = verify(task, image, files, folder, expected)
        if outcome == "environment_error" and expected is not None:
            log = "\n".join(
                (Path(folder) / name).read_text() for name in ("stdout.txt", "stderr.txt")
            )
            if (
                any(
                    marker in log
                    for marker in (
                        "SyntaxError",
                        "ImportError",
                        "Test suite failed to run",
                        "ERROR collecting",
                        "ReferenceError",
                    )
                )
                and not stats.get("test_files_modified")
                and not stats.get("test_files_unreadable")
            ):
                outcome = "fail"
                record = Path(folder) / "verification.json"
                stored = json.loads(record.read_text())
                stored["verdict"] = outcome
                stats["failure_stage"] = "execution"
                stored["stats"] = stats
                atomic(record, stored)
        return outcome, stats

    folder = Path(folder)
    box = Sandbox(image, compile_tests=task.language in {"cpp", "go", "rust"})
    protected = dict(task.verifier_files)
    protected["_weyra_verify.py"] = DRIVER
    full = {**protected, **files}
    try:
        box.create(full)
        atomic(folder / "isolation.json", isolation(box.inspect()))
        mode = (
            "go-counter"
            if task.id == "go/counter" and task.editable == ("counter_test.go",)
            else task.language
        )
        result = box.execute(["python", "/work/_weyra_verify.py", mode], timeout=120)
        (folder / "stdout.txt").write_text(result["stdout"])
        (folder / "stderr.txt").write_text(result["stderr"])
        if result["returncode"] == 124:
            stats = {"timed_out": True, "failure_stage": "execution"}
            verdict = "fail" if expected is not None else "environment_error"
            atomic(folder / "verification.json", {
                "returncode": 124, "stats": stats, "verdict": verdict,
            })
            return verdict, stats
        lines = [
            line for line in result["stdout"].splitlines() if line.startswith("WEYRA_VERDICT ")
        ]
        stats = json.loads(lines[-1].split(" ", 1)[1]) if lines else {}
        checked = [name for name in protected if name not in files]
        if not stats or box.export(checked) != {key: protected[key].encode() for key in checked}:
            verdict = "environment_error"
        elif stats["code"] or stats["failed"]:
            verdict = "fail"
        elif stats["case_ids"] and (expected is None or stats["case_ids"] == expected):
            verdict = "pass"
        else:
            verdict = "environment_error"
        atomic(folder / "verification.json", {"verdict": verdict, "stats": stats})
        return verdict, stats
    finally:
        box.close()

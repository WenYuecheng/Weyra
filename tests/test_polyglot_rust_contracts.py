"""Offline regressions for protected Rust doctest grading."""

import json
import subprocess
import sys

import pytest

from benchmarks.polyglot_full.glm_dataset import discover_all
from benchmarks.polyglot_full.glm_verify import DRIVER

PRIVATE = "_weyra_rust_contracts/"
MANIFEST = PRIVATE + "manifest.json"
FIRST = "doctest:src/lib.rs:1"
SECOND = "doctest:src/pre_implemented.rs:1"
EXAMPLE = "/// ```compile_fail\n/// use react::Missing;\n/// ```\n"


def rust_exercise(root, slug="react", starter=EXAMPLE, fixed=EXAMPLE):
    folder = root / "rust/exercises/practice" / slug
    for name, content in {
        ".meta/config.json": json.dumps(
            {
                "files": {
                    "solution": ["src/lib.rs"],
                    "example": [".meta/example.rs"],
                    "test": [],
                }
            }
        ),
        ".meta/example.rs": EXAMPLE.replace("Missing", "FromReference"),
        "Cargo.toml": '[package]\nname = "' + slug + '"\nversion = "0.1.0"\n',
        "src/lib.rs": starter,
        "src/pre_implemented.rs": fixed.replace("///", "//!"),
    }.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return next(discover_all(root))


def test_private_contracts_come_from_original_rust_files(tmp_path):
    task = rust_exercise(tmp_path)
    private = {p: v for p, v in task.verifier_files.items() if p.startswith(PRIVATE)}
    assert set(private) == {MANIFEST, PRIVATE + "harness.rs", PRIVATE + "positive.rs"}
    assert private.keys().isdisjoint(task.files)
    assert private.keys() <= task.metadata()["verifier_sha256"].keys()
    assert "use react::Missing;" in private[PRIVATE + "harness.rs"]
    assert "FromReference" not in private[PRIVATE + "harness.rs"]
    assert list(json.loads(private[MANIFEST])["cases"].values()) == [FIRST, SECOND]
    moved = rust_exercise(tmp_path, starter="\n/// Intro\n" + EXAMPLE)
    assert all(moved.verifier_files[path] == content for path, content in private.items())


@pytest.mark.parametrize(
    "slug, starter, error",
    [
        ("react", "/// ```compile_fail\n/// use react::Missing;\n", "Unclosed"),
        ("react", "/// ```compile_fail\n//! use react::Missing;\n/// ```\n", "Unclosed"),
        ("react", "/** ```compile_fail\ntext\n``` */", "Unsupported"),
        ("react", '#[doc = "```compile_fail"]', "Unsupported"),
        ("new-exercise", EXAMPLE, "positive control"),
    ],
)
def test_discovery_rejects_unsupported_contracts(tmp_path, slug, starter, error):
    with pytest.raises(ValueError, match=error):
        rust_exercise(tmp_path, slug, starter)


def test_reference_only_fence_does_not_change_task_identity(tmp_path):
    task = rust_exercise(tmp_path, starter="pub struct Starter;\n", fixed="")
    assert not any(path.startswith(PRIVATE) for path in task.verifier_files)


@pytest.mark.parametrize(
    "scenario, stage, failed_cases, expected_cases",
    [
        ("pass", None, [], [FIRST, SECOND, "functional"]),
        ("failed", "tests", [SECOND], [FIRST, SECOND, "functional"]),
        ("ignored", "execution", [SECOND], [FIRST, SECOND, "functional"]),
        ("missing", "execution", [], [FIRST, "functional"]),
        ("control", "compilation", [], ["functional"]),
    ],
)
def test_driver_checks_candidate_rlib_and_requires_every_doc_result(
    tmp_path, monkeypatch, capsys, scenario, stage, failed_cases, expected_cases
):
    task = rust_exercise(tmp_path / "upstream")
    for path, content in task.verifier_files.items():
        destination = tmp_path / path
        destination.parent.mkdir(exist_ok=True, parents=True)
        destination.write_text(content)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["driver", "rust"])

    rlib = tmp_path / "target/debug/deps/libreact-abcd.rlib"
    rlib.parent.mkdir(parents=True)
    rlib.touch()
    manifest = json.loads((tmp_path / MANIFEST).read_text())
    cases = list(manifest["cases"])
    seen = []

    def simulate(argv, **kwargs):
        seen.append(argv[0])
        if argv[0] == "cargo":
            assert argv[1:6] == ("test", "--offline", "--all-features", "--test", "*")
            artifact = {
                "reason": "compiler-artifact",
                "manifest_path": str(tmp_path / "Cargo.toml"),
                "target": {"name": "react", "kind": ["lib"]},
                "profile": {"test": False},
                "filenames": [str(rlib)],
            }
            output = json.dumps(artifact) + "\n\ntest functional ... ok\n"
            return subprocess.CompletedProcess(argv, 0, stdout=output)
        assert f"react={rlib}" in argv
        assert "dependency=target/debug/deps" in argv
        assert "--edition=2015" in argv
        if argv[0] == "rustc":
            code = int(scenario == "control")
            return subprocess.CompletedProcess(
                argv, code, stdout="error[E0432]: missing API\n" if code else ""
            )
        assert argv[0] == "rustdoc" and "--test" in argv
        output = f"test {PRIVATE}harness.rs - {cases[0]} (line 3) ... ok\n"
        status = {"pass": "ok", "failed": "FAILED", "ignored": "ignored"}
        if scenario != "missing":
            output += f"test {PRIVATE}harness.rs - {cases[1]} (line 10) ... {status[scenario]}\n"
        passed = 2 if scenario == "pass" else 1
        failed = int(scenario == "failed")
        ignored = int(scenario == "ignored")
        filtered = int(scenario == "missing")
        result = "FAILED" if failed else "ok"
        output += (
            f"test result: {result}. {passed} passed; {failed} failed; {ignored} ignored; "
            f"0 measured; {filtered} filtered out; finished in 0.01s\n"
        )
        return subprocess.CompletedProcess(argv, failed, stdout=output)

    monkeypatch.setattr(subprocess, "run", simulate)
    exec(compile(DRIVER, "test-rust-driver", "exec"), {})
    output = capsys.readouterr().out
    verdict = json.loads(output.splitlines()[-1].removeprefix("WEYRA_VERDICT "))
    assert verdict["case_ids"] == expected_cases
    assert verdict["failure_stage"] == stage
    assert verdict["failed_case_ids"] == failed_cases
    assert bool(verdict["code"] or verdict["failed"]) == (scenario != "pass")
    assert seen == (["cargo", "rustc"] if scenario == "control" else ["cargo", "rustc", "rustdoc"])
    if scenario == "control":
        assert "error[E0432]: missing API" in output


@pytest.mark.parametrize("slug", ["decimal", "fizzy", "example"])
def test_reference_owned_unit_tests_are_excluded(tmp_path, monkeypatch, capsys, slug):
    task = rust_exercise(tmp_path / "upstream", slug, starter="", fixed="")
    marker = "_weyra_rust_test_targets.json"
    assert marker in task.metadata()["verifier_sha256"]
    assert marker not in task.files
    (tmp_path / marker).write_text(task.verifier_files[marker])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["driver", "rust"])
    seen = []

    def simulate(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="test official_integration ... ok\n")

    monkeypatch.setattr(subprocess, "run", simulate)
    exec(compile(DRIVER, "test-rust-driver", "exec"), {})
    assert seen == [
        (
            "cargo",
            "test",
            "--offline",
            "--all-features",
            "--test",
            "*",
            "--",
            "--test-threads=1",
        )
    ]
    output = capsys.readouterr().out.splitlines()[-1]
    assert json.loads(output.removeprefix("WEYRA_VERDICT "))["case_ids"] == ["official_integration"]

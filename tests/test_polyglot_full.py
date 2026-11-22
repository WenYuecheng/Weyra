"""Offline checks for the fixed denominator and special exercise contract."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from benchmarks.polyglot_full import glm_full
from benchmarks.polyglot_full.glm_full import COUNTS, report, schedule
from benchmarks.polyglot_full.glm_verify import DRIVER


def outcomes():
    """Give Weyra one gain in each language with 225 fully scored pairs."""
    return [
        {
            "id": f"{lang}/case-{index}",
            "A": {"status": "fail" if index == 0 else "pass"},
            "R": {"status": "pass"},
        }
        for lang, count in COUNTS.items()
        for index in range(count)
    ]


def test_goal_requires_full_coverage_six_gains_and_no_regressions():
    rows = outcomes()
    complete = report(rows)
    assert complete["target_met"]
    assert complete["all"]["paired"] == 225
    assert complete["all_tasks_solved_by_weyra"]

    rows[-1]["R"] = {"status": "infrastructure_error"}
    incomplete = report(rows)
    assert not incomplete["target_met"]
    assert incomplete["all"]["planned"] == 225
    assert incomplete["all"]["paired"] == 224

    rows[-1]["R"] = {"status": "fail"}
    regression = report(rows)
    assert regression["coverage_complete"]
    assert not regression["target_met"]
    assert regression["all"]["losses"] == 1


def test_scheduled_order_has_225_unique_pairs():
    order = schedule([row["id"] for row in outcomes()])
    assert len(order) == len({task for task, _ in order}) == 225
    for language in COUNTS:
        arms = [arms[0] for task, arms in order if task.startswith(language + "/")]
        assert abs(arms.count("A") - arms.count("R")) <= 1


@pytest.mark.parametrize(
    "mode, expected_failures",
    [
        ("working", []),
        ("empty", [f"counter-implementation-{i}" for i in (4, 1, 2, 3)]),
        ("missed_mutant", ["counter-implementation-2"]),
        ("compile_failure", [f"counter-implementation-{i}" for i in (4, 1, 2, 3)]),
    ],
)
def test_counter_requires_assertions_and_all_mutants(
    tmp_path, monkeypatch, capsys, mode, expected_failures
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["driver", "go-counter"])
    for name in ("GOTMPDIR", "GOCACHE", "COUNTER_IMPL"):
        monkeypatch.setenv(name, "")
    seen = []

    def simulate(argv, **kwargs):
        assert argv[:2] == ("go", "test")
        index = int(os.environ["COUNTER_IMPL"])
        seen.append(index)
        if mode == "empty":
            rows, code = [{"Action": "pass"}], 0
        elif mode == "compile_failure":
            rows, code = [{"Action": "fail", "FailedBuild": True}], 1
        else:
            passed = index == 4 or (mode == "missed_mutant" and index == 2)
            rows, code = (
                [{"Action": "pass" if passed else "fail", "Test": "TestCounter"}],
                int(not passed),
            )
        return subprocess.CompletedProcess(
            argv, code, stdout="".join(json.dumps(e) + "\n" for e in rows)
        )

    monkeypatch.setattr(subprocess, "run", simulate)
    exec(compile(DRIVER, "test-counter-driver", "exec"), {})
    assert seen == [4, 1, 2, 3]
    line = capsys.readouterr().out.splitlines()[-1]
    verdict = json.loads(line.removeprefix("WEYRA_VERDICT "))
    assert verdict["failed_case_ids"] == sorted(expected_failures)
    assert verdict["failed"] is bool(expected_failures)


def test_prepare_retries_failed_grader_and_reuses_verified_grader(tmp_path, monkeypatch):
    images = {"python": "image"}
    task = SimpleNamespace(language="python")
    monkeypatch.setattr(glm_full.campaign, "check_identity", lambda task, image: image)
    checks = iter([RuntimeError("Docker unreachable"), {"status": "stub_passes", "cases": ["case"]}])

    def check(task, image, folder):
        value = next(checks)
        if isinstance(value, RuntimeError):
            raise value
        return value

    monkeypatch.setattr(glm_full.campaign, "preflight_one", check)
    tasks = {"python/example": task}
    first = glm_full.prepare(tmp_path, tasks, images)
    assert glm_full.runnable_checks(first, tasks, images)[1] == {"python/example": "environment_error"}

    recovered = glm_full.prepare(tmp_path, tasks, images)
    assert glm_full.runnable_checks(recovered, tasks, images) == (
        {"python/example": {"image": "image", "cases": ["case"]}},
        {},
    )
    assert glm_full.prepare(tmp_path, tasks, images) == recovered


def test_glm_profile_is_sent_with_the_configured_output_limit(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from benchmarks.polyglot_full.glm_campaign import Ledger, runtime_for
    from benchmarks.polyglot_full.glm_full import PROFILE
    from weyra.contracts import PreparedRequest

    captured = []

    async def request(**kwargs):
        captured.append(kwargs)

        async def stream():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(finish_reason="stop", delta=SimpleNamespace(content="ready"))
                ],
                usage={"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
            )

        return stream()

    async def collect(provider, envelope):
        return [event async for event in provider.stream(envelope, None)]

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
    with Ledger(tmp_path / "usage.db", 100_000) as ledger:
        runtime, provider = runtime_for(client, ledger, "example", PROFILE)
        model = runtime.resolve()
        envelope = PreparedRequest(model, "system", (), parameters=runtime.parameters_for())
        events = asyncio.run(collect(provider, envelope))
        assert ledger.totals() == (11, 1, 0)
    assert [event.kind for event in events][-1] == "response"
    assert captured[0]["reasoning_effort"] == "max"
    assert captured[0]["temperature"] == 1.0
    assert captured[0]["top_p"] == 0.95
    assert captured[0]["max_tokens"] == 32_768


def test_prepare_cli_freezes_source_and_runs_preflight(tmp_path, monkeypatch):
    tasks = {"python/example": SimpleNamespace(language="python")}
    images = dict.fromkeys(glm_full.LANGUAGES, "image")
    monkeypatch.setattr(sys, "argv", ["glm_full", "prepare", "--upstream", str(tmp_path),
                                     "--output", str(tmp_path / "output")])
    monkeypatch.setattr(glm_full, "dataset_at", lambda _: tasks)
    monkeypatch.setattr(glm_full.campaign, "image_ids", lambda _: images)
    monkeypatch.setattr(glm_full.campaign, "check_identity", lambda task, image: image)
    monkeypatch.setattr(glm_full.campaign, "preflight_one",
                        lambda *args: {"status": "eligible", "cases": ["case"]})

    glm_full.main()

    frozen = json.loads((tmp_path / "output/full-preflight-source.json").read_text())
    assert len(frozen["source"]) == 64
    assert frozen["images"] == images
    checks = json.loads((tmp_path / "output/full-preflight.json").read_text())
    assert checks["python/example"]["result"]["cases"] == ["case"]


@pytest.mark.parametrize("language", ["python", "javascript", "go", "rust", "cpp", "java"])
@pytest.mark.parametrize("expected, verdict", [(["case"], "fail"), (None, "environment_error")])
def test_verifier_timeout_is_submission_failure_only_after_preflight(
    tmp_path, monkeypatch, language, expected, verdict
):
    from benchmarks.polyglot_full import glm_verify, runner

    class TimedOutSandbox:
        def __init__(self, *args, **kwargs):
            pass

        def create(self, files):
            pass

        def inspect(self):
            return {}

        def execute(self, argv, timeout=60):
            return {"returncode": 124, "stdout": "", "stderr": ""}

        def export(self, paths):
            raise RuntimeError("Container stopped after verifier timeout")

        def close(self):
            pass

    task = SimpleNamespace(language=language, id=language + "/example", editable=("solution",),
                           verifier_files={"tests": "immutable"}, command=("test",))
    for module in (runner, glm_verify):
        monkeypatch.setattr(module, "Sandbox", TimedOutSandbox)
        monkeypatch.setattr(module, "isolation", lambda _: {})
    observed, stats = glm_verify.verify_task(task, "image", {"solution": "loop"},
                                            tmp_path, expected)
    assert observed == verdict
    assert stats["timed_out"] is True


def test_public_feedback_preserves_assertion_diagnostics_and_bounds_output(tmp_path):
    from benchmarks.polyglot_full.glm_verify import test_output_feedback

    (tmp_path / "stdout.txt").write_text("Compiler diagnostic\n" + "x" * 20000)
    (tmp_path / "stderr.txt").write_text(
        'Expected substring: "Queen must be placed on the board"\nReceived: "invalid"'
    )
    result = test_output_feedback(tmp_path)
    assert "Compiler diagnostic" in result
    assert 'Expected substring: "Queen must be placed on the board"' in result
    assert len(result) <= 12000
    assert "truncated" in result


def test_cpp_assertion_xml_reaches_public_feedback(tmp_path, monkeypatch):
    from benchmarks.polyglot_full import glm_verify

    class Workspace:
        def __init__(self, *args, **kwargs):
            pass
        def create(self, files):
            self.files = files
        def inspect(self):
            return {}
        def execute(self, argv, timeout=60):
            if argv[0] == "cat":
                output = '<testsuite><testcase classname="Case" name="answer"><failure message="Expected 42, actual 7">answer() == 42</failure></testcase></testsuite>'
            else:
                output = 'WEYRA_VERDICT ' + json.dumps({"code": 1, "failed": True,
                    "case_ids": ["Case::answer"], "failed_case_ids": ["Case::answer"]})
            return {"returncode": 0, "stdout": output, "stderr": ""}
        def export(self, paths):
            return {name: self.files[name].encode() for name in paths}
        def close(self):
            pass

    monkeypatch.setattr(glm_verify, "Sandbox", Workspace)
    monkeypatch.setattr(glm_verify, "isolation", lambda _: {})
    task = SimpleNamespace(language="cpp", id="cpp/example", editable=("answer.cpp",),
                           verifier_files={"answer_test.cpp": "immutable"})
    outcome, _ = glm_verify.verify_task(task, "image", {"answer.cpp": "wrong"}, tmp_path,
                                      ["Case::answer"])
    assert outcome == "fail"
    diagnostic = glm_verify.test_output_feedback(tmp_path)
    assert "Expected 42, actual 7" in diagnostic
    assert "answer() == 42" in diagnostic


def test_pool_refills_while_an_earlier_pair_is_still_running():
    import asyncio

    async def example():
        release = asyncio.Event()
        started = []

        async def group(task_id, arms):
            started.append(task_id)
            if task_id == "0":
                await release.wait()
            if task_id == "4":
                release.set()

        ledger = SimpleNamespace(totals=lambda: (0, 0, 0))
        jobs = [(str(i), ["A", "R"]) for i in range(5)]
        result = await asyncio.wait_for(glm_full.dispatch(
            jobs, group, ledger, 10_000_000, 4, asyncio.Event(), lambda: None
        ), timeout=2)
        assert result == "completed"
        assert len(started) == 5

    asyncio.run(example())


def test_pool_never_starts_a_pair_without_its_budget():
    import asyncio

    async def group(*args):
        pytest.fail("No model work should be started")

    ledger = SimpleNamespace(totals=lambda: (0, 0, 0))
    result = asyncio.run(glm_full.dispatch(
        [("task", ["A", "R"])], group, ledger, 1, 4, asyncio.Event(), lambda: None
    ))
    assert result == "campaign_budget"


def test_run_cli_freezes_source_tools_and_full_task_metadata(tmp_path, monkeypatch):
    from benchmarks.polyglot_full.dataset import Task

    task = Task(
        "python/example", "python", {"answer.py": "starter"}, {"tests.py": "test"},
        {"answer.py": "reference"}, ("answer.py",), ("pytest",), "Implement the answer.",
    )
    tasks = {task.id: task}
    images = dict.fromkeys(glm_full.LANGUAGES, "image")
    output = tmp_path / "output"
    argv = ["glm_full", "prepare", "--upstream", str(tmp_path), "--output", str(output)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(glm_full, "dataset_at", lambda _: tasks)
    monkeypatch.setattr(glm_full.campaign, "image_ids", lambda _: images)
    monkeypatch.setattr(glm_full.campaign, "preflight_one",
                        lambda *args: {"status": "eligible", "cases": ["case"]})

    async def no_model_run(*args):
        pass

    monkeypatch.setattr(glm_full, "run", no_model_run)
    glm_full.main()

    argv[1] = "run"
    argv.extend(["--token-cap", str(glm_full.TASK_TOKENS * len(glm_full.ARMS))])
    glm_full.main()
    glm_full.main()

    frozen = json.loads((output / "full-protocol.json").read_text())
    assert frozen["source"] == glm_full.campaign.source_hash()
    assert frozen["tool_names"] == list(glm_full.campaign.TOOL_NAMES)
    assert frozen["tasks"] == {task.id: task.metadata()}


@pytest.mark.parametrize("reuse_enabled", [True, False])
def test_report_cli_identifies_frozen_baseline_reuse(tmp_path, monkeypatch, capsys, reuse_enabled):
    tasks = {"python/example": SimpleNamespace(language="python")}
    compact = {
        "source": str(tmp_path / "older-baseline"),
        "protocol_sha256": "protocol-digest",
        "report_sha256": "report-digest",
        "accounted_tokens_from_original_campaign": 1234,
        "reused_arms": ["A"],
        "reused_task_count": 1,
    }
    protocol = {}
    if reuse_enabled:
        protocol["baseline_reuse"] = {
            **{key: value for key, value in compact.items() if key != "reused_task_count"},
            "outcomes": {"python/example": {"files": {"attempt-1/result.json": "digest"}}},
        }
    (tmp_path / "full-protocol.json").write_text(json.dumps(protocol))
    monkeypatch.setattr(sys, "argv", ["glm_full", "report", "--upstream", str(tmp_path),
                                     "--output", str(tmp_path)])
    monkeypatch.setattr(glm_full, "dataset_at", lambda _: tasks)

    glm_full.main()

    report = json.loads((tmp_path / "full-report.json").read_text())
    printed = json.loads(capsys.readouterr().out)
    if reuse_enabled:
        assert report["baseline_reuse"] == compact
        assert printed["baseline_reuse"] == compact
    else:
        assert not report.get("baseline_reuse")
        assert not printed.get("baseline_reuse")
    assert printed["planned"] == report["all"]["planned"]

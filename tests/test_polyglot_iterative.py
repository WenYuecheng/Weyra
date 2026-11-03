import asyncio
import json
from decimal import Decimal

import pytest

from benchmarks.polyglot.iterative import solve, run_independent


class Budget:
    def __init__(self):
        self.cost = Decimal(0)
        self.calls = 0

    def begin(self, *args, **kwargs):
        pass

    def task_total(self, task):
        return self.cost

    def task_calls(self, task):
        return self.calls

    def task_remaining(self, task):
        return Decimal(".95") - self.cost


def test_continuous_feedback_third_submission_passes(tmp_path):
    budget = Budget()
    seen = []

    async def attempt(index, feedback, seconds, steps):
        seen.append(feedback)
        budget.cost += Decimal(".1")
        budget.calls += 1
        if index:
            assert (tmp_path / f"rounds/{index - 1:04d}/result.json").exists()
        return {"agent_end": "normal", "model_steps": 1}, {"a.py": str(index).encode()}

    async def grade(files, folder, seconds):
        number = int(files["a.py"])
        return {
            "verdict": "pass" if number == 2 else "fail",
            "classification": "passed" if number == 2 else "assertion_failure",
            "eligible_feedback": number != 2,
            "feedback": "fix" + str(number),
        }

    row = asyncio.run(solve("python/a", tmp_path, "p", budget, attempt, grade))
    assert row["status"] == "passed" and len(row["rounds"]) == 3
    assert seen == ["", "fix0", "fix1"]
    assert row["cost_cny"] == "0.3"
    again = asyncio.run(solve("python/a", tmp_path, "p", budget, attempt, grade))
    assert again == row and len(seen) == 3


def test_sealed_submission_recovered_without_repeating_model(tmp_path):
    budget = Budget()
    calls = []

    async def attempt(index, feedback, seconds, steps):
        calls.append(index)
        budget.cost += Decimal(".1")
        return {"agent_end": "normal", "model_steps": 1}, {"a.py": b"ok"}

    async def interrupted_grade(*args):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(solve("python/a", tmp_path, "p", budget, attempt, interrupted_grade))

    async def grade(*args):
        return {"verdict": "pass", "classification": "passed"}

    row = asyncio.run(solve("python/a", tmp_path, "p", budget, attempt, grade))
    assert row["status"] == "passed" and calls == [0]


def test_infrastructure_verifier_retry_is_not_model_feedback(tmp_path):
    budget = Budget()
    models = []
    grades = []

    async def attempt(index, *args):
        models.append(index)
        budget.calls += 1
        return {"agent_end": "normal", "model_steps": 1}, {"a.py": b"ok"}

    async def grade(*args):
        grades.append(1)
        return (
            {"verdict": "environment_error", "classification": "verification_error"}
            if len(grades) < 3
            else {"verdict": "pass", "classification": "passed"}
        )

    row = asyncio.run(solve("python/a", tmp_path, "p", budget, attempt, grade, retry_delay=0))
    assert row["status"] == "passed" and models == [0] and len(grades) == 3


def test_task_failure_and_reporting_failure_do_not_cancel_peers():
    calls = []

    async def worker(value):
        if value == 1:
            raise RuntimeError("one case failed")
        await asyncio.sleep(0.01)
        calls.append(value)
        return value

    def report(*args):
        raise RuntimeError("summary failed")

    result = asyncio.run(run_independent([1, 2, 3], worker, report))
    assert calls == [2, 3] and len(result) == 3


def test_budget_exhaustion_still_verifies_latest_candidate(tmp_path):
    budget = Budget()
    calls = []

    async def attempt(index, *args):
        budget.cost = Decimal(".95")
        budget.calls = 1
        return {"agent_end": "budget_stopped", "model_steps": 1}, {"a.py": b"candidate"}

    async def grade(*args):
        calls.append(1)
        return {"verdict": "pass", "classification": "passed"}

    row = asyncio.run(solve("python/a", tmp_path, "p", budget, attempt, grade))
    assert row["status"] == "passed" and calls == [1]
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "passed"


@pytest.mark.skipif(
    __import__("os").environ.get("POLYGLOT_DOCKER_TESTS") != "1", reason="Opt-in Docker"
)
def test_docker_feedback_recovery_and_transient_request(tmp_path):
    from benchmarks.polyglot.iterative import TaskHost, configuration
    from benchmarks.polyglot.dataset import Task
    from benchmarks.polyglot.budget_v3 import IterativeBudget
    from benchmarks.polyglot.grading_v2 import verify
    from benchmarks.polyglot.feedback import awaited_thread
    from weyra.contracts import ModelResponse, ToolCall, Usage
    from weyra.llm.providers import ScriptedProvider, ModelError
    from weyra.session import SessionStore

    async def run():
        config = configuration()
        runtime = config.build_runtime()
        responses = [ModelError("transient")]
        for number, expression in enumerate(("a-b", "a*b", "a+b")):
            responses.extend(
                [
                    ModelResponse(
                        tool_calls=(ToolCall("r" + str(number), "read_file", {"path": "add.py"}),),
                        stop_reason="tool_calls",
                    ),
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "w" + str(number),
                                "write_file",
                                {
                                    "path": "add.py",
                                    "content": "def add(a,b):\n return " + expression + "\n",
                                },
                            ),
                        ),
                        stop_reason="tool_calls",
                    ),
                    ModelResponse("Saved."),
                ]
            )
        for response in responses:
            if isinstance(response, ModelResponse):
                response.usage = Usage(
                    input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0
                )
        provider = ScriptedProvider(responses)
        runtime.registry.providers["cun"] = provider
        task = Task(
            "python/development-iterative",
            "python",
            {
                "add.py": "def add(a,b):\n pass\n",
                "INSTRUCTIONS.md": "Implement the numeric sum add(a,b).",
            },
            {
                "add.py": "",
                "test_add.py": "from add import add\ndef test_add():\n assert add(2,3)==5\n",
            },
            {},
            ("add.py",),
            ("python", "-m", "pytest", "-q", "--junitxml=/tmp/junit.xml"),
            "Implement the numeric sum add(a,b).",
        )
        ledger = SessionStore(project=tmp_path / "ledger", session_id="development")
        budget = IterativeBudget()
        budget.attach(ledger)
        host = TaskHost(task, tmp_path / "task", "weyra-polyglot:20261023", config, budget, runtime)
        grades = []

        async def grade(files, folder, seconds):
            row = await awaited_thread(verify, task, "weyra-polyglot:20261023", files, folder)
            grades.append(row["verdict"])
            if len(grades) == 2:
                assert b"return a*b" in files["add.py"]
            if len(grades) == 1:
                await awaited_thread(host.box.close)
            return row

        try:
            row = await solve(
                task.id,
                tmp_path / "task",
                "dev",
                budget,
                host.attempt,
                grade,
                host.metrics,
                retry_delay=0,
            )
            assert row["status"] == "passed"
            assert grades == ["fail", "fail", "pass"]
            assert len(provider.requests) == 10
            assert task.instruction in str(provider.requests[0].messages)
            assert provider.requests[0].parameters["max_output_tokens"] == 4096
            assert host.metrics()["model_steps"] == 9
            assert budget.task_calls(task.id) == 10
            checkpoint = host.restore_files()
            assert b"return a+b" in checkpoint["add.py"]
            count = len(provider.requests)
            again = await solve(
                task.id, tmp_path / "task", "dev", budget, host.attempt, grade, host.metrics
            )
            assert again == row and len(provider.requests) == count
            (tmp_path / "development-evidence.json").write_text(
                json.dumps(
                    {"passed": True, "grades": grades, "paid_calls": 0, "row": row}, indent=2
                )
            )
        finally:
            await host.close()
            ledger.close()

    asyncio.run(run())


def test_saved_passing_verifier_survives_expired_deadline(tmp_path):
    from benchmarks.polyglot.runner import atomic

    budget = Budget()

    async def attempt(*args):
        return {"agent_end": "normal", "model_steps": 1}, {"a.py": b"ok"}

    async def interrupted_grade(files, folder, seconds):
        atomic(folder / "verification.json", {"verdict": "pass", "classification": "passed"})
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(solve("python/a", tmp_path, "p", budget, attempt, interrupted_grade))
    state = json.loads((tmp_path / "progress.json").read_text())
    state["deadline"] = 1
    atomic(tmp_path / "progress.json", state)

    async def forbidden(*args):
        raise AssertionError("No model or verifier repeats")

    row = asyncio.run(solve("python/a", tmp_path, "p", budget, forbidden, forbidden))
    assert row["status"] == "passed"


def test_frozen_input_changes_detected_before_paid_work():
    from benchmarks.polyglot.iterative import check_task
    from benchmarks.polyglot.dataset import Task

    task = Task(
        "python/a",
        "python",
        {"a.py": "pass", "INSTRUCTIONS.md": "original"},
        {"test_a.py": "test"},
        {},
        ("a.py",),
        ("pytest",),
        "original",
    )
    metadata = task.metadata()
    check_task(task, metadata)
    task.files["INSTRUCTIONS.md"] = "changed"
    with pytest.raises(ValueError):
        check_task(task, metadata)


def test_cache_reconciliation_reopens_only_remaining_resources(tmp_path):
    from benchmarks.polyglot.iterative import reopen_task
    from benchmarks.polyglot.runner import atomic

    row = {
        "id": "python/a",
        "protocol": "p",
        "status": "resource_exhausted",
        "rounds": [],
        "model_steps": 30,
        "physical_requests": 40,
        "cost_cny": ".8",
        "seconds": 120,
        "started_at": 1,
        "deadline": 721,
        "phase": "idle",
        "next_index": 0,
    }
    atomic(tmp_path / "result.json", row)
    atomic(tmp_path / "progress.json", row)
    budget = Budget()
    budget.cost = Decimal(".2")
    budget.calls = 40
    assert reopen_task(tmp_path, budget)
    state = json.loads((tmp_path / "progress.json").read_text())
    assert state["resume_remaining_seconds"] == 600
    assert state["elapsed_before"] == 120
    assert state["model_steps"] == 30 and state["physical_requests"] == 40
    assert json.loads((tmp_path / "resumptions/0000/result.json").read_text()) == row
    assert not reopen_task(tmp_path, budget)

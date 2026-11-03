import asyncio
import json

import pytest

from benchmarks.polyglot.feedback import run_stages, repair_allowed


@pytest.mark.parametrize(
    "first,expected",
    [
        (
            {
                "agent_end": "normal",
                "verification": {"verdict": "pass", "eligible_feedback": False},
            },
            False,
        ),
        (
            {"agent_end": "normal", "verification": {"verdict": "fail", "eligible_feedback": True}},
            True,
        ),
        (
            {
                "agent_end": "provider_error",
                "verification": {"verdict": "fail", "eligible_feedback": True},
            },
            False,
        ),
        (
            {
                "agent_end": "normal",
                "verification": {"verdict": "environment_error", "eligible_feedback": False},
            },
            False,
        ),
    ],
)
def test_repair_gate(first, expected):
    assert repair_allowed(first) is expected


def test_first_is_sealed_before_feedback_and_only_one_repair(tmp_path):
    calls = []

    async def attempt(stage, feedback):
        calls.append(stage)
        if stage == "repair":
            assert (
                json.loads((tmp_path / "first/result.json").read_text())["verification"]["verdict"]
                == "fail"
            )
            assert len(feedback) == 6000
        return {"agent_end": "normal", "model_steps": 1}, {"a.py": b"candidate"}

    async def grade(files, folder):
        return {"verdict": "fail", "eligible_feedback": True, "feedback": "x" * 7000}

    first = asyncio.run(run_stages(tmp_path, "fingerprint", attempt, grade))
    second = asyncio.run(run_stages(tmp_path, "fingerprint", attempt, grade))
    assert first == second
    assert calls == ["first", "repair"]
    assert (tmp_path / "first/artifacts/a.py").read_bytes() == b"candidate"


@pytest.mark.parametrize("verdict", ["pass", "environment_error"])
def test_no_unnecessary_second_attempt(tmp_path, verdict):
    calls = []

    async def attempt(stage, feedback):
        calls.append(stage)
        return {"agent_end": "normal"}, {}

    async def grade(files, folder):
        return {"verdict": verdict, "eligible_feedback": False}

    asyncio.run(run_stages(tmp_path, "p", attempt, grade))
    assert calls == ["first"]


def test_interrupted_started_task_never_reacquires_attempt(tmp_path):
    (tmp_path / "started.json").write_text(json.dumps({"protocol": "p"}))

    async def forbidden(*args):
        raise AssertionError("Must not restart an interrupted attempt")

    row = asyncio.run(run_stages(tmp_path, "p", forbidden, forbidden))
    assert row["interrupted"]
    assert row["first"]["agent_end"] == "interrupted_unknown"
    with pytest.raises(ValueError):
        asyncio.run(run_stages(tmp_path, "changed", forbidden, forbidden))


@pytest.mark.skipif(
    __import__("os").environ.get("POLYGLOT_DOCKER_TESTS") != "1", reason="Opt-in Docker"
)
def test_real_container_feedback_same_agent_session(tmp_path):
    from benchmarks.polyglot.feedback import one_task
    from benchmarks.polyglot.dataset import Task
    from benchmarks.polyglot.budget_v2 import FeedbackBudget
    from benchmarks.polyglot.runner import runtime_config, atomic
    from weyra.contracts import ModelResponse, ToolCall
    from weyra.llm.providers import ScriptedProvider

    async def run():
        config, _ = runtime_config()
        runtime = config.build_runtime()
        provider = ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=(ToolCall("r1", "read_file", {"path": "add.py"}),),
                    stop_reason="tool_calls",
                ),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "w1",
                            "write_file",
                            {"path": "add.py", "content": "def add(a,b):\n return a-b\n"},
                        ),
                    ),
                    stop_reason="tool_calls",
                ),
                ModelResponse("First saved."),
                ModelResponse(
                    tool_calls=(ToolCall("r2", "read_file", {"path": "add.py"}),),
                    stop_reason="tool_calls",
                ),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "w2",
                            "write_file",
                            {"path": "add.py", "content": "def add(a,b):\n return a+b\n"},
                        ),
                    ),
                    stop_reason="tool_calls",
                ),
                ModelResponse("Repair saved."),
            ]
        )
        runtime.registry.providers["cun"] = provider
        task = Task(
            "python/development-feedback",
            "python",
            {"INSTRUCTIONS.md": "Implement add(a,b).", "add.py": "def add(a,b):\n pass\n"},
            {
                "add.py": "",
                "test_add.py": "from add import add\ndef test_sum():\n assert add(2,3)==5\n",
            },
            {},
            ("add.py",),
            ("python", "-m", "pytest", "-q", "--junitxml=/tmp/junit.xml"),
            "Implement add(a,b).",
        )
        budget = FeedbackBudget()
        row = await one_task(
            task, tmp_path, "weyra-polyglot:20261023", config, budget, "dev", None, "check", runtime
        )
        assert row["first"]["verification"]["verdict"] == "fail"
        assert row["repair"]["verification"]["verdict"] == "pass"
        assert row["first"]["model_steps"] == row["repair"]["model_steps"] == 3
        assert len(provider.requests) == 6
        assert any(
            "official failure report" in m.content.lower() for m in provider.requests[3].messages
        )
        assert "First saved." in str(provider.requests[3].messages)
        before = len(provider.requests)
        resumed = await one_task(
            task, tmp_path, "weyra-polyglot:20261023", config, budget, "dev", None, "check", runtime
        )
        assert resumed == row
        assert len(provider.requests) == before
        atomic(
            tmp_path / "offline-feedback-check.json", {"passed": True, "row": row, "paid_calls": 0}
        )
        await runtime.close()

    asyncio.run(run())


def test_paid_run_requires_successful_current_checks(tmp_path):
    from benchmarks.polyglot.feedback import validate_checks
    from benchmarks.polyglot.runner import atomic

    atomic(
        tmp_path / "check-result.json",
        {"protocol": "p", "first": {"verification": {"verdict": "fail"}}},
    )
    atomic(tmp_path / "offline-feedback-check.json", {"passed": True, "source_hash": "source"})
    with pytest.raises(ValueError):
        validate_checks(tmp_path, "p", "source")
    atomic(
        tmp_path / "check-result.json",
        {"protocol": "old", "first": {"verification": {"verdict": "pass"}}},
    )
    with pytest.raises(ValueError):
        validate_checks(tmp_path, "p", "source")
    atomic(
        tmp_path / "check-result.json",
        {"protocol": "p", "first": {"verification": {"verdict": "pass"}}},
    )
    validate_checks(tmp_path, "p", "source")
    with pytest.raises(ValueError):
        validate_checks(tmp_path, "p", "changed")


def test_report_accepts_unknown_usage_without_inventing_tokens(tmp_path):
    from benchmarks.polyglot.feedback import report
    from benchmarks.polyglot.budget_v2 import FeedbackBudget

    row = {
        "id": "python/a",
        "first": {
            "agent_end": "timeout",
            "model_steps": 2,
            "physical_requests": 1,
            "usage": [{"input_tokens": None, "output_tokens": None}],
            "verification": {"verdict": "fail"},
        },
        "repair": None,
    }
    s = report(tmp_path, [row], FeedbackBudget())
    assert s["input_tokens"] == 0 and s["output_tokens"] == 0
    assert s["unknown_usage_requests"] == 1


def test_workers_are_drained_before_caller_closes_ledger():
    from benchmarks.polyglot.feedback import gather_workers

    closed = []

    async def run():
        async def failing():
            await asyncio.sleep(0.01)
            raise RuntimeError("report failed")

        async def worker():
            try:
                await asyncio.sleep(10)
            finally:
                await asyncio.sleep(0.01)
                assert not closed

        with pytest.raises(RuntimeError):
            await gather_workers([failing(), worker()])
        closed.append(True)

    asyncio.run(run())


def test_cancelled_thread_finishes_before_resource_cleanup():
    from benchmarks.polyglot.feedback import awaited_thread
    import time

    finished = []

    async def run():
        def create():
            time.sleep(0.03)
            finished.append(True)

        task = asyncio.create_task(awaited_thread(create))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished

    asyncio.run(run())

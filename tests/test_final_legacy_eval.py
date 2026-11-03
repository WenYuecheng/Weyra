import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from weyra import final_legacy_eval as legacy


def test_legacy_selection_is_exact_and_originals_are_read_only():
    finish = legacy._frozen_module(
        legacy.FINISH_FREEZE, "_weyra_test_legacy_finish", "finish_evaluation"
    )
    core_corpus = legacy._frozen_module(
        legacy.CORE_FREEZE, "_weyra_test_legacy_core", "core_corpus"
    )
    finish_manifest = finish.load_manifest()
    core_manifest = core_corpus.load_core_manifest()
    loc_plan = [row for row in legacy._finish_plan(finish_manifest)
                if row["group"] == "localization"]
    skill_plan = [row for row in legacy._finish_plan(finish_manifest)
                  if row["group"] == "skill_test"]
    progressive = legacy._core_progressive_plan(core_manifest)
    finish_originals = json.loads(legacy.FINISH_ORIGINALS.read_text())

    assert len(loc_plan) == 96
    assert len(skill_plan) == 48
    assert len([row for row in loc_plan if row["key"] not in finish_originals]) == 83
    assert len([row for row in loc_plan if row["key"] in finish_originals]) == 13
    assert len([key for key, row in finish_originals.items()
                if key.startswith("localization/") and row.get("status") in legacy.INFRASTRUCTURE]) == 4
    assert len([key for key, row in finish_originals.items()
                if key.startswith("skill_test/") and row.get("status") in legacy.INFRASTRUCTURE]) == 7
    assert len(progressive) == 54
    assert {(row["cluster"], row["variant"], row["repeat"])
            for row in progressive} == {
                (family["id"], arm, repeat)
                for family in core_manifest["evolution_families"]
                for arm in ("old_old", "old_new", "new_new")
                for repeat in range(3)
            }


def test_frozen_modules_import_from_archived_package_namespace():
    old_finish = legacy._frozen_module(
        legacy.FINISH_FREEZE, "_weyra_test_frozen_namespace", "finish_evaluation"
    )
    assert old_finish.__name__ == "_weyra_test_frozen_namespace.finish_evaluation"
    assert Path(old_finish.__file__).resolve().is_relative_to(legacy.FINISH_FREEZE.resolve())
    assert old_finish.FinishRunner.__module__.startswith("_weyra_test_frozen_namespace")


def test_source_freeze_hashes_match_archived_snapshots():
    assert len(legacy._snapshot_integrity(legacy.FINISH_FREEZE, legacy.FINISH_SNAPSHOT)) > 0
    assert len(legacy._snapshot_integrity(legacy.CORE_FREEZE, legacy.CORE_SNAPSHOT)) > 0


def test_run_refuses_non_100_cny_budget_before_creating_output(tmp_path):
    class Budget:
        cost_limit_cny = Decimal("50")

    output = tmp_path / "run"
    with pytest.raises(ValueError, match="100 CNY"):
        asyncio.run(legacy.run_legacy(object(), Budget(), output, tmp_path / "ledger"))
    assert not output.exists()


def _current_scripted_runtime(responses):
    from weyra.contracts import ModelInfo
    from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider

    registry = ProviderRegistry()
    provider = ScriptedProvider(responses)
    registry.register("test", provider)
    model = ModelInfo("test", "m", 20000, 2048, input_price_per_million="1",
                      output_price_per_million="1")
    runtime = ModelRuntime(registry, {"main": model})
    runtime.pricing = {"test/m": {"currency": "CNY", "input_per_million": "1",
                                   "output_per_million": "1"}}
    return runtime, provider


def test_frozen_finish_task_runs_current_runtime_tool_calls_without_network(tmp_path):
    import asyncio

    from weyra.contracts import ModelResponse, ToolCall, Usage
    from weyra.runtime_state import Budget

    finish = legacy._frozen_module(
        legacy.FINISH_FREEZE, "_weyra_test_runtime_finish", "finish_evaluation"
    )
    case = finish.load_manifest()["families"][0]["tasks"][0]
    expected = json.dumps(finish.expected_output(case))
    runtime, provider = _current_scripted_runtime([
        ModelResponse(tool_calls=(ToolCall("skill", "skill", {"name": case["family"]}),
                                    ToolCall("read", "read_file", {"path": "input.json"})),
                      usage=Usage(20, 1)),
        ModelResponse(tool_calls=(ToolCall("write", "write_file", {
            "path": "result.json", "content": expected}),), usage=Usage(20, 1)),
        ModelResponse("done", usage=Usage(5, 1)),
    ])
    budget = Budget(cost_limit_cny="100")
    runner = finish.FinishRunner(tmp_path / "legacy-finish", runtime, budget,
                                 ledger_root=tmp_path / "finish-ledger", max_concurrency=1)
    try:
        result = asyncio.run(runner.task(case, "Use skill and file tools", tmp_path / "task"))
        assert result["status"] == "pass"
        assert {tool.get("name", tool.get("function", {}).get("name"))
                for tool in provider.requests[0].tools} >= {
            "skill", "read_file", "write_file"
        }
        assert len(provider.requests) == 3
    finally:
        runner.close()


def test_frozen_progressive_task_runs_current_runtime_tool_calls_without_network(tmp_path):
    import asyncio

    from weyra.contracts import ModelResponse, ToolCall, Usage
    from weyra.runtime_state import Budget

    core = legacy._frozen_module(
        legacy.CORE_FREEZE, "_weyra_test_runtime_core", "core_evaluation"
    )
    corpus = legacy._frozen_module(
        legacy.CORE_FREEZE, "_weyra_test_runtime_core", "core_corpus"
    )
    case = next(task for task in corpus.load_core_manifest()["evolution_families"][0]["tasks"]
                if task["split"] == "test")
    expected = json.dumps(case["grading"]["expected"])
    runtime, provider = _current_scripted_runtime([
        ModelResponse(tool_calls=(ToolCall("skill", "skill", {"name": case["family"]}),),
                      usage=Usage(20, 1)),
        ModelResponse(expected, usage=Usage(20, 1)),
    ])
    budget = Budget(cost_limit_cny="100")
    result = asyncio.run(core.progressive_infer(
        case, "Read input.json and return the expected object", case["family"],
        tmp_path / "progressive-task", runtime, budget, new_locator=True))
    assert result["status"] == "pass"
    assert result["skill_loaded"] is True
    assert len(provider.requests) == 2


def test_frozen_core_runner_keeps_shared_finish_ledger_attached(tmp_path):
    from weyra.runtime_state import Budget
    from weyra.session import SessionStore

    core = legacy._frozen_module(
        legacy.CORE_FREEZE, "_weyra_test_ledger_core", "core_evaluation"
    )
    budget = Budget(cost_limit_cny="100")
    root_ledger = SessionStore(project=tmp_path / "ledger", session_id="finish-live-cny100")
    budget.attach(root_ledger)
    runner = core.CoreRunner(tmp_path / "progressive", object(), budget,
                             repeats=3, ledger_root=None)
    try:
        assert budget._journal is root_ledger
        assert runner.ledger is runner.journal
        assert runner.journal.session.session_id == "core-eval-controller"
        assert runner.journal.session.project_path != root_ledger.project_path
    finally:
        runner.journal.close()
        root_ledger.close()


def test_frozen_scoped_runtime_forwards_attempt_budget_by_identity():
    from weyra.contracts import ModelResponse

    finish = legacy._frozen_module(
        legacy.FINISH_FREEZE, "_weyra_test_attempt_finish", "finish_evaluation"
    )
    marker = {"attempts": 0, "limit": 1}

    class Runtime:
        async def complete(self, request, context, *, attempt_budget=None, **kwargs):
            assert attempt_budget is marker
            return ModelResponse("ok")

    class Session:
        def append(self, *args, **kwargs):
            pass

    from types import SimpleNamespace
    context = SimpleNamespace(budget_scope=None, session=Session())

    request = SimpleNamespace(id="request-1", purpose="main")
    result = asyncio.run(finish.ScopedRuntime(Runtime()).complete(
        request, context, attempt_budget=marker))
    assert result.content == "ok"

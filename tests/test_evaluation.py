import asyncio
import json
from pathlib import Path

from weyra.runtime_state import Budget


def test_twenty_cases_have_fixtures_objectives_and_machine_checks():
    cases = json.loads(Path("data/scenarios.json").read_text())
    assert len(cases) == len({case["id"] for case in cases}) == 20
    assert all(case["fixtures"] and case["objective"] and case["expected"] for case in cases)


def test_offline_baseline_executes_all_twenty_and_resumes(tmp_path):
    from weyra.evaluation import run_evaluation

    async def run():
        report = await run_evaluation(project=tmp_path, groups=[])
        assert report["mode"] == "offline"
        assert report["quality_claim"] is False
        assert report["baseline"]["denominator"] == 20
        assert len(report["baseline"]["cases"]) == 20
        assert {case["status"] for case in report["baseline"]["cases"]} <= {
            "pass",
            "fail",
            "error",
            "budget_stopped",
        }
        assert report["baseline"]["counts"]["pass"] == 20
        resumed = await run_evaluation(project=tmp_path, groups=[])
        assert resumed["resumed_cases"] == 20
        assert resumed["new_model_calls"] == 0

    asyncio.run(run())


def test_budget_stopped_cases_stay_in_fixed_denominator(tmp_path):
    from weyra.evaluation import run_evaluation

    async def run():
        report = await run_evaluation(project=tmp_path, groups=[], budget=Budget(call_limit=0))
        assert report["baseline"]["denominator"] == 20
        assert report["baseline"]["counts"]["budget_stopped"] == 20
        assert report["baseline"]["success_rate"] == 0

    asyncio.run(run())


def test_done_text_is_not_a_success_check(tmp_path):
    from weyra.evaluation import check_case, load_cases

    case = load_cases()[0]
    assert not all(check_case(case, tmp_path, "done", []).values())


def test_missing_protected_and_edited_files_are_failed_checks(tmp_path):
    from weyra.evaluation import check_case

    case = {
        "expected": {},
        "reads": [],
        "fixtures": {"protected.txt": "keep"},
        "edits": [{"path": "missing.py", "new_text": "replacement"}],
    }
    checks = check_case(case, tmp_path, "done", [])
    assert checks["protected_file_unchanged"] is False
    assert checks["edit_missing.py"] is False


def test_all_mechanisms_execute_distinct_paths_and_account_auxiliary_usage(tmp_path):
    from weyra.evaluation import run_evaluation

    async def run():
        report = await run_evaluation(project=tmp_path)
        assert report["baseline"]["counts"]["pass"] == 20
        total = 20
        for group in report["groups"].values():
            for variant in group["variants"].values():
                assert variant["counts"]["pass"] == variant["eligible_denominator"]
                total += variant["executed"]
                for case in variant["cases"]:
                    assert case["metrics"]["cache_read_tokens"] is None
                    assert case["metrics"]["usage_samples"] == case["model_calls"]
                    assert case["metrics"]["input_tokens"] == 20 * case["model_calls"]
        assert total == 50
        for variant, count in (("plain", 1), ("dsh", 1), ("three_layer", 3)):
            for case in report["groups"]["compression"]["variants"][variant]["cases"]:
                assert case["metrics"]["compacted"] is True
                assert case["metrics"]["usage_by_purpose"]["compression"]["calls"] == count
        for case in report["groups"]["memory"]["variants"]["baseline"]["cases"]:
            assert case["metrics"]["legacy_recall_injections"] == 0

    asyncio.run(run())


def test_resume_identity_changes_with_experiment_configuration(tmp_path):
    from weyra.evaluation import run_evaluation

    async def run():
        first = await run_evaluation(project=tmp_path, groups=[], run_id="named")
        changed = await run_evaluation(project=tmp_path, groups=["memory"], run_id="named")
        assert first["run_id"] != changed["run_id"]
        assert changed["resumed_cases"] == 0

    asyncio.run(run())


def test_group_cost_sums_decimal_without_float_rounding():
    from weyra.evaluation import _summary

    assert (
        _summary([{"status": "pass", "cost_cny": "0.1"}, {"status": "fail", "cost_cny": "0.2"}])[
            "cost_cny"
        ]
        == "0.3"
    )


def test_live_evaluation_requires_shared_root_budget():
    from weyra.evaluation import run_evaluation
    import pytest

    with pytest.raises(ValueError, match="shared root budget"):
        asyncio.run(run_evaluation(offline=False, runtime=object()))


def test_runtime_code_change_does_not_reuse_old_outcomes(tmp_path, monkeypatch):
    from weyra.evaluation import run_evaluation

    async def run():
        first = await run_evaluation(project=tmp_path, groups=[])
        original = Path.read_text

        def changed_source(path, *args, **kwargs):
            content = original(path, *args, **kwargs)
            return (
                content + "\n# deployed runtime revision\n"
                if path.name == "agent_loop.py"
                else content
            )

        monkeypatch.setattr(Path, "read_text", changed_source)
        changed = await run_evaluation(project=tmp_path, groups=[])
        assert changed["run_id"] != first["run_id"]
        assert changed["resumed_cases"] == 0
        assert changed["new_model_calls"] > 0

    asyncio.run(run())


def test_case_timeout_is_recorded_without_removing_denominator(tmp_path, monkeypatch):
    from weyra.evaluation import run_evaluation, Agent

    async def slow(self, *args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(Agent, "run_once", slow)
    report = asyncio.run(run_evaluation(project=tmp_path, groups=[], case_timeout_seconds=0.001))
    assert report["baseline"]["denominator"] == 20
    assert report["baseline"]["counts"]["error"] == 20
    assert all(
        case["error"] == "TimeoutError" and case["timed_out"]
        for case in report["baseline"]["cases"]
    )


def test_checker_normalizes_successful_read_paths_inside_project(tmp_path):
    from weyra.evaluation import check_case
    (tmp_path / 'source.json').write_text('{}')
    (tmp_path / 'result.json').write_text('{}')
    case = {'expected': {}, 'fixtures': {}, 'reads': ['source.json']}
    records = [
        {'kind': 'tool_intent', 'data': {'call_id': 'r', 'name': 'read_file', 'arguments': {'path': str(tmp_path / 'source.json')}}},
        {'kind': 'tool_result', 'data': {'call_id': 'r', 'status': 'success'}},
    ]
    assert check_case(case, tmp_path, '', records)['source_evidence_read']
    records[0]['data']['arguments']['path'] = str(tmp_path.parent / 'source.json')
    assert not check_case(case, tmp_path, '', records)['source_evidence_read']


def test_cache_rebuild_changes_prefix_without_duplicating_history(tmp_path, monkeypatch):
    from weyra.evaluation import _run_case, load_cases
    from weyra.context import ContextManager
    observed = []
    original = ContextManager.refresh

    def refresh(self, system, *args, **kwargs):
        observed.append((self.system, system))
        return original(self, system, *args, **kwargs)

    monkeypatch.setattr(ContextManager, 'refresh', refresh)
    case = load_cases()[0]
    result = asyncio.run(_run_case(case, group='cache', variant='rebuild', root=tmp_path,
                                  runtime=None, budget=Budget(), offline=True))
    assert result['status'] == 'pass'
    before, after = observed[-1]
    assert before != after
    assert len(after) <= len(before) + 200
    assert case['objective'] not in after


def test_narrative_scope_check_accepts_chinese_synonyms(tmp_path, monkeypatch):
    from weyra import evaluation
    original = evaluation._offline_runtime

    def alternate_wording(case, group, variant):
        runtime = original(case, group, variant)
        response = runtime.registry.get('evolution').responses[0]
        object.__setattr__(response, 'content', '正式报告先给结论。小说和其他叙事类作品按叙事顺序展开，不必先给结论。')
        return runtime

    monkeypatch.setattr(evaluation, '_offline_runtime', alternate_wording)
    case = next(c for c in evaluation.load_cases() if c['id'] == 'W03')
    result = asyncio.run(evaluation._run_case(case, group='evolution', variant='full_merge',
                                            root=tmp_path, runtime=None, budget=Budget(), offline=True))
    assert result['checks']['narrative_exception']

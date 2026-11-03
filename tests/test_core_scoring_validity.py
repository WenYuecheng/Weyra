import pytest

from benchmarks.core_scoring import grade_task, build_scoring_validity_audit


def fixture():
    return {
        "id": "synthetic-contract",
        "input": {
            "output_contract": {
                "diagnosis": {"type": "str"},
                "source_id": {"type": "str"},
                "state": {"type": "str", "allowed_values": ["open", "closed"]},
                "count": {"type": "int"},
            }
        },
        "grading": {
            "expected": {
                "diagnosis": "temporary upstream outage",
                "source_id": "receipt-12",
                "state": "open",
                "count": 3,
            }
        },
    }


def test_unbounded_wording_difference_is_pending_never_pass():
    c = fixture()
    a = dict(c["grading"]["expected"], diagnosis="transient service unavailability")
    g = grade_task(c, a)
    assert not g["success"] and g["pending_review"] and g["hard_failures"] == 0


def test_numeric_identifier_enum_structure_conflicts_remain_hard():
    c = fixture()
    for change in [
        {"count": 4},
        {"source_id": "receipt-13"},
        {"state": "closed"},
        {"extra": "field"},
        {"diagnosis": ""},
    ]:
        a = (
            dict(c["grading"]["expected"], diagnosis="different wording", **change)
            if "diagnosis" not in change
            else dict(c["grading"]["expected"], **change)
        )
        g = grade_task(c, a)
        assert not g["success"] and not g["pending_review"] and g["hard_failures"] > 0


def test_posthoc_only_demotes_disputed_failures_and_keeps_original():
    c = fixture()
    original = {
        "success": False,
        "hard_failures": 1,
        "checks": {"field:diagnosis": False},
        "pending_review": False,
    }
    records = [
        {
            "case_id": c["id"],
            "answer": dict(c["grading"]["expected"], diagnosis="service temporarily unavailable"),
            "original_grade": original,
        }
    ]
    audit = build_scoring_validity_audit([c], records)
    assert audit["records"][0]["original_grade"] == original
    assert audit["records"][0]["review_status"] == "pending_review"
    assert audit["records"][0]["audit_grade"]["success"] is False
    assert audit["summary"]["promoted_to_success"] == 0


def test_posthoc_never_promotes_frozen_failure_even_if_rescore_matches():
    c = fixture()
    original = {"success": False, "hard_failures": 2, "checks": {}, "pending_review": False}
    audit = build_scoring_validity_audit(
        [c], [{"case_id": c["id"], "answer": c["grading"]["expected"], "original_grade": original}]
    )
    assert audit["records"][0]["audit_grade"] == original
    assert audit["summary"]["promoted_to_success"] == 0


def test_audit_writer_refuses_existing_result(tmp_path):
    import pytest
    from benchmarks.core_scoring import write_scoring_validity_audit

    path = tmp_path / "audit.json"
    write_scoring_validity_audit(path, [], [])
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_scoring_validity_audit(path, [], [])
    assert path.read_bytes() == original


def test_progressive_propagates_pending_status(tmp_path):
    import asyncio
    from benchmarks.core_evaluation import progressive_infer
    from weyra.contracts import ModelInfo, ModelResponse
    from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
    from weyra.runtime_state import Budget

    c = {
        "id": "wording",
        "input": {
            "objective": "Explain the diagnosis",
            "output_contract": {"diagnosis": {"type": "str"}},
        },
        "grading": {"expected": {"diagnosis": "temporary outage"}},
    }
    registry = ProviderRegistry()
    registry.register(
        "test", ScriptedProvider([ModelResponse('{"diagnosis":"transient unavailability"}')])
    )
    runtime = ModelRuntime(registry, {"main": ModelInfo("test", "m", 20000, 1000)})
    result = asyncio.run(
        progressive_infer(c, "Explain diagnosis", "test-skill", tmp_path, runtime, Budget())
    )
    assert result["status"] == "pending_review"


def test_compression_propagates_pending_status(tmp_path, monkeypatch):
    import asyncio
    from weyra import core_context, core_evaluation

    async def fake(*args, **kwargs):
        return {"status": "completed", "answer": '{"diagnosis":"transient unavailability"}'}

    monkeypatch.setattr(core_context, "run_context_case", fake)
    runner = object.__new__(core_evaluation.CoreRunner)
    runner.stopped = False
    runner.root = tmp_path
    runner.repeats = 1
    runner.runtime = None
    runner.budget = None
    runner.manifest = {
        "long_history_cases": [
            {
                "id": "wording-1",
                "input": {"output_contract": {"diagnosis": {"type": "str"}}},
                "grading": {"expected": {"diagnosis": "temporary outage"}},
            }
        ]
    }
    results = []

    async def unit(key, info, operation):
        results.append(await operation())

    runner.unit = unit
    asyncio.run(runner.compression())
    assert len(results) == 5 and all(r["status"] == "pending_review" for r in results)


def test_audit_accepts_result_and_case_mappings():
    c = fixture()
    record = {
        "id": c["id"],
        "answer": dict(c["grading"]["expected"], diagnosis="different wording"),
        "grade": {"success": False, "hard_failures": 1},
    }
    audit = build_scoring_validity_audit({c["id"]: c}, {"saved/run/key": record})
    assert audit["records"][0]["record_id"] == "saved/run/key"
    assert audit["records"][0]["review_status"] == "pending_review"


@pytest.mark.parametrize(
    "actual",
    [
        {"patches": [True, 2]},
        {"patches": [1.0, 2]},
        {"patches": [1, {"count": True}]},
        {"patches": [1, {"count": 2.0}]},
        {"patches": [1, {"count": 2, "extra": 3}]},
    ],
)
def test_nested_values_require_strict_types_and_keys(actual):
    expected = (
        {"patches": [1, {"count": 2}]}
        if isinstance(actual["patches"][1], dict)
        else {"patches": [1, 2]}
    )
    case = {"grading": {"expected": expected}, "input": {}}
    grade = grade_task(case, actual)
    assert not grade["success"]
    assert grade["hard_failures"] > 0
    assert not grade["pending_review"]


def test_nested_strict_values_accept_exact_match():
    value = {"patches": [1, {"count": 2, "items": [False, 2.0, None]}]}
    assert grade_task({"grading": {"expected": value}}, value)["success"]


def test_nested_dict_boolean_cannot_match_integer_one():
    case = {"grading": {"expected": {"record": {"count": 1}}}}
    grade = grade_task(case, {"record": {"count": True}})
    assert not grade["success"] and not grade["pending_review"]


def test_nested_dictionary_key_types_are_strict():
    case = {"grading": {"expected": {"record": {1: "value"}}}}
    assert not grade_task(case, {"record": {True: "value"}})["success"]

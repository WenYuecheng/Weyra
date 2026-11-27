"""Zero-API pytest integration test running the 3-suite reliability & 30-round evolution benchmark."""

from __future__ import annotations

import asyncio

from benchmarks.reliability.run_suite import run_all


def test_zero_api_reliability_and_30_round_suite_passes():
    report = asyncio.run(run_all(output_path=None))
    assert report["paid_api_calls"] == 0
    assert report["all_passed"] is True

    suite_a = report["suite_a_fault_injection"]
    assert suite_a["passed"] == 6
    assert suite_a["total"] == 6

    suite_b = report["suite_b_continual_30_round"]
    assert suite_b["baseline"]["focus_retention"] < 1.0
    assert suite_b["weyra_guarded"]["focus_retention"] == 1.0
    assert suite_b["weyra_guarded"]["judge_llm_calls"] == 0
    assert suite_b["weyra_guarded"]["validation_cache_hits"] > 0

    suite_c = report["suite_c_trace_audit"]
    assert suite_c["clean"]["process"]["verify_after_edit"] is True
    assert suite_c["clean"]["process"]["error_loops"] == 0
    assert list(suite_c["clean"]["skill_funnel"]["activated"]) == ["code-hygiene"]
    assert suite_c["noisy"]["process"]["error_loops"] >= 1
    assert suite_c["noisy"]["process"]["unverified_claim"] is True

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
    assert suite_b["baseline"]["anchor_retention"] == 0.0
    assert suite_b["baseline"]["boundary_knowledge_retention"] == 0.0
    assert suite_b["weyra_guarded"]["anchor_retention"] == 1.0
    assert suite_b["weyra_guarded"]["boundary_knowledge_retention"] == 1.0
    assert suite_b["weyra_guarded"]["judge_llm_calls"] == 0
    assert suite_b["weyra_guarded"]["replay_cache_hits"] > 0

    suite_c = report["suite_c_trace_audit"]
    assert suite_c["ideal"]["process"]["tier"] == "ideal"
    assert suite_c["ideal"]["skill_funnel"]["activated"] == ["code-hygiene"] or suite_c["ideal"]["skill_funnel"]["activated"] == ("code-hygiene",)
    assert suite_c["lucky_pass"]["process"]["tier"] == "lucky_pass"

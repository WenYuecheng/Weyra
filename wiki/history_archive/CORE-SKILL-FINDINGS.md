# Core skill findings — interim live v3

Read-only observation at 2026-10-18T05:59:36.427481+00:00. The paid run is ongoing. Counts below are a moving snapshot, not the final experiment matrix: 384 result rows; group counts {'localization': 120, 'compression': 29, 'public': 72, 'maintenance': 34, 'skill_test': 129}; maintenance statuses {'pass': 28, 'interrupted': 6}. Captured results.json SHA-256: `189b50c90789eb73fa0e7a1bf43ecafb91a17ea0a71d1a6738ab14858ed4148d`.

These examples describe completed repeat-0 family/arm units. Maintenance `pass` means the maintenance procedure completed; it does not mean a candidate was accepted or heldout tasks passed. Neither partial counts nor these selected examples establish causal benefit from experience memory or bounded editing.

## Three completed examples

| Family / arm / repeat | Candidate decisions | Heldout new requirement / old ability / scope exception | Effective version and body hash |
|---|---|---|---|
| narrative / experience_bounded / 0 | Round 0: accepted, validation successes 0→1/2 and hard failures 3→1. Round 1: discarded as already covered. | pass / pass / pass | 1.0.1; `7ffeb84e52c9a5dc1b30dd409be74b5612598d5d5493d0b9440408b393d4e1f4` |
| report / no_experience_bounded / 0 | Round 0: rejected, successes 2→1/2 and hard failures 0→1. Round 1: rejected 2/2 tie, hard failures 0→0. | pass / fail / pass | 1.0.0; `a2d18f8b48bbc5d3d831389213cd6c9f76eac9e84a846f6fef9ca813c8b54c09` |
| technical / experience_bounded / 0 | Round 0: discarded as task-level evidence. Round 1: rejected before gate: Invalid knowledge kind. | pass / fail / pass | 1.0.0; `8dc337cd7c2d96544423aa5db0da35ca2ffc76606e380b55d133f4aa59369ace` |

The narrative candidate adds an explicit boundary: leave an unestablished cause null rather than inferring it from temporal order. Its old-ability task preserves chronology and named actors; its scope exception correctly permits the fictional scenario label `dragon`. All three heldout checks pass. The frozen-old repeat-0 arm also passes all three, so this accepted candidate is evidence of a working gate/publication path, not evidence of an observed heldout gain over frozen old.

The report candidate never publishes; the effective body remains version 1.0.0. Its old-ability answer wraps the required fields in a `data` object, failing the exact output schema (five failed checks). The frozen-old arm also fails that task; the no-skill arm passes it. This is an observed output-contract failure with unchanged skill bytes, not a regression caused by a published edit. The scope exception correctly copies total 23 without imposing a rate.

The technical arm likewise keeps version 1.0.0 after a model proposal uses an unsupported knowledge-note kind. The old-ability task has `records[-11]`, length 10: it returns test indices `[0,-11]`, failing the expected nearest valid negative boundary `[-10,-11]`. Frozen old passes this task; the no-skill arm also fails. Since the compared arm retained the original skill, this difference cannot be attributed to candidate publication. The scope exception still follows HTTP 503 retry guidance.

## Ceiling and interpretation limits

Initial skills already give broad correct rules, and task inputs supply detailed field types, allowed values, output contracts, and scope guidance. Thus the experiment measures incremental maintenance on top of substantial task guidance. For these three repeat-0 families, frozen-old heldout results are narrative 3/3, report 2/3, and technical 3/3; no-skill results are 2/3, 3/3, and 2/3 respectively. These examples show both a ceiling and nontrivial remaining schema/boundary failures; they do not support a general ranking from partial data.

Each candidate gate has only two validation tasks and requires strictly more successful tasks with no increase in hard-failure checks. If old succeeds on 2/2, acceptance is mathematically impossible even if the candidate also gets 2/2. Validation ceiling does not guarantee heldout success, as the report schema failure illustrates. Candidate and old are sampled separately, and stochastic outcomes can change across arms/rounds; repeated identical skill bodies do not imply identical model answers.

Gated creation intentionally rejects a new skill because no old-skill comparison baseline is implemented. The evaluated maintenance families start with existing skills; this run cannot demonstrate validated creation. Interrupted/error maintenance must retain its explicit status and actual effective artifact identity. Pending units and incomplete repeat/arm coverage remain outside any final causal conclusion.

## Inspectable evidence

[Live result rows](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/results.json) contain the exact maintenance keys `maintenance/{family}/{arm}/0`, candidate gate reports, heldout answers/checks, and effective body hashes. These hashes identify the canonical body digest recorded by the runner, not the complete SKILL.md file hash.

- [narrative: active effective skill](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/units/narrative-experience_bounded-0/.weyra/skills/narrative/SKILL.md); [proposal and provenance directory](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/units/narrative-experience_bounded-0/.weyra/skill-evolution).
- [report: active effective skill](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/units/report-no_experience_bounded-0/.weyra/skills/report/SKILL.md); [proposal and provenance directory](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/units/report-no_experience_bounded-0/.weyra/skill-evolution).
- [technical: active effective skill](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/units/technical-experience_bounded-0/.weyra/skills/technical/SKILL.md); [proposal and provenance directory](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/units/technical-experience_bounded-0/.weyra/skill-evolution).

- [Frozen maintenance/gate runner](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/frozen-source/agents/core_evaluation.py); [frozen gate publication implementation](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/frozen-source/agents/skill_evolution.py).
- [Frozen manifest with train/validation/test roles](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/frozen-source/data/core-eval-v1.json); [frozen deterministic grader](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/frozen-source/agents/core_scoring.py).

This review did not invoke models, modify the live/frozen code, change tasks or grading, or alter result artifacts.

## 最终结果入口

本文件保留原样本观察时间与案例，不追改中期样本。批次现已停止；最终覆盖、评分争议与预算边界以 [最终主报告](/Users/wenyuecheng/code/python/Weyra/wiki/CORE-EVALUATION.md) 和 [最终验收表](/Users/wenyuecheng/code/python/Weyra/wiki/CORE-ACCEPTANCE.md) 为准。

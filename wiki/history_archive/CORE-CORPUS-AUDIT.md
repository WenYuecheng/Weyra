# Core corpus audit

This is a read-only audit of the final `data/core-eval-v1.json` corpus and its deterministic scorer. It does not change cases, gold labels, grading, or completed results. No model calls were used. Counts below describe fixture coverage rather than statistical independence or demonstrated model improvement.

## Localization: 60 rows, 34 distinct probes

Canonical JSON serialization of the complete `input` yields **34 unique inputs out of 60 rows**. The old variant consumes the query alone; it also has **34 unique queries**. The new variant consumes `(query, explicit_id, used_ids)`; that tuple has **34 unique values**. Thus each variant has 34 distinct probes, and paired comparison has 34 distinct input pairs, not 60 independent observations per arm or 120 independent observations overall.

| Category | Rows | Unique full inputs | Unique old queries | Unique new resolver tuples |
|---|---:|---:|---:|---:|
| explicit | 10 | 6 | 6 | 6 |
| recent | 10 | 6 | 6 | 6 |
| cross_language | 10 | 6 | 6 | 6 |
| synonyms | 10 | 6 | 6 | 6 |
| ambiguous | 10 | 5 | 5 | 5 |
| absent | 10 | 5 | 5 | 5 |
| Total | 60 | 34 | 34 | 34 |

Within each of the first four categories, four probes occur twice and two occur once. Within ambiguous and absent, all five probes occur twice. There are 26 duplicate rows beyond first occurrences. Repeated rows measure deterministic consistency and weight some probes twice; they do not increase independent coverage. Uniform row weighting and uniform unique-probe weighting therefore answer different questions. Report both if needed, with deduplicated pairs as the inferential unit; do not bootstrap repeated rows as independent samples.

The new variant is intentionally given explicit IDs or successful-use IDs for those respective categories, while old search only sees the query. This tests available resolver behavior under richer operational metadata, not equal-information natural-language understanding. Cross-language and synonym fixtures include their target phrases as skill metadata aliases. Success here demonstrates alias matching within this library; it does not establish open-ended multilingual semantic retrieval. Ambiguity is author-labelled, and some underspecified prompts could reasonably support clarification or a different interpretation. The eight-skill library is small, with six primary skills and two distractors; production-scale catalog behavior is unmeasured.

## Evolution: six related families, 18 held-out tasks

Each family has four train, two validation, and three test tasks. The actual test-role field is `test_role`, with exactly one `new_requirement`, one `old_ability`, and one `scope_exception` per family. All 54 task IDs and group IDs are distinct, but distinct IDs do not guarantee semantic independence.

Train and validation tasks within a family mostly share one template and differ in counts, names, or identifiers. Most `new_requirement` test tasks are the same operation with new values and a shorter objective. The `old_ability` evidence, technical, report, and structured tasks introduce agreement, negative indexing, zero completion, and nonblank notes respectively; narrative and file-workflow old-ability tasks remain close numeric/name substitutions. This is deliberate rule-transfer coverage but weak protection against template memorization. It does not establish broad task-distribution generalization. Family-level clustering is more defensible than treating all task rows or repeated runs as independent; six families remain a small sample.

Scope-exception tasks are actual scored cases: source-label indexing, copying a prose total, fictional causal labeling, retrying a transient 503, inventory without conversion, and preserving raw display strings. They test selected boundaries, not every possible conflict between a learned rule and a current request. `cross_boundary_examples` is additional family metadata, not extra scored tasks.

The family feedback overlaps the original skill body, and many train/validation objectives explicitly restate the desired rule. Consequently some initial skills can already solve these tasks, creating ceiling effects and reducing the ability to attribute test success to maintenance. Shorter test objectives remove some repetition, but typed field descriptions and enums still provide substantial guidance. Maintenance improvements should therefore be interpreted alongside initial-arm performance and old-ability/scope-exception results, rather than as proof of new capability acquisition.

The scorer parses JSON and checks exact key sets, exact Python value types, and exact task-specific values. It rejects a copied grader object, wrong factual values, extra fields, and malformed JSON. Categorical contracts expose allowed vocabularies, not the selected gold choice. Some contract descriptions also describe the intended decision rule, notably evidence resolution and boundary indices. These are deterministic contract checks, not a blinded assessment of prose quality. Nested list/object equality uses Python equality rather than a recursive type schema. Numerically equivalent integer/float output can fail at the top-level field because the grader requires exact types. All arrays have significant order. A plausible alternative diagnosis, manifest, or representation can fail even when useful.

File-workflow tasks grade a JSON plan; they do not execute a conversion or inspect a generated artifact. The scorer accepts a workspace argument but does not inspect it. Technical tasks grade the chosen repair and test indices; they do not execute repair code. Report and structured tasks verify small specified values, not robustness over realistic datasets. These limitations must accompany any broad quality or reliability claim.

## Long histories: 12 synthetic source histories

There are six types, each repeated twice, with six fixed and six pressure triggers and six fit and six overflow labels. Histories contain substantive batch/shipment decisions, state changes, task statuses, and source observations. Six contain complete native tool-call/result pairs with results longer than 8,192 characters. Multi-compression cases request two fold points; final fixed fold points include the end of source history.

The two cases per type share most of their template and expected facts. Their statistical information is closer to six source-story families than twelve independent real user histories. Larger histories include many distinct records generated by the same pattern; meaningful values avoid empty padding but do not reproduce the diversity of real conversation. Simple fact extraction is narrower than long-horizon execution quality. Labels alone do not prove fit, pressure, overflow, pruning, or multiple successful folds: the runtime's observed token counts, actual context capacity, pruning counts, and completed fold counts are the evidence for each branch. Repeating a history or changing its trigger does not create a new independent story.

## Public questions: capability baseline only

The public subset contains **12 HLE questions and zero GAIA questions**. All source candidates from both datasets have audit entries with source IDs, canonical record SHA-256 hashes, inclusion decisions, and reasons. Selection is a fixed supported text subset, not random sampling. The selected questions were read in full: none requires an attachment or an omitted image. In particular, the contour-integral question supplies its defining curve and integrand as text even if upstream metadata classifies the original record as multimodal. Mathematical background knowledge is still required; self-contained here means no retrieval or external file is necessary to read the problem.

The public questions have only their original query in model-visible input. They carry no artificial histories or forced compaction. Plain versus DSH results on these 12 are **capability and compatibility baselines, not compression evidence**. Predominantly mathematical HLE coverage cannot represent GAIA tool use, browsing, multimodal reasoning, or general public benchmark performance. Prior exposure or benchmark contamination is not measured.

Public scoring normalizes case and whitespace only. Exact matches count as success; nonempty mismatches are unsuccessful with `pending_review=True`, while empty answers fail without equivalence review. Equivalent mathematical notation, explanatory text surrounding the correct answer, and alternative valid representations may require blinded domain review. Until that review occurs, distinguish exact-match success, pending review, and confirmed incorrectness; do not call every nonmatch wrong or count pending answers as correct. The audit provides provenance, not a guarantee that every upstream reference answer is unambiguous or correct.

## Reporting boundary

Keep model/run failures, context coverage failures, deterministic score failures, and pending semantic review separate. Use paired case comparisons, deduplicate identical localization probes, and cluster evolution and history claims by family. Repeats can measure run variation but do not expand corpus diversity. This corpus supports a small controlled engineering evaluation; it does not support precise population-level improvement estimates or broad causal claims about compaction or learned skill quality on its own.

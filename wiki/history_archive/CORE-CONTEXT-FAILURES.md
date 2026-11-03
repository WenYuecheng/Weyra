# Interim context failure analysis — v3

Read-only investigation of completed units in `.weyra/core-eval-20261018-v3`, observed on 2026-10-18. The paid run was ongoing during this investigation. These are sampled diagnoses, not final aggregate counts. No model requests were made, and no frozen code, inputs, graders, or run artifacts were changed.

Evidence was read from each unit's SQLite database with `mode=ro`: `model_request`, referenced prompt blobs, `evaluation_response`, `model_usage`, and `compaction` events. The results snapshot was read from `.weyra/core-eval-20261018-v3/results.json`; expected values were read from the host manifest. Only short response excerpts are reproduced below.

## 1. JSON merge truncation prevented a fold despite a correct final answer

Unit: `compression/history-early_constraint-1/single_json/0`.

Source: `.weyra/core-eval-20261018-v3/units/history-early_constraint-1-single_json-0/.weyra/state.db`.

**Definite:** Four compression responses were recorded. The first three were valid JSON containing `episode_memory`, `working_memory`, and `tool_memory`. The fourth, the merge after three `summary_segment` events, ended with `stop_reason=length`, output usage 4,096 tokens, and invalid JSON. Its final excerpt was `..."prior_estimate": 19, "reviewer": "R4", "zone": 3}, {"accepted`.

The request actually recorded `max_output_tokens: 4096`; this diagnosis uses that durable request rather than an earlier intended limit. No structured compaction was published. The completed result was `error` with `error_kind=insufficient_folds`, not a failed answer grade. Its main response was `{"budget": 70, "preserve_originals": true}`, matching the requested facts.

The memory prompt began: “Return only JSON with nonempty episode_memory and working_memory objects, and a tool_memory object.” The failed merge expanded into detailed batch records. **Inference:** excessive detail contributed to truncation. The trace proves truncation and fold rejection; it does not prove that the model forgot the target fact.

## 2. Pressure retry amplification and component-format mismatch

Unit: `compression/history-early_constraint-2/three_parallel/0`.

Source: `.weyra/core-eval-20261018-v3/units/history-early_constraint-2-three_parallel-0/.weyra/state.db`.

**Definite:** The unit timed out at the 900-second limit. Its database contained 129 physical model requests and 115 recorded responses, with one committed pressure fold. Among those responses, 102 were valid JSON, 13 invalid JSON, and two had `stop_reason=length`. Sixty-nine responses contained all three outer memory keys.

Each component prompt first requested a JSON object containing all three memory keys, then appended a conflicting specialization, for example: “Produce only the episode_memory object without its outer key.” A sampled response instead began `{"episode_memory":{"project":"Vega"...` and included the other two memory keys. Another sampled response was malformed JSON. The component parser accepts any parsed JSON value, while the combined three-key validator checks that each assembled component is a dictionary; an incorrectly wrapped component can therefore survive that shallow shape check and create nested memory objects.

**Inference, supported by the helper control flow:** after pressure is reached, the helper attempts compaction again after every subsequent completed appended interaction. A rejected/noncommitted summary does not establish a bounded failure budget or a cooldown. Hierarchical segmentation and three parallel components multiply physical requests, explaining the observed request amplification and timeout. The trace does not explicitly log every swallowed compaction failure reason; the remaining failures cannot all be assigned definitively to malformed JSON, nonshrinking output, or incorrect wrapping. A timeout is not evidence that every target fact was lost.

For a future version, bound total compaction attempts and physical summary calls per unit, stop or record a distinct compression failure after repeated unsuccessful attempts, and record rejection reasons. Resolve the full-object versus component-only prompt conflict and validate component shapes explicitly. These suggestions were not implemented or tuned during this run.

## 3. Strict symbolic-value grading rejected descriptive answers in both arms

Unit: `compression/history-failure_path-1/dsh_checkpoint/0`; baseline: `compression/history-failure_path-1/none/0`.

Sources:

- `.weyra/core-eval-20261018-v3/units/history-failure_path-1-dsh_checkpoint-0/.weyra/state.db`
- `.weyra/core-eval-20261018-v3/units/history-failure_path-1-none-0/.weyra/state.db`
- `.weyra/core-eval-20261018-v3/results.json`
- `data/core-eval-v1.json`, case `history-failure_path-1`

**Definite:** DSH published one structured fold, reducing its recorded projection estimate from 22,240 to 3,203 tokens, following one tool-result reduction. Its final response used descriptive strings: `failed_action` was “Retrying the corrupt archive upload unchanged”; `failure` described checksum mismatch and a failed unchanged retry; `successful_repair` described successful archive rebuilding. The uncompressed baseline likewise answered “Upload the archive”, “Checksum mismatch (retrying unchanged failed again)”, and “Rebuilt the archive”.

The host grader required exact values `upload`, `checksum_mismatch`, and `rebuild_archive`. The query requested JSON with the three field names but did not specify that canonical value vocabulary. Both outputs had the correct JSON keys and types, but all three exact-value checks failed.

**Inference:** these particular failures primarily expose output-contract/grader strictness rather than demonstrated semantic retention loss. The DSH `failed_action` wording may also shift from the original upload to its failed retry, so this sample is not proof of perfect retention. It is, however, insufficient evidence to conclude that compression caused the scored failure, because the baseline also received the same three value failures while describing the source events.

## Interpretation limits

Preserve separate categories for answer-grade failures, summary-format failures, insufficient committed folds, provider failures, and timeouts. The uncompressed overflow-labelled baseline can succeed if the provider's actual tokenization fits its physical window; the conservative local pressure estimate is not a provider rejection measurement. Final comparisons should retain all planned units in coverage reporting and present trigger/regime strata. Recompute aggregate counts only after the controller finishes; this document records the three terminal samples above and does not estimate the full run's final success rate.


Final execution report: [CORE-EVALUATION.md](CORE-EVALUATION.md). The samples and interim counts above are preserved as observed; use the final report for execution-wide coverage and totals.

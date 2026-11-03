# Posthoc scoring validity policy

This correction applies to the current production scorer, not the frozen evaluation snapshot or its stored scores. It was designed from contracts and synthetic counterexamples before inspecting completed answers. The original corpus and reference answers are unchanged.

Exact structured success is unchanged. A differing nonempty string whose vocabulary is not explicitly constrained is now a disputed check. It can produce `pending_review=True` only when no other hard failure exists. This is uncertainty about scoring validity, not a finding of semantic equivalence or correctness. Explicit enums (`allowed_values`, `enum`, `const`), identifier/source/path/owner/actor fields, structural differences, type differences, blank strings, and numeric value contradictions retain their hard constraints. Arrays remain exact. Existing public-question nonexact matching continues to require review.

`grade_task(case, answer, workspace=None)` retains its interface and adds `disputed_checks`. `success` remains false for disputed answers. `hard_failures` excludes eligible disputed wording checks but includes all other failed checks. `checks` still preserves each exact-match boolean for traceability.

For reporting without rewriting any original scores:

```python
from agents.core_scoring import write_scoring_validity_audit

artifact = write_scoring_validity_audit(
    new_audit_path,
    cases,
    [{"record_id": run_key, "case_id": case_id,
      "answer": saved_answer, "original_grade": saved_grade}],
)
```

`build_scoring_validity_audit(cases, records)` returns the same artifact in memory. Its schema is `version`, `policy`, `summary`, and `records`. Each record contains the original grade, a separate audit grade, review status, disputed check names, and a reason. Original successes and undisputed results are preserved. An original failure may only become pending review; it can never become a success, even if rescoring happens to match. Unknown case IDs preserve the original result. The writer creates a new file exclusively and refuses to overwrite an existing audit or main result. Raw answers are not copied into the audit artifact.

A report should show the frozen score as primary and the number of potentially over-strict failures as a separate pending-review count. It must not add these to successes, recompute an improved primary score, or claim semantic equivalence without blinded review. This policy is intentionally broader than a semantic classifier: unconstrained wording differences remain unknown rather than automatically correct. The field-name reference guard is conservative and can leave some wording differences hard-failed; this avoids silently relaxing identifiers and evidence attribution.

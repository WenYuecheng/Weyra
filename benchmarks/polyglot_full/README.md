# Weyra on Aider Polyglot

This experiment compares a minimal tool loop with Weyra on all 225 exercises
at Aider Polyglot revision `7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f`.
Both arms start from fresh copies and use the same GLM-5.3-Flash API, sandbox,
tools, test feedback, model parameters, and limits. Weyra adds a first-round
review instruction and, after a failed submission, rebuilds the active context
from the task, saved implementation, and bounded verifier diagnostic.

| Setting | Value |
| --- | --- |
| Reasoning effort | max |
| Temperature / top-p | 1.0 / 0.95 |
| Maximum output per model request | 32,768 tokens |
| Maximum ledger volume per task and arm | 196,608 tokens |
| Maximum physical model requests per task and arm | 12 |
| Submission limit | 2 |

## Run the comparison

Run from the repository root with Python 3.11+, Git, and Docker. Download the
dataset independently and use the experiment's language images.

```sh
python -m pip install -e '.[dev]'
git clone https://github.com/Aider-AI/polyglot-benchmark.git .benchmarks/polyglot-upstream
git -C .benchmarks/polyglot-upstream checkout 7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f

docker build -t weyra-polyglot:20261023 benchmarks/polyglot_full
for lang in go rust cpp java; do
  docker build -f benchmarks/polyglot_full/Dockerfile.glm --target "$lang" \
    -t "weyra-polyglot-glm:$lang-v1" benchmarks/polyglot_full
done

python -m benchmarks.polyglot_full.glm_full inventory --upstream .benchmarks/polyglot-upstream
python -m benchmarks.polyglot_full.glm_full prepare --upstream .benchmarks/polyglot-upstream \
  --output .benchmarks/results/full-glm
python -m pytest -q tests/test_polyglot_full.py tests/test_polyglot_fresh_repair.py
```

Set `ZAI_API_KEY` in the host process before running the paid comparison.
Set `POLYGLOT_TOKEN_CAP` to the desired campaign ceiling in accounted tokens.
With the per-task limit above, the worst-case ceiling for all 450 task-arm
runs is 88,473,600 accounted tokens. The token cap is a local accounting limit.
Re-running with the same protocol resumes recorded work.

```sh
python -m benchmarks.polyglot_full.glm_full run --upstream .benchmarks/polyglot-upstream \
  --output .benchmarks/results/full-glm --token-cap "$POLYGLOT_TOKEN_CAP"
python -m benchmarks.polyglot_full.glm_full report --upstream .benchmarks/polyglot-upstream \
  --output .benchmarks/results/full-glm
```

The grading preflight confirms the reference and records the starter outcome.
Passing starters stay in the denominator. For `go/counter`, the submission is
a test suite that must accept the supplied correct implementation and reject
the three defective implementations. For `rust/robot-name`, the reference
is adapted to the image's rand 0.8 API. The cases and image IDs are frozen
before the first paid request. If a grader fails preflight, the command reports
the affected task IDs in `full-preflight-blocked.json`. After resolving the
error, rerun `prepare` with the same source and images to retry those tasks.

## Read the report

The full denominator is 225, including missing pairs. Both arms receive a
binary pass outcome for each valid task, so a task already passed by the
baseline can only be matched. The report records per-task wins, ties and
regressions, total and per-language pass counts, and first-submission results.
The target requires complete coverage, a strict gain in every language, zero
task regressions, and at least 198 total Weyra passes. The 198 target is the
published five-run mean for a quantized GLM-5.3-Flash checkpoint with Aider.
That run used different decoding and orchestration and supports a numerical
reference. This repository's report documents the Weyra protocol.

The source release includes offline test coverage and can inventory all 225
tasks. A live campaign with this protocol is still pending.

## Bounded whole-file profile

For a smaller campaign budget, `glm_whole` uses the same 225 tasks and graders,
with one file-writing response per submission and at most two submissions.
Both arms receive only `write_file`, use `high` reasoning, an 8,192-token output
limit, at most two physical requests and 65,536 accounted tokens per task-arm.
All editable file contents are included in the prompt. Weyra retains the review
instruction and repair-context reset. The single-step Weyra loop reaches its
step limit after saving; the independent verifier grades the saved artifact.

Use a separate output directory so outcomes from different profiles stay separate:

```sh
python -m benchmarks.polyglot_full.glm_whole prepare --upstream .benchmarks/polyglot-upstream \
  --output .benchmarks/results/whole-glm
python -m benchmarks.polyglot_full.glm_whole run --upstream .benchmarks/polyglot-upstream \
  --output .benchmarks/results/whole-glm --token-cap "$POLYGLOT_TOKEN_CAP"
python -m benchmarks.polyglot_full.glm_whole report --upstream .benchmarks/polyglot-upstream \
  --output .benchmarks/results/whole-glm
```

The whole-file and multi-tool profiles must be reported independently. Neither
is the external Aider protocol, which uses different editing and feedback.

- [Aider Polyglot dataset](https://github.com/Aider-AI/polyglot-benchmark/tree/7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f)
- [GLM-5.3-Flash configuration](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)
- [Published GLM-5.3-Flash reference results](https://github.com/local-inference-lab/rtx6kpro/blob/master/models/glm-5.3-flash/aider-polyglot-release-10152026.md)

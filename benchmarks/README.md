# Historical benchmarks (source distribution only)

Daily installed-package validation remains `weyra eval --offline` with twenty scenarios.
Historical implementations and datasets are maintained here, outside the runtime wheel.

From a checkout or extracted source archive:

```sh
python -m pip install -e '.[dev]'
python -m benchmarks.core_evaluation --help
python -m benchmarks.final_delivery_eval --help
python -m scripts.reports.finish_report --help
```

The corresponding `python -m weyra.core_evaluation` and other historical module paths
remain source-checkout compatibility entries. Ordinary wheel installations report
that the source repository/archive is required. Install commands do not run experiments.

Use `.benchmarks/results/<run-name>` for new output directories. Paid experiment
runners still require explicit output/ledger arguments, retain their budgets, and
refuse conflicting protocols. Existing `.weyra` evidence paths and frozen sources
remain valid. Do not overwrite prior results or regenerate a historical freeze.

Source fingerprints include nested runtime Python, prompts, TUI resources, benchmark
implementations and fixture data. New protocol versions distinguish this fingerprint
layout from archived protocols. Older source files and hashes are preserved.

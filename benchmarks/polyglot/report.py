"""Recompute the November 24 Polyglot results from the published task verdicts."""

import argparse
from collections import Counter
import csv
import json
from math import comb
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
ARMS = ("baseline", "weyra")
STAGES = ("first", "second", "final")
COUNTS = {"python": 34, "go": 39, "rust": 30, "cpp": 26, "java": 47}
VERDICTS = {"pass", "fail"}
SCORED = VERDICTS | {"resource_exhausted"}


def load_rows(path):
    """Read the fixed cohort and reject duplicate tasks or invalid verdict sequences."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = ["task", *(f"{arm}_{stage}" for arm in ARMS for stage in STAGES)]
        if reader.fieldnames != columns:
            raise ValueError("Unexpected result columns")
        rows = {}
        for row in reader:
            task = row["task"]
            if not task or task in rows or not re.fullmatch(r"[a-z]+/[a-z0-9-]+", task):
                raise ValueError(f"Invalid or duplicate task: {task!r}")
            if set(row) != set(columns):
                raise ValueError(f"Unexpected result fields: {task}")
            for arm in ARMS:
                first, second, final = (row[f"{arm}_{stage}"] for stage in STAGES)
                if first not in VERDICTS | {""} or second not in VERDICTS | {""}:
                    raise ValueError(f"Invalid submission verdict: {task}/{arm}")
                if final not in SCORED | {"infrastructure_error"}:
                    raise ValueError(f"Invalid final verdict: {task}/{arm}")
                if second and first != "fail":
                    raise ValueError(f"Second submission requires a first failure: {task}/{arm}")
                if final in VERDICTS and (second or first) != final:
                    raise ValueError(f"Final verdict and submissions disagree: {task}/{arm}")
                if final == "resource_exhausted" and (first, second) != ("fail", ""):
                    raise ValueError(f"Invalid resource exhaustion sequence: {task}/{arm}")
            rows[task] = row
    if Counter(task.split("/")[0] for task in rows) != COUNTS:
        raise ValueError("Unexpected language counts for the frozen five-language subset")
    return list(rows.values())


def paired(rows, stage):
    """Score only tasks with a verdict from both arms at the given stage."""
    valid = SCORED if stage == "final" else VERDICTS
    pairs = []
    for row in rows:
        baseline, weyra = (row[f"{arm}_{stage}"] for arm in ARMS)
        if baseline in valid and weyra in valid:
            pairs.append((baseline == "pass", weyra == "pass"))
    wins = pairs.count((False, True))
    losses = pairs.count((True, False))
    discordant = wins + losses
    p = 2 * sum(comb(discordant, k) for k in range(min(wins, losses) + 1)) / 2**discordant
    return {
        "pairs": len(pairs),
        "weyra_pass": sum(weyra for _, weyra in pairs),
        "baseline_pass": sum(baseline for baseline, _ in pairs),
        "weyra_wins": wins,
        "baseline_wins": losses,
        "exact_two_sided_mcnemar_p": min(1.0, p),
    }


def summarize(rows):
    repairs = {}
    for arm in ARMS:
        failures = [row for row in rows if row[f"{arm}_first"] == "fail"]
        recovered = sum(row[f"{arm}_second"] == row[f"{arm}_final"] == "pass" for row in failures)
        repairs[arm] = {"repaired": recovered, "first_failures": len(failures)}
    return {
        "planned": len(rows),
        "first": paired(rows, "first"),
        "repair": repairs,
        "final": paired(rows, "final"),
    }


def report(rows):
    languages = {
        language: summarize([row for row in rows if row["task"].startswith(language + "/")])
        for language in COUNTS
    }
    return {
        "total": summarize(rows),
        "languages": languages,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="?", type=Path, default=HERE / "results.csv")
    print(json.dumps(report(load_rows(parser.parse_args().results)), indent=2))


if __name__ == "__main__":
    main()

"""Run and report a full 225-task, two-arm GLM-5.3-Flash Polyglot comparison."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import random

from openai import AsyncOpenAI

from . import glm_campaign as campaign
from . import baseline_reuse
from .glm_dataset import LANGUAGES, UPSTREAM, discover_all
from .sandbox import command


COUNTS = {"python": 34, "javascript": 49, "go": 39, "rust": 30, "cpp": 26, "java": 47}
ARMS = ("A", "R")
PROFILE = campaign.InferenceSettings(
    context_window=1_000_000,
    output_tokens=32_768,
    reasoning_effort="max",
    temperature=1.0,
    top_p=0.95,
    steps=6,
    seconds=1_800,
)
TASK_TOKENS, TASK_CALLS, ROUNDS = 196_608, 12, 2
PUBLIC_REFERENCE_PASSES = 198
RUNNABLE_PREFLIGHT = {"eligible", "stub_passes"}
WEYRA_SKILL = (
    campaign.BASE_SKILL + " Before the first submission, check the saved implementation against "
    "the exact interface and reason through the stated examples, empty cases, boundary conditions "
    "and state transitions where applicable. Fix any discrepancy before finishing."
)


def dataset_at(upstream):
    """Load exactly the unmodified, pinned corpus in all six languages."""
    rev = command(["git", "-C", str(upstream), "rev-parse", "HEAD"])
    status = command(["git", "-C", str(upstream), "status", "--porcelain"])
    if (
        rev["returncode"]
        or status["returncode"]
        or status["stdout"].strip()
        or rev["stdout"].strip() != UPSTREAM
    ):
        raise ValueError("Use a clean checkout of the pinned Aider Polyglot dataset")
    tasks = list(discover_all(upstream, full_coverage=True))
    counts = {lang: sum(task.language == lang for task in tasks) for lang in LANGUAGES}
    if counts != COUNTS or len({task.id for task in tasks}) != sum(COUNTS.values()):
        raise ValueError("Dataset must have all 225 unique exercises")
    return {task.id: task for task in tasks}


def schedule(tasks):
    """Deterministically interleave languages and balance the first arm within each."""
    jobs = []
    for language in LANGUAGES:
        ids = sorted(key for key in tasks if key.startswith(language + "/"))
        random.Random("Weyra-full-20261121:" + language).shuffle(ids)
        jobs.extend(
            [key, list(ARMS if index % 2 == 0 else ARMS[::-1])] for index, key in enumerate(ids)
        )
    random.Random(20261121).shuffle(jobs)
    return jobs


def paired_counts(rows):
    """Report wins, losses, ties and all missing pairs on the fixed denominator."""
    pairs = [
        row
        for row in rows
        if all((row.get(arm) or {}).get("status") in campaign.OUTCOMES for arm in ARMS)
    ]
    comparison = campaign.compare(pairs, *ARMS)
    return {
        "planned": len(rows),
        "missing_pairs": len(rows) - len(pairs),
        **comparison,
        "baseline_passes": sum(row["A"]["status"] == "pass" for row in pairs),
        "weyra_passes": sum(row["R"]["status"] == "pass" for row in pairs),
        "baseline_first_passes": sum(
            row["A"].get("rounds", [{}])[0].get("verdict") == "pass"
            for row in pairs
            if row["A"].get("rounds")
        ),
        "weyra_first_passes": sum(
            row["R"].get("rounds", [{}])[0].get("verdict") == "pass"
            for row in pairs
            if row["R"].get("rounds")
        ),
    }


def report(rows):
    """Require the full denominator before emitting any success claim."""
    total = paired_counts(rows)
    by_language = {
        lang: paired_counts([row for row in rows if row["id"].startswith(lang + "/")])
        for lang in LANGUAGES
    }
    complete = len({row["id"] for row in rows}) == sum(COUNTS.values()) == len(rows) and all(
        by_language[lang]["paired"] == count for lang, count in COUNTS.items()
    )
    strict_gains = complete and all(data["net_passes"] > 0 for data in by_language.values())
    public_score_reached = complete and total["weyra_passes"] >= PUBLIC_REFERENCE_PASSES
    no_regressions = complete and total["losses"] == 0
    return {
        "coverage_complete": complete,
        "strict_gain_each_language": strict_gains,
        "no_task_regressions": no_regressions,
        "all_tasks_solved_by_weyra": complete and total["weyra_passes"] == sum(COUNTS.values()),
        "external_reference": {
            "passes": PUBLIC_REFERENCE_PASSES,
            "task_count": sum(COUNTS.values()),
            "numerical_target_reached": public_score_reached,
        },
        "target_met": strict_gains and no_regressions and public_score_reached,
        "all": total,
        "languages": by_language,
        "rows": rows,
    }


def save_report(output, tasks, status):
    """Write an auditable report after each batch, including unfinished tasks."""
    rows = [
        {
            "id": task_id,
            **{arm: campaign.result_for(output / "formal", task_id, arm) for arm in ARMS},
        }
        for task_id in tasks
    ]
    result = {"campaign_status": status, **report(rows)}
    campaign.atomic(output / "full-report.json", result)
    return result


def prepare(output, tasks, images):
    """Freeze case IDs for all tasks whose reference passes, including passing stubs."""
    preflight = {}
    with ThreadPoolExecutor(max_workers=4) as pool:

        def prepare_task(item):
            task_id, task = item
            folder = output / "preflight" / task_id
            stamp = campaign.check_identity(task, images[task.language])
            saved = folder / "preflight.json"
            if saved.is_file():
                check = campaign.read(saved)
                if check["identity"] != stamp:
                    raise ValueError(f"Verification inputs changed for {task_id}")
                if check["result"]["status"] in RUNNABLE_PREFLIGHT:
                    return task_id, check
            try:
                check = {
                    "identity": stamp,
                    "result": campaign.preflight_one(
                        task,
                        images[task.language],
                        folder,
                    ),
                }
            except (OSError, RuntimeError) as error:
                check = {
                    "identity": stamp,
                    "result": {
                        "status": "environment_error",
                        "cases": [],
                        "error_type": type(error).__name__,
                    },
                }
            campaign.atomic(saved, check)
            return task_id, check

        for task_id, check in pool.map(prepare_task, tasks.items()):
            preflight[task_id] = check
            print("preflight", task_id, check["result"]["status"], flush=True)
    campaign.atomic(output / "full-preflight.json", preflight)
    return preflight


def runnable_checks(preflight, tasks, images):
    """Keep passing starters and reject missing, mismatched or unverified cases."""
    if set(preflight) != set(tasks):
        raise ValueError("Preflight must list all 225 tasks")
    checks, blocked = {}, {}
    for task_id, task in tasks.items():
        entry = preflight[task_id]
        observed = entry["result"]
        image = images[task.language]
        if entry["identity"] != campaign.check_identity(task, image):
            blocked[task_id] = "identity_mismatch"
        elif observed["status"] not in RUNNABLE_PREFLIGHT or not observed["cases"]:
            blocked[task_id] = observed["status"]
        else:
            checks[task_id] = {"image": image, "cases": observed["cases"]}
    return checks, blocked


async def dispatch(pending, group, ledger, cap, parallel, stop, checkpoint):
    """Refill free worker slots while retaining a conservative budget for active pairs."""
    cursor, status = 0, "completed"
    active = {}
    async with asyncio.TaskGroup() as workers:
        while cursor < len(pending) or active:
            while cursor < len(pending) and len(active) < parallel and not stop.is_set():
                task_id, arms = pending[cursor]
                amount = len(arms) * TASK_TOKENS
                if ledger.totals()[0] + sum(active.values()) + amount > cap:
                    break
                active[workers.create_task(group(task_id, arms))] = amount
                cursor += 1
            if not active:
                if cursor < len(pending) and not stop.is_set():
                    status = "campaign_budget"
                break
            done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            for job in done:
                del active[job]
                if reason := job.result():
                    status = reason
                    stop.set()
            checkpoint()
    return status


async def run(output, tasks, checks, order, cap, parallel):
    """Use a metered client only after the full task inventory passed preflight."""
    key = os.environ.get("ZAI_API_KEY") or (
        campaign.KEY_FILE.read_text().strip() if campaign.KEY_FILE.is_file() else ""
    )
    if not key:
        raise ValueError("Set ZAI_API_KEY for the official GLM Flash API")
    formal = output / "formal"
    formal.mkdir(exist_ok=True)
    with campaign.Ledger(formal / "usage.db", cap, TASK_TOKENS, TASK_CALLS) as ledger:
        async with AsyncOpenAI(
            api_key=key, base_url=campaign.ENDPOINT, max_retries=0, timeout=240
        ) as client:
            stop = asyncio.Event()

            async def group(task_id, arms):
                for arm in arms:
                    if stop.is_set():
                        break
                    result = await campaign.run_arm(
                        formal,
                        tasks[task_id],
                        arm,
                        WEYRA_SKILL if arm == "R" else campaign.BASE_SKILL,
                        checks[task_id],
                        ledger,
                        client,
                        round_limit=ROUNDS,
                        settings=PROFILE,
                        resume_infrastructure=True,
                    )
                    print("formal", task_id, arm, result["status"], flush=True)
                    if reason := campaign.campaign_stop_reason(result):
                        stop.set()
                        return reason
                return None

            pending = []
            for task_id, arms in order:
                remaining = []
                for arm in arms:
                    previous = campaign.result_for(formal, task_id, arm)
                    if previous is None or (
                        previous["status"] == "infrastructure_error"
                        and not (formal / task_id / arm / "attempt-2/result.json").is_file()
                    ):
                        remaining.append(arm)
                if remaining:
                    pending.append((task_id, remaining))
            status = await dispatch(
                pending, group, ledger, cap, parallel, stop,
                lambda: save_report(output, tasks, "running"),
            )
    result = save_report(output, tasks, status)
    if status == "completed" and not result["coverage_complete"]:
        result = save_report(output, tasks, "incomplete_pairs")
    print(result["campaign_status"], result["all"]["paired"], "/", sum(COUNTS.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inventory", "prepare", "run", "report"))
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--token-cap", type=int)
    parser.add_argument("--parallel", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--reuse-baseline", type=Path)
    args = parser.parse_args()
    tasks = dataset_at(args.upstream)
    if args.action == "inventory":
        print(
            json.dumps(
                {lang: [key for key in tasks if key.startswith(lang + "/")] for lang in LANGUAGES},
                indent=2,
            )
        )
        return
    if args.output is None:
        parser.error("--output is required")
    with campaign.exclusive(args.output):
        run_path = args.output / "full-protocol.json"
        if (args.output / "formal").exists() and not run_path.is_file():
            parser.error("Use a new output directory or the original full-run protocol")
        if args.action == "report":
            path = args.output / "full-report.json"
            existing = campaign.read(path)["campaign_status"] if path.is_file() else "unscored"
            print(json.dumps(save_report(args.output, tasks, existing)["all"], indent=2))
            return
        images = campaign.image_ids(LANGUAGES)
        if set(images) != set(LANGUAGES):
            parser.error("All six Docker toolchain images must be accessible")
        source = {"dataset": UPSTREAM, "source": campaign.source_hash(), "images": images}
        manifest = args.output / "full-preflight-source.json"
        if args.action == "prepare":
            campaign.freeze(manifest, source)
            inventory = prepare(args.output, tasks, images)
        else:
            if not manifest.is_file() or campaign.read(manifest) != source:
                parser.error("Run prepare with the same source and Docker images")
            inventory = campaign.read(args.output / "full-preflight.json")
        checks, blocked = runnable_checks(inventory, tasks, images)
        if blocked:
            campaign.atomic(args.output / "full-preflight-blocked.json", blocked)
            parser.error(
                f"{len(blocked)} tasks lack a verified grader. See full-preflight-blocked.json"
            )
        (args.output / "full-preflight-blocked.json").unlink(missing_ok=True)
        if args.action == "run":
            if args.token_cap is None or args.token_cap < TASK_TOKENS * len(ARMS):
                parser.error("--token-cap must cover at least one complete pair")
            order = schedule(tasks)
            protocol = {
                **source,
                "model": campaign.MODEL,
                "endpoint": campaign.ENDPOINT,
                "profile": asdict(PROFILE),
                "checks": checks,
                "order": order,
                "task_tokens": TASK_TOKENS,
                "task_calls": TASK_CALLS,
                "rounds": ROUNDS,
                "token_cap": args.token_cap,
                "baseline_skill": campaign.BASE_SKILL,
                "weyra_skill": WEYRA_SKILL,
                "system": campaign.SYSTEM,
                "tool_names": list(campaign.TOOL_NAMES),
                "tasks": {task_id: task.metadata() for task_id, task in tasks.items()},
            }
            if args.reuse_baseline is not None:
                protocol["baseline_reuse"] = baseline_reuse.snapshot(
                    args.reuse_baseline, protocol, tasks,
                )
            campaign.freeze(run_path, protocol)
            if args.reuse_baseline is not None:
                baseline_reuse.restore(args.reuse_baseline, args.output, protocol["baseline_reuse"])
            asyncio.run(run(args.output, tasks, checks, order, args.token_cap, args.parallel))


if __name__ == "__main__":
    main()

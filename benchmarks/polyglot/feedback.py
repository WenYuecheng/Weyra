"""Frozen 24-task trial: first submission, then at most one test-feedback repair."""

import argparse
import asyncio
from collections import Counter
import csv
from decimal import Decimal
import fcntl
import hashlib
import json
from pathlib import Path
import statistics
import time

from weyra.coding.agent import CodingAgent, DEFAULT_SYSTEM
from weyra.contracts import digest
from weyra.core.state import BudgetExceeded
from weyra.llm.providers import ModelError
from weyra.session import SessionStore
from .budget_v2 import FeedbackBudget
from .dataset import Task, discover
from .grading_v2 import verify
from .runner import atomic, bind_tools, isolation, load_once, runtime_config, save_once, source_hash
from .sandbox import Sandbox, command

SYSTEM = (
    DEFAULT_SYSTEM
    + """\nWorkspace tools operate inside /work in an isolated container. Use project-relative paths.
Implement the exercise by editing only the implementation files; inspect them before editing.
Official tests are hidden. You may write and run your own tests, using installed dependencies only.
Save your implementation before finishing. Report only self-tests actually run.
If a later user message provides official failure feedback, make one focused repair using that report.
Do not access the network or change test/dependency configuration."""
)

LIMITS = {
    "total_cny": "40",
    "check_cny": "4",
    "formal_cny": "36",
    "first_cny": "1",
    "repair_cny": ".5",
    "task_cny": "1.5",
    "first_steps": 20,
    "repair_steps": 10,
    "physical_calls": 80,
    "first_seconds": 300,
    "repair_seconds": 180,
    "verifier_seconds": 120,
    "feedback_characters": 6000,
    "parallel": 4,
}


async def awaited_thread(function, *args):
    """Drain an already-started operation before cancellation cleans up its resources."""
    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await operation
        raise


async def gather_workers(workers):
    tasks = [asyncio.create_task(worker) for worker in workers]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def repair_allowed(first):
    verification = first.get("verification", {})
    return (
        first.get("agent_end")
        not in {
            "provider_error",
            "container_error",
            "cancelled",
            "interrupted_unknown",
        }
        and verification.get("verdict") == "fail"
        and verification.get("eligible_feedback") is True
    )


async def run_stages(folder, protocol, attempt, grade):
    """Seal each submission before disclosing feedback; never reacquire an interrupted attempt."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / "result.json"
    if final.exists():
        return load_once(final, protocol)
    if (folder / "started.json").exists():
        load_once(folder / "started.json", protocol)
        row = {"protocol": protocol, "interrupted": True, "repair": None}
        for stage in ("first", "repair"):
            sealed = folder / stage / "result.json"
            if sealed.exists():
                row[stage] = load_once(sealed, protocol)
            elif (folder / stage / "started.json").exists():
                draft = folder / stage / "submission.json"
                row[stage] = (
                    load_once(draft, protocol)
                    if draft.exists()
                    else {
                        "agent_end": "interrupted_unknown",
                        "model_steps": None,
                        "physical_requests": None,
                        "tool_calls": None,
                    }
                )
                row[stage]["verification"] = {"verdict": "not_run"}
        row.setdefault(
            "first",
            {
                "agent_end": "interrupted_unknown",
                "model_steps": None,
                "physical_requests": None,
                "tool_calls": None,
                "verification": {"verdict": "not_run"},
            },
        )
        save_once(final, row)
        return row
    save_once(folder / "started.json", {"protocol": protocol, "at": time.time()})
    row = {"protocol": protocol, "interrupted": False, "repair": None}
    for stage in ("first", "repair"):
        if stage == "repair" and not repair_allowed(row["first"]):
            break
        destination = folder / stage
        destination.mkdir(parents=True, exist_ok=True)
        save_once(destination / "started.json", {"protocol": protocol, "at": time.time()})
        feedback = (
            row["first"]["verification"].get("feedback", "")[:6000] if stage == "repair" else ""
        )
        if stage == "repair":
            save_once(destination / "feedback.json", {"protocol": protocol, "text": feedback})
        record, files = await attempt(stage, feedback)
        artifacts = destination / "artifacts"
        artifacts.mkdir(exist_ok=True)
        for path, content in files.items():
            target = artifacts / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        record.update(
            protocol=protocol,
            artifact_sha256={p: hashlib.sha256(v).hexdigest() for p, v in files.items()},
        )
        # Persist candidate and metrics before a fresh verifier ever sees them.
        save_once(destination / "submission.json", record)
        if record.get("artifact_error"):
            verification = {
                "verdict": "environment_error",
                "classification": "artifact_error",
                "eligible_feedback": False,
            }
        else:
            verification = await grade(files, destination / "verification")
        record["verification"] = verification
        save_once(destination / "result.json", record)
        row[stage] = record
    save_once(final, row)
    return row


def _end(exc):
    if isinstance(exc, BudgetExceeded):
        return "budget_stopped"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if type(exc).__name__ in ("ContextOverflow", "ContextCapacityError") or (
        isinstance(exc, RuntimeError) and "summary" in str(exc).lower()
    ):
        return "compaction_failure"
    if isinstance(exc, ModelError):
        return "provider_error"
    if isinstance(exc, RuntimeError) and "step limit" in str(exc):
        return "step_stopped"
    return "tool_error"


async def one_task(
    task, root, image, config, budget, protocol, expected_cases, phase="formal", runtime=None
):
    folder = root / ("tasks" if phase == "formal" else "checks") / task.id
    box = Sandbox(image)
    agent = None
    own_runtime = runtime is None
    runtime = runtime or config.build_runtime()
    runtime.fallbacks = {}
    runtime.skill_evolution = {"enabled": False}
    runtime.mcp_servers = {}
    setup_error = None

    async def attempt(stage, feedback):
        nonlocal agent, setup_error
        budget.begin(task.id, phase, stage)
        began = time.monotonic()
        end, answer, artifact_error = "normal", "", None
        before = len(agent.session.records) if agent else 0
        try:
            if agent is None:
                try:
                    await awaited_thread(box.create, task.files)
                    atomic(
                        folder / "agent-isolation.json",
                        isolation(await asyncio.to_thread(box.inspect)),
                    )
                    agent = CodingAgent(
                        folder / "agent-state",
                        runtime,
                        budget=budget,
                        system=SYSTEM,
                        max_steps=20,
                        enable_memory=False,
                        enable_evolution=False,
                        permission_mode="bypassPermissions",
                        owns_runtime=False,
                    )
                    atomic(folder / "tools.json", bind_tools(agent, box))
                    before = len(agent.session.records)
                except Exception as exc:
                    setup_error = type(exc).__name__
                    raise
            agent.max_steps = 20 if stage == "first" else 10
            box.versions = {}
            prompt = (
                f"Implement this official {task.language} exercise. Preserve the starting interfaces.\n"
                "Implementation files: "
                + ", ".join(task.editable)
                + "\n\n"
                + task.instruction
                + "\n\nSave the implementation and run useful self-tests. "
                "Official verification has not run yet."
                if stage == "first"
                else "Your first submission has been frozen and independently tested. "
                "This is the only feedback repair round. Read your current files, fix the implementation, "
                "and save it. Do not change test configuration. Official failure report:\n\n"
                + feedback
            )
            answer = await asyncio.wait_for(
                agent.run_once(prompt), 300 if stage == "first" else 180
            )
            turn = agent.session.latest("turn_end") or {}
            if turn.get("state") not in ("completed", "success", "done"):
                end = turn.get("state", "normal")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            end = "container_error" if setup_error else _end(exc)
            # Exception messages may contain provider secrets; persist only safe fields.
            answer = type(exc).__name__
        files = {}
        try:
            files = await asyncio.to_thread(box.export, list(task.editable))
        except Exception as exc:
            artifact_error = type(exc).__name__
        records = agent.session.records[before:] if agent else []
        events = [r["data"] for r in records if r["kind"] == "model_usage"]
        attempts = [r["data"] for r in records if r["kind"] == "attempt_end"]
        steps = sum(
            r["kind"] == "message" and r["data"].get("role") == "assistant" for r in records
        )
        if agent:
            agent.session.export_jsonl(folder / stage / "trajectory.jsonl")
        return {
            "agent_end": end,
            "answer": answer,
            "artifact_error": artifact_error,
            "model_steps": steps,
            "physical_requests": len(events),
            "tool_calls": sum(r["kind"] == "tool_intent" for r in records),
            "usage": events,
            "cost_cny": str(budget.stage_total(task.id, stage, phase)),
            "seconds": time.monotonic() - began,
            "attempts": attempts,
            "compression_failures": sum(r["kind"] == "compression_failure" for r in records),
            "unchanged": all(files.get(p) == task.files[p].encode() for p in task.editable),
        }, files

    async def grade(files, destination):
        return await asyncio.to_thread(verify, task, image, files, destination, expected_cases)

    try:
        result = await run_stages(folder, protocol, attempt, grade)
        return {"id": task.id, **result}
    finally:
        errors = []
        for name, action in (
            ("agent", agent.close(close_runtime=False) if agent else None),
            ("runtime", runtime.close() if own_runtime else None),
            ("container", asyncio.to_thread(box.close)),
        ):
            if action is not None:
                try:
                    await action
                except Exception as exc:
                    errors.append({"resource": name, "exception_type": type(exc).__name__})
        folder.mkdir(parents=True, exist_ok=True)
        atomic(folder / "cleanup.json", {"removed": not errors, "errors": errors})


def freeze(root, image, upstream):
    selection = json.loads((root / "selection.json").read_text())
    assert len(selection["tasks"]) == 24
    assert Counter(t["language"] for t in selection["tasks"]) == {"python": 12, "javascript": 12}
    _, config = runtime_config()
    p = {
        "version": "weyra-polyglot-improvement-v2",
        "tasks": selection["tasks"],
        "dataset_commit": command(["git", "-C", str(upstream), "rev-parse", "HEAD"])[
            "stdout"
        ].strip(),
        "dataset_url": "https://github.com/Aider-AI/polyglot-benchmark",
        "source_hash": source_hash(),
        "image": image,
        "image_id": json.loads(command(["docker", "image", "inspect", image])["stdout"])[0]["Id"],
        "system": SYSTEM,
        "config": config,
        "limits": LIMITS,
        "reference_cases": {
            r["id"]: r["stats"]["case_ids"]
            for r in selection["preflight"]
            if r["id"] in {t["id"] for t in selection["tasks"]}
        },
        "excluded": [r for r in selection["preflight"] if r["verdict"] != "pass"],
        "previously_used_or_excluded": selection["prior_excluded"],
        "cost_basis": "approved_conservative_ceiling_not_provider_invoice",
        "scoring": "fixed denominator24; first attempt and after at most one feedback repair",
        "resources": "fresh task session; same session/workspace for repair; no memory/evolution; six container tools",
        "recovery": "sealed results reused; interrupted tasks not restarted or granted new attempts",
        "test_enablement": "upstream xtest( to test(; no other official test changes",
        "source_license": "upstream Exercism attribution and per-task LICENSE retained in upstream clone",
    }
    p["fingerprint"] = digest(p)
    if (root / "protocol.json").exists():
        if json.loads((root / "protocol.json").read_text()) != p:
            raise ValueError("Refusing protocol overwrite")
    else:
        save_once(root / "protocol.json", p)
    return p


def report(root, rows, budget):
    def first_pass(r):
        return r["first"].get("verification", {}).get("verdict") == "pass"

    def final_pass(r):
        return (
            first_pass(r)
            or (r.get("repair") or {}).get("verification", {}).get("verdict") == "pass"
        )

    first = sum(first_pass(r) for r in rows)
    final = sum(final_pass(r) for r in rows)
    stages = [r[s] for r in rows for s in ("first", "repair") if r.get(s)]
    steps = [
        sum((r.get(s) or {}).get("model_steps", 0) or 0 for s in ("first", "repair"))
        for r in rows
        if all((r.get(s) or {}).get("model_steps", 0) is not None for s in ("first", "repair"))
    ]
    costs = {p: str(budget.phase_cost(p)) for p in ("formal", "check")}
    counts = Counter(a.get("agent_end") for a in stages)
    summary = {
        "denominator": 24,
        "completed_tasks": len(rows),
        "first_passed": first,
        "first_attempt_pass_rate": first / 24,
        "repair_added_passed": final - first,
        "final_passed": final,
        "after_one_feedback_pass_rate": final / 24,
        "repair_attempted": sum(r.get("repair") is not None for r in rows),
        "languages": {
            lang: {
                "denominator": 12,
                "first_passed": sum(first_pass(r) for r in rows if r["id"].startswith(lang + "/")),
                "repair_added_passed": sum(
                    final_pass(r) and not first_pass(r)
                    for r in rows
                    if r["id"].startswith(lang + "/")
                ),
                "final_passed": sum(final_pass(r) for r in rows if r["id"].startswith(lang + "/")),
            }
            for lang in ("python", "javascript")
        },
        "cost_cny": costs,
        "total_added_cost_cny": str(sum(map(Decimal, costs.values()))),
        "cost_basis": "conservative_ceiling_not_provider_invoice",
        "cost_per_planned_task_cny": str(Decimal(costs["formal"]) / 24),
        "cost_per_passed_task_cny": str(Decimal(costs["formal"]) / final) if final else None,
        "model_steps": {
            "sum": sum(steps),
            "mean": statistics.mean(steps) if steps else None,
            "median": statistics.median(steps) if steps else None,
            "distribution": steps,
        },
        "unknown_step_stages": sum(a.get("model_steps") is None for a in stages),
        "physical_requests": sum(r["scope"].startswith("formal/") for r in budget.records)
        + sum(n for scope, n in budget.recovered_calls.items() if scope.startswith("formal/")),
        "tool_calls": sum(a.get("tool_calls", 0) or 0 for a in stages),
        "unknown_tool_stages": sum(a.get("tool_calls") is None for a in stages),
        "compression_failures": sum(a.get("compression_failures", 0) for a in stages),
        "input_tokens": sum(
            u.get("input_tokens", 0) or 0 for a in stages for u in a.get("usage", [])
        ),
        "output_tokens": sum(
            u.get("output_tokens", 0) or 0 for a in stages for u in a.get("usage", [])
        ),
        "unknown_usage_requests": sum(
            u.get("input_tokens") is None or u.get("output_tokens") is None
            for a in stages
            for u in a.get("usage", [])
        ),
        "agent_seconds": sum(a.get("seconds", 0) for a in stages),
        "agent_end_counts": dict(counts),
        "verification_counts": dict(
            Counter(a.get("verification", {}).get("classification", "not_run") for a in stages)
        ),
        "compression_length_responses": sum(
            a.get("purpose") == "compression" and a.get("stop_reason") == "length"
            for s in stages
            for a in s.get("attempts", [])
        ),
        "limits": LIMITS,
    }
    atomic(root / "summary.json", summary)
    fields = [
        "id",
        "first_verdict",
        "repair_verdict",
        "first_end",
        "repair_end",
        "first_steps",
        "repair_steps",
        "physical_requests",
        "cost_cny",
        "final_reason",
    ]
    with (root / "results.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        for r in rows:
            a, b = r["first"], r.get("repair") or {}
            writer.writerow(
                {
                    "id": r["id"],
                    "first_verdict": a.get("verification", {}).get("verdict"),
                    "repair_verdict": b.get("verification", {}).get("verdict", "not_run"),
                    "first_end": a.get("agent_end"),
                    "repair_end": b.get("agent_end", ""),
                    "first_steps": a.get("model_steps", 0),
                    "repair_steps": b.get("model_steps", 0),
                    "physical_requests": None
                    if a.get("physical_requests") is None
                    or (b and b.get("physical_requests") is None)
                    else a.get("physical_requests", 0) + b.get("physical_requests", 0),
                    "cost_cny": str(budget.task_total(r["id"])),
                    "final_reason": (b or a)
                    .get("verification", {})
                    .get("classification", "not_run"),
                }
            )
    return summary


def validate_checks(root, protocol, source):
    connection = load_once(root / "check-result.json", protocol)
    passed = any(
        (connection.get(stage) or {}).get("verification", {}).get("verdict") == "pass"
        for stage in ("first", "repair")
    )
    offline = json.loads((root / "offline-feedback-check.json").read_text())
    if not passed or offline.get("passed") is not True or offline.get("source_hash") != source:
        raise ValueError("Successful current connection and feedback checks required")


async def run(root, image, upstream, check=False):
    p = json.loads((root / "protocol.json").read_text())
    copy = dict(p)
    fingerprint = copy.pop("fingerprint")
    if digest(copy) != fingerprint:
        raise ValueError("Protocol corrupted")
    config, safe = runtime_config()
    current_source = source_hash()
    source_ok = current_source == p["source_hash"]
    amendment_file = root / "recovery-amendment.json"
    if not source_ok and amendment_file.exists():
        amendment = json.loads(amendment_file.read_text())
        source_ok = (
            amendment.get("original_protocol") == fingerprint
            and amendment.get("original_source_hash") == p["source_hash"]
            and amendment.get("source_hash") == current_source
            and amendment.get("changed_files") == ["benchmarks/polyglot/feedback.py"]
        )
    if safe != p["config"] or not source_ok:
        raise ValueError("Configuration/source changed after freeze")
    if (
        json.loads(command(["docker", "image", "inspect", image])["stdout"])[0]["Id"]
        != p["image_id"]
    ):
        raise ValueError("Image changed")
    tasks = {t.id: t for t in discover(upstream)}
    chosen = [tasks[t["id"]] for t in p["tasks"]]
    if [t.metadata() for t in chosen] != p["tasks"]:
        raise ValueError("Dataset changed")
    ledger = SessionStore(project=root / "ledger", session_id="polyglot-v2-budget")
    budget = FeedbackBudget()
    budget.attach(ledger)
    rows = []
    try:
        if check:
            task = Task(
                "python/development-check",
                "python",
                {
                    "INSTRUCTIONS.md": "Implement add(a,b). Return the numeric sum.",
                    "add.py": "def add(a, b):\n    pass\n",
                },
                {
                    "add.py": "",
                    "test_add.py": "from add import add\ndef test_add():\n assert add(2, 3) == 5\n assert add(-1, 1) == 0\n",
                },
                {},
                ("add.py",),
                ("python", "-m", "pytest", "-q", "--junitxml=/tmp/junit.xml"),
                "Implement add(a,b), returning the numeric sum. Preserve the interface.",
            )
            row = await one_task(task, root, image, config, budget, fingerprint, None, "check")
            atomic(root / "check-result.json", row)
            if (
                row["first"]["verification"]["verdict"] != "pass"
                and not (row.get("repair") or {}).get("verification", {}).get("verdict") == "pass"
            ):
                raise RuntimeError("Development connection/tool check did not pass")
        else:
            validate_checks(root, fingerprint, p["source_hash"])
            semaphore = asyncio.Semaphore(4)

            async def worker(task):
                async with semaphore:
                    row = await one_task(
                        task,
                        root,
                        image,
                        config,
                        budget,
                        fingerprint,
                        p["reference_cases"][task.id],
                    )
                    rows.append(row)
                    summary = report(root, rows, budget)
                    print(
                        task.id,
                        row["first"]["verification"]["verdict"],
                        (row.get("repair") or {}).get("verification", {}).get("verdict", "-"),
                        "aggregate",
                        summary["first_passed"],
                        summary["final_passed"],
                        "cost",
                        budget.phase_cost("formal"),
                        flush=True,
                    )

            await gather_workers(worker(t) for t in chosen)
            report(root, rows, budget)
    finally:
        ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "check", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--image", default="weyra-polyglot:20261023")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.mode == "freeze":
            freeze(root, args.image, args.upstream.resolve())
        else:
            asyncio.run(run(root, args.image, args.upstream.resolve(), args.mode == "check"))


if __name__ == "__main__":
    main()

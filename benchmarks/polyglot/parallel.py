"""User-approved scheduling amendment; retains frozen agent/model/test protocol."""

import argparse
import asyncio
from contextvars import ContextVar
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from weyra.contracts import digest
from weyra.session import SessionStore
from .accounting import CampaignBudget, summarize
from .dataset import discover
from .runner import BASE, atomic, load_once, one_task, runtime_config, save_once
from .sandbox import command


class ParallelBudget(CampaignBudget):
    def __init__(self):
        self._active_context = ContextVar("polyglot_task", default=None)
        super().__init__()

    @property
    def active(self):
        return self._active_context.get()

    @active.setter
    def active(self, value):
        self._active_context.set(value)


def base_source_hash():
    return digest(
        {
            str(p.relative_to(BASE)): hashlib.sha256(p.read_bytes()).hexdigest()
            for top in ("weyra", "benchmarks/polyglot")
            for p in sorted((BASE / top).rglob("*.py"))
            if p != Path(__file__).resolve()
        }
    )


async def run(root, upstream, image):
    protocol = json.loads((root / "protocol.json").read_text())
    fp = protocol["fingerprint"]
    copy = dict(protocol)
    copy.pop("fingerprint")
    if digest(copy) != fp or base_source_hash() != protocol["source_hash"]:
        raise ValueError("Frozen agent/tool/model source changed")
    config, safe = runtime_config()
    if config.safe_summary() != protocol["config"]:
        raise ValueError("Configuration changed")
    if (
        json.loads(command(["docker", "image", "inspect", image])["stdout"])[0]["Id"]
        != protocol["image_id"]
    ):
        raise ValueError("Image changed")
    all_tasks = {t.id: t for t in discover(upstream)}
    tasks = [all_tasks[t["id"]] for t in protocol["tasks"]]
    if [t.metadata() for t in tasks] != protocol["tasks"]:
        raise ValueError("Task data changed")
    amendment = {
        "base_protocol": fp,
        "max_parallel": 4,
        "model_tools_tests_unchanged": True,
        "reason": "explicit user approval to accelerate completion",
        "parallel_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    ap = root / "parallel-amendment.json"
    if ap.exists():
        if json.loads(ap.read_text()) != amendment:
            raise ValueError("Scheduling amendment changed")
    else:
        save_once(ap, amendment)
    for task in tasks:
        folder = root / "tasks" / task.id
        marker = folder / "started.json"
        if marker.exists() and json.loads(marker.read_text()).get("scheduling_claim"):
            marker.rename(folder / "scheduling-claim.json")
    if (root / "summary.json").exists() and not (root / "serial-handoff-summary.json").exists():
        shutil.copyfile(root / "summary.json", root / "serial-handoff-summary.json")
    ledger = SessionStore(project=root / "ledger", session_id="polyglot40-budget")
    budget = ParallelBudget()
    budget.attach(ledger)
    results = {
        t.id: load_once(root / "tasks" / t.id / "result.json", fp)
        for t in tasks
        if (root / "tasks" / t.id / "result.json").exists()
    }
    sem = asyncio.Semaphore(4)

    def publish():
        ordered = [results[t.id] for t in tasks if t.id in results]
        costs = {p: str(budget.phase_cost(p)) for p in ("formal", "check")}
        summary = summarize(ordered, costs)
        summary["scheduling_amendment"] = amendment
        summary["completed_tasks"] = len(ordered)
        summary["remaining_tasks"] = [t.id for t in tasks if t.id not in results]
        atomic(root / "summary.json", summary)

    async def work(task):
        if task.id in results:
            return
        async with sem:
            print("start", task.id, flush=True)
            row = await one_task(
                task, root, image, config, budget, fp, protocol["reference_cases"][task.id]
            )
            results[task.id] = row
            publish()
            print(
                "task",
                task.id,
                row["agent_end"],
                row["verifier"],
                "steps",
                row["model_steps"],
                "cost",
                row.get("cost_cny"),
                flush=True,
            )

    try:
        publish()
        await asyncio.gather(*(work(t) for t in tasks))
        publish()
    finally:
        ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--image", default="weyra-polyglot:20261023")
    parser.add_argument("--serial-pid", type=int)
    a = parser.parse_args()
    if a.serial_pid:
        while True:
            try:
                os.kill(a.serial_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(1)
    with (a.output / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(run(a.output.resolve(), a.upstream.resolve(), a.image))


if __name__ == "__main__":
    main()

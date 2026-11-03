"""Budgeted iterative Polyglot repair and the consolidated 64-task campaign."""

import argparse
import asyncio
import base64
import fcntl
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import sqlite3
import time

from weyra.coding.agent import CodingAgent, DEFAULT_SYSTEM
from weyra.contracts import ToolResult, digest
from weyra.core.state import BudgetExceeded
from weyra.session import SessionStore
from weyra.skills import SkillStore
from .budget_v3 import IterativeBudget
from .campaign_v3 import build_catalog, write_report
from .dataset import discover
from .feedback import awaited_thread, gather_workers, _end
from .grading_v2 import verify
from .runner import ALLOWED, atomic, isolation, runtime_config, save_once, source_hash
from .sandbox import Sandbox, command, safe_path

SYSTEM = (
    DEFAULT_SYSTEM
    + """\nWorkspace tools operate inside /work in an isolated container; use project-relative paths.
Implement the public exercise, preserving the starting interfaces. Read files before editing.
You may run self-tests with installed dependencies. Save the implementation before finishing.
Official tests are run independently. Later user messages may provide their failure report: use it to repair the implementation.
Use short, focused tool calls. Do not install dependencies, change test configuration or access the network."""
)
LIMITS = dict(
    task_cny=".95",
    campaign_cny="33.25",
    combined_v2_v3_cny="40",
    model_steps=80,
    physical_requests=120,
    task_seconds=720,
    first_steps=40,
    feedback_steps=20,
    verifier_seconds=120,
    verifier_retries=2,
    feedback_characters=6000,
    parallel=4,
    main_output_tokens=4096,
    summary_output_tokens=2048,
    summary_expanded_tokens=4096,
    context_window=16384,
)


def read(path):
    return json.loads(Path(path).read_text())


def store_files(folder, files):
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        target = folder / safe_path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}


def load_files(folder, hashes):
    result = {}
    for name, sha in hashes.items():
        content = (folder / safe_path(name)).read_bytes()
        if hashlib.sha256(content).hexdigest() != sha:
            raise ValueError("Artifact checkpoint hash mismatch")
        result[name] = content
    return result


async def solve(
    task_id, folder, protocol, budget, attempt, grade, metrics=None, time_limit=720, retry_delay=2
):
    """Persist before verification and continue feedback until a real resource limit."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    progress = folder / "progress.json"
    final = folder / "result.json"
    if final.exists():
        previous = read(final)
        if previous["protocol"] != protocol:
            raise ValueError("Task protocol changed")
        if previous["status"] in ("passed", "resource_exhausted"):
            return previous
    state = (
        read(progress)
        if progress.exists()
        else dict(
            id=task_id,
            protocol=protocol,
            status="running",
            phase="idle",
            rounds=[],
            started_at=time.time(),
            deadline=time.time() + time_limit,
            next_index=0,
        )
    )
    if state["protocol"] != protocol or state["id"] != task_id:
        raise ValueError("Task protocol changed")
    if "resume_remaining_seconds" in state:
        state["segment_started_at"] = time.time()
        state["deadline"] = state["segment_started_at"] + state.pop("resume_remaining_seconds")
        atomic(progress, state)
    if hasattr(budget, "set_deadline"):
        budget.set_deadline(task_id, state["deadline"])
    # Reconcile a sealed result whose progress update was interrupted.
    sealed = sorted((folder / "rounds").glob("*/result.json"))
    state["rounds"] = [read(p) for p in sealed]
    state["next_index"] = max(
        state["next_index"], max((r["index"] + 1 for r in state["rounds"]), default=0)
    )
    if any(r.get("verification", {}).get("verdict") == "pass" for r in state["rounds"]):
        state["status"] = "passed"
    if state.get("phase") == "model":
        destination = folder / "rounds" / f"{state['next_index']:04d}"
        if not (destination / "submission.json").exists():
            destination.mkdir(parents=True, exist_ok=True)
            atomic(
                destination / "interruption.json",
                {"at": time.time(), "model_consumption_retained": True},
            )
            state["next_index"] += 1
            state["phase"] = "idle"
    infra_failures = 0
    while state["status"] != "passed":
        known = (
            metrics()
            if metrics
            else {"model_steps": sum(r.get("model_steps", 0) or 0 for r in state["rounds"])}
        )
        left = state["deadline"] - time.time()
        index = state["next_index"]
        destination = folder / "rounds" / f"{index:04d}"
        submission = destination / "submission.json"
        if not submission.exists() and (
            left < 130
            or known["model_steps"] >= 80
            or budget.task_calls(task_id) >= 120
            or budget.task_remaining(task_id) <= 0
        ):
            state["status"] = "resource_exhausted"
            break
        destination.mkdir(parents=True, exist_ok=True)
        feedback = next(
            (
                r["verification"].get("feedback", "")[:6000]
                for r in reversed(state["rounds"])
                if r.get("verification", {}).get("eligible_feedback")
            ),
            "",
        )
        if submission.exists():
            record = read(submission)
            files = load_files(destination / "artifacts", record["artifact_sha256"])
        else:
            budget.begin(task_id, index)
            state["phase"] = "model"
            atomic(progress, state)
            save_once(
                destination / "started.json",
                {"protocol": protocol, "at": time.time(), "feedback": feedback},
            )
            try:
                record, files = await attempt(
                    index,
                    feedback,
                    min(300, max(1, left - 130)),
                    min(40 if index == 0 else 20, 80 - known["model_steps"]),
                )
            except asyncio.CancelledError:
                atomic(progress, state)
                raise
            except Exception as exc:
                record, files = (
                    {
                        "agent_end": "container_error",
                        "model_steps": 0,
                        "exception_type": type(exc).__name__,
                    },
                    {},
                )
            record.update(
                index=index,
                protocol=protocol,
                artifact_sha256=store_files(destination / "artifacts", files),
            )
            save_once(submission, record)
        state["phase"] = "grading"
        atomic(progress, state)
        verification = None
        for verify_index in range(3):
            verifier_folder = destination / "verification" / f"{verify_index:02d}"
            saved = verifier_folder / "verification.json"
            if saved.exists():
                verification = read(saved)
            else:
                if state["deadline"] - time.time() < 120:
                    verification = {
                        "verdict": "not_run",
                        "classification": "time_exhausted",
                        "eligible_feedback": False,
                    }
                    break
                try:
                    verification = await grade(files, verifier_folder, 120)
                except Exception as exc:
                    verification = {
                        "verdict": "environment_error",
                        "classification": "verification_error",
                        "exception_type": type(exc).__name__,
                        "eligible_feedback": False,
                    }
                verifier_folder.mkdir(parents=True, exist_ok=True)
                atomic(saved, verification)
            if verification["verdict"] in ("pass", "fail"):
                break
            if verify_index < 2 and retry_delay:
                await asyncio.sleep(retry_delay)
        record["verification"] = verification
        save_once(destination / "result.json", record)
        state["rounds"].append(record)
        state["next_index"] = index + 1
        state["phase"] = "idle"
        state["status"] = "running"
        atomic(progress, state)
        if verification["verdict"] == "pass":
            state["status"] = "passed"
        elif (
            record["agent_end"] == "budget_stopped"
            and not record.get("infrastructure_reclassified")
        ) or verification["verdict"] == "not_run":
            state["status"] = "resource_exhausted"
            break
        elif verification["verdict"] == "environment_error":
            state["status"] = "infrastructure_error"
            break
        elif record["agent_end"] == "container_error":
            infra_failures += 1
            if infra_failures >= 3:
                state["status"] = "infrastructure_error"
                break
        else:
            infra_failures = 0
    known = (
        metrics()
        if metrics
        else {"model_steps": sum(r.get("model_steps", 0) or 0 for r in state["rounds"])}
    )
    state.update(
        model_steps=known["model_steps"],
        physical_requests=budget.task_calls(task_id),
        cost_cny=str(budget.task_total(task_id)),
        seconds=state.get("elapsed_before", 0)
        + time.time()
        - state.get("segment_started_at", state["started_at"]),
        result_path=str(final.resolve()),
    )
    atomic(progress, state)
    atomic(final, state)
    return state


async def run_independent(items, worker, report):
    rows = []
    semaphore = asyncio.Semaphore(4)

    async def isolated(item):
        async with semaphore:
            try:
                row = await worker(item)
            except Exception as exc:
                row = {
                    "id": getattr(item, "id", item),
                    "status": "infrastructure_error",
                    "exception_type": type(exc).__name__,
                    "rounds": [],
                    "model_steps": None,
                    "physical_requests": None,
                    "cost_cny": None,
                }
            rows.append(row)
            try:
                report(rows)
            except Exception:
                # Reports are derived; a failure here must never cancel paid work.
                pass
            return row

    await gather_workers(isolated(item) for item in items)
    return rows


class TaskHost:
    def __init__(self, task, folder, image, config, budget, runtime=None):
        self.task, self.folder, self.image, self.budget = task, Path(folder), image, budget
        self.runtime = runtime or config.build_runtime()
        self.runtime.fallbacks = {}
        self.runtime.skill_evolution = {"enabled": False}
        self.runtime.mcp_servers = {}
        provider = self.runtime.registry.get("cun")
        if hasattr(provider, "_client"):
            provider._client().timeout = 60.0
        self.agent = None
        self.box = None
        self.lock = asyncio.Lock()
        self.identity_path = self.folder / "session.json"
        self.checkpoint_path = self.folder / "workspace.json"
        self.operation_id = 0
        self.deadline = None

    async def classify_connection_failures(self):
        if not self.agent:
            return
        rows = self.agent.session.records
        failures = {
            r["data"]["attempt_id"]: r["data"]
            for r in rows
            if r["kind"] == "attempt_end"
            and r["data"].get("state") == "failed"
            and r["data"].get("exception_type") in ("APIConnectionError", "APITimeoutError")
            and r["data"].get("usage") is None
        }
        for row in rows:
            if row["kind"] != "model_usage":
                continue
            usage = row["data"]
            error = failures.get(usage.get("attempt_id"))
            if error and usage.get("input_tokens") is None and usage.get("output_tokens") is None:
                await self.budget.classify_infrastructure(
                    usage["reservation"],
                    error["exception_type"],
                    digest(
                        {"error": error, "usage": usage, "policy": "separate_connection_reserve_v1"}
                    ),
                )

    def metrics(self):
        db = self.folder / "agent-state/.weyra/state.db"
        if not db.exists():
            return {"model_steps": 0}
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM events WHERE kind='message' AND json_extract(data,'$.role')='assistant'"
            ).fetchone()[0]
        finally:
            connection.close()
        return {"model_steps": count}

    def checkpoint(self, files):
        atomic(
            self.checkpoint_path,
            {
                "files": {p: base64.b64encode(v).decode() for p, v in files.items()},
                "sha256": {p: hashlib.sha256(v).hexdigest() for p, v in files.items()},
            },
        )

    def restore_files(self):
        if not self.checkpoint_path.exists():
            return {p: self.task.files[p].encode() for p in self.task.editable}
        row = read(self.checkpoint_path)
        files = {p: base64.b64decode(v, validate=True) for p, v in row["files"].items()}
        if set(files) != set(self.task.editable) or any(
            hashlib.sha256(v).hexdigest() != row["sha256"][p] for p, v in files.items()
        ):
            raise ValueError("Invalid workspace checkpoint")
        return files

    async def setup(self):
        if self.box is not None:
            try:
                healthy = (
                    self.box.created
                    and (await awaited_thread(self.box.inspect))["State"]["Running"]
                )
            except Exception:
                healthy = False
            if not healthy:
                try:
                    await awaited_thread(self.box.close)
                except Exception:
                    pass
                self.box = None
        if self.box is None:
            # Record the name before creation so even a killed host can remove the orphan.
            old = self.folder / "container.json"
            if old.exists():
                orphan = read(old)["name"]
                if not orphan.startswith("weyra-polyglot-"):
                    raise ValueError("Invalid owned container name")
                await awaited_thread(command, ["docker", "rm", "-f", orphan])
            self.box = Sandbox(self.image, deadline=self.deadline)
            atomic(old, {"name": self.box.name})
            await awaited_thread(self.box.create, {**self.task.files, **self.restore_files()})
            atomic(
                self.folder / "isolation.json", isolation(await awaited_thread(self.box.inspect))
            )
        if self.agent is None:
            identity = (
                read(self.identity_path).get("session_id") if self.identity_path.exists() else None
            )
            self.agent = CodingAgent(
                self.folder / "agent-state",
                self.runtime,
                session_id=identity,
                budget=self.budget,
                system=SYSTEM,
                max_steps=40,
                enable_memory=False,
                enable_evolution=False,
                permission_mode="bypassPermissions",
                owns_runtime=False,
            )
            atomic(self.identity_path, {"session_id": self.agent.session.session_id})
            specs = {name: self.agent.tools.specs[name] for name in ALLOWED}
            for name, spec in specs.items():

                async def handler(args, ctx, operation=name):
                    async with self.lock:
                        result = await awaited_thread(self.box.worker, operation, args)
                        if operation in ("write_file", "edit_file", "run_shell"):
                            try:
                                files = await awaited_thread(
                                    self.box.export, list(self.task.editable)
                                )
                                self.checkpoint(files)
                            except Exception:
                                # Keep the last verified checkpoint if a shell damaged an artifact.
                                pass
                    return ToolResult(
                        "",
                        operation,
                        result.get("content", ""),
                        status=result.get("status", "success"),
                        effect="possible"
                        if operation in ("write_file", "edit_file", "run_shell")
                        else "none",
                        structured=result.get("structured"),
                    )

                from dataclasses import replace
                self.agent.tools.register(replace(spec, handler=handler))
            self.agent.tools.specs = {name: self.agent.tools.specs[name] for name in specs}
            self.agent.skills = SkillStore(self.agent.project, skill_dirs=[])
            atomic(self.folder / "tools.json", self.agent.tools.declarations())

    async def attempt(self, index, feedback, seconds, steps):
        began = time.monotonic()
        end, answer = "normal", ""
        before_steps = self.metrics()["model_steps"]
        before_calls = self.budget.task_calls(self.task.id)
        before_cost = self.budget.task_total(self.task.id)
        before_infra = self.budget.task_infrastructure_total(self.task.id)
        try:
            await self.setup()
            seconds -= time.monotonic() - began
            if seconds <= 0:
                raise TimeoutError("Setup consumed model execution window")
            self.agent.max_steps = steps
            self.box.versions = {}
            prompt = (
                f"Implement this official {self.task.language} exercise. Read the implementation files: "
                + ", ".join(self.task.editable)
                + "\n\n"
                + self.task.instruction
                + "\n\nSave the implementation. Run useful self-tests; official verification follows."
                if index == 0
                else "Continue improving the saved implementation. Read the current code before editing. "
                + "Official failure feedback:\n\n"
                + feedback
                if feedback
                else "Resume the saved implementation after an execution interruption. Read the current files, complete the task and save it."
            )
            answer = await asyncio.wait_for(self.agent.run_once(prompt), max(0.1, seconds))
            turn = self.agent.session.latest("turn_end") or {}
            if turn.get("state") not in ("completed", "success", "done"):
                end = turn.get("state", "normal")
        except asyncio.CancelledError:
            raise
        except BudgetExceeded:
            end = "budget_stopped"
        except Exception as exc:
            end = _end(exc) if self.agent else "container_error"
            answer = type(exc).__name__
        try:
            async with self.lock:
                files = await awaited_thread(self.box.export, list(self.task.editable))
            self.checkpoint(files)
            recovered = False
        except Exception:
            files = self.restore_files()
            recovered = True
            if self.box:
                try:
                    await awaited_thread(self.box.close)
                except Exception:
                    pass
            self.box = None
        if self.agent:
            await self.classify_connection_failures()
            trace = self.folder / "rounds" / f"{index:04d}" / "trajectory.jsonl"
            trace.parent.mkdir(parents=True, exist_ok=True)
            if not trace.exists():
                self.agent.session.export_jsonl(trace)
        return {
            "agent_end": end,
            "answer": answer,
            "model_steps": self.metrics()["model_steps"] - before_steps,
            "physical_requests": self.budget.task_calls(self.task.id) - before_calls,
            "cost_cny": str(self.budget.task_total(self.task.id) - before_cost),
            "seconds": time.monotonic() - began,
            "artifact_recovered": recovered,
            "infrastructure_reclassified": self.budget.task_infrastructure_total(self.task.id)
            > before_infra,
        }, files

    async def close(self):
        errors = []
        for name, action in (
            ("agent", self.agent.close(close_runtime=False) if self.agent else None),
            ("runtime", self.runtime.close()),
            ("container", awaited_thread(self.box.close) if self.box else None),
        ):
            if action:
                try:
                    await action
                except Exception as exc:
                    errors.append({"resource": name, "exception_type": type(exc).__name__})
        atomic(self.folder / "cleanup.json", {"removed": not errors, "errors": errors})


def configuration():
    config, _ = runtime_config()
    config.route_parameters.setdefault("main", {})["max_output_tokens"] = 4096
    return config


def check_task(task, metadata):
    actual = task.metadata()
    if any(actual[key] != metadata.get(key) for key in actual):
        raise ValueError("Frozen task input or verifier changed")


def reopen_task(folder, budget):
    """Resume a credit-unblocked case without resetting any consumed resource."""
    folder = Path(folder)
    result = folder / "result.json"
    if not result.exists():
        return False
    row = read(result)
    execution_cost = getattr(budget, "task_execution_total", budget.task_total)(row["id"])
    if (
        row["status"] != "resource_exhausted"
        or execution_cost >= Decimal(row["cost_cny"])
        or row["model_steps"] >= 80
        or budget.task_calls(row["id"]) >= 120
        or row["seconds"] >= 590
    ):
        return False
    previous = folder / "resumptions"
    index = len(list(previous.glob("*/result.json")))
    archive = previous / f"{index:04d}"
    archive.mkdir(parents=True, exist_ok=True)
    save_once(archive / "result.json", row)
    state = read(folder / "progress.json")
    save_once(archive / "progress.json", state)
    state.update(
        status="running",
        phase="idle",
        elapsed_before=row["seconds"],
        resume_remaining_seconds=720 - row["seconds"],
    )
    atomic(folder / "progress.json", state)
    atomic(result, {**row, "status": "retryable", "resume_reason": "cache_ceiling_reconciliation"})
    return True


def freeze(root, old, new, image):
    catalog = build_catalog(old, new)
    config = configuration()
    image_id = json.loads(command(["docker", "image", "inspect", image])["stdout"])[0]["Id"]
    protocol = {
        "version": "weyra-polyglot-64-v3",
        "catalog": catalog,
        "config": config.safe_summary(),
        "system": SYSTEM,
        "limits": LIMITS,
        "source_hash": source_hash(),
        "image": image,
        "image_id": image_id,
        "order": sorted(
            catalog["pending_ids"],
            key=lambda s: hashlib.sha256(("weyra-polyglot-64-v3:" + s).encode()).hexdigest(),
        ),
    }
    protocol["fingerprint"] = digest(protocol)
    target = root / "protocol.json"
    if target.exists():
        if read(target) != protocol:
            raise ValueError("Frozen protocol changed")
    else:
        save_once(target, protocol)
    write_report(root, catalog, [], 0)
    return protocol


async def run(root):
    protocol = read(root / "protocol.json")
    copy = dict(protocol)
    fingerprint = copy.pop("fingerprint")
    config = configuration()
    current_source = source_hash()
    source_ok = current_source == protocol["source_hash"]
    amendment_path = root / "cache-ceiling-amendment.json"
    if not source_ok and amendment_path.exists():
        amendment = read(amendment_path)
        source_ok = (
            amendment.get("original_protocol") == fingerprint
            and amendment.get("original_source_hash") == protocol["source_hash"]
            and amendment.get("source_hash") == current_source
        )
    if digest(copy) != fingerprint or not source_ok or config.safe_summary() != protocol["config"]:
        raise ValueError("Frozen source or configuration changed")
    image = protocol["image"]
    if (
        json.loads(command(["docker", "image", "inspect", image])["stdout"])[0]["Id"]
        != protocol["image_id"]
    ):
        raise ValueError("Frozen image changed")
    catalog = protocol["catalog"]
    current = {t.id: t for t in discover(catalog["upstream_root"])}
    for metadata in catalog["tasks"]:
        check_task(current[metadata["id"]], metadata)
    if len(protocol["order"]) != 35 or set(protocol["order"]) != set(catalog["pending_ids"]):
        raise ValueError("Rerun order differs from the pending task set")
    ledger = SessionStore(project=root / "ledger", session_id="polyglot-v3-budget")
    budget = IterativeBudget(catalog["previous_v2_cost_cny"])
    budget.attach(ledger)

    async def worker(task):
        folder = root / "tasks" / task.id
        folder.mkdir(parents=True, exist_ok=True)
        host = TaskHost(task, folder, image, config, budget)

        async def grade(files, destination, seconds):
            verification_deadline = min(
                time.time() + seconds, read(folder / "progress.json")["deadline"]
            )
            return await awaited_thread(
                lambda: verify(
                    task,
                    image,
                    files,
                    destination,
                    catalog["reference_cases"][task.id],
                    deadline=verification_deadline,
                )
            )

        async def attempt(index, feedback, seconds, steps):
            host.deadline = read(folder / "progress.json")["deadline"]
            return await host.attempt(index, feedback, seconds, steps)

        try:
            row = await solve(task.id, folder, fingerprint, budget, attempt, grade, host.metrics)
            passed = sum(r["verification"]["verdict"] == "pass" for r in row["rounds"])
            print(
                task.id,
                row["status"],
                "rounds",
                len(row["rounds"]),
                "cost",
                row["cost_cny"],
                "verified",
                passed,
                flush=True,
            )
            return row
        finally:
            await host.close()

    def report(rows):
        try:
            effective = [{**row, "cost_cny": str(budget.task_total(row["id"]))} for row in rows]
            return write_report(root, catalog, effective, str(budget.spend_total()))
        except Exception as exc:
            atomic(
                root / "report-error.json",
                {"exception_type": type(exc).__name__, "at": time.time()},
            )
            raise

    try:
        rows = await run_independent([current[i] for i in protocol["order"]], worker, report)
        report(rows)
    finally:
        ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "run", "report"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--old", type=Path, default=Path(".benchmarks/results/polyglot-40-20261023")
    )
    parser.add_argument(
        "--previous", type=Path, default=Path(".benchmarks/results/polyglot-v2-20261023")
    )
    parser.add_argument("--image", default="weyra-polyglot:20261023")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.mode == "freeze":
            freeze(root, args.old, args.previous, args.image)
        elif args.mode == "run":
            asyncio.run(run(root))
        else:
            protocol = read(root / "protocol.json")
            ledger = SessionStore(project=root / "ledger", session_id="polyglot-v3-budget")
            try:
                budget = IterativeBudget(protocol["catalog"]["previous_v2_cost_cny"])
                budget.attach(ledger)
                rows = [read(p) for p in (root / "tasks").glob("*/*/result.json")]
                write_report(root, protocol["catalog"], rows, str(budget.spend_total()))
            finally:
                ledger.close()


if __name__ == "__main__":
    main()

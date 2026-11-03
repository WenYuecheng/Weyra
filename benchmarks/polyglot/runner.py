"""Official Polyglot single-attempt runner. No paid work on import or preflight."""

import argparse
import asyncio
from collections import Counter
import fcntl
import hashlib
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

from weyra.coding.agent import CodingAgent, DEFAULT_SYSTEM
from weyra.config import load_config
from weyra.contracts import Message, PreparedRequest, ToolResult, digest
from weyra.core.state import BudgetExceeded, RunContext
from weyra.session import SessionStore
from weyra.skills import SkillStore
from .accounting import CampaignBudget, summarize
from .dataset import discover
from .sandbox import Sandbox, command

BASE = Path(__file__).resolve().parents[2]
SYSTEM = (
    DEFAULT_SYSTEM
    + """\nBenchmark workspace tools operate inside /work in an isolated container. Use project-relative paths.
Implement the task in INSTRUCTIONS.md by editing the implementation files. Read files before editing.
Official verification tests are unavailable. You may create and execute your own tests. Do not install dependencies or access the network.
Finish only after saving your implementation. No official test feedback or second attempt is available."""
)
ALLOWED = ("read_file", "write_file", "edit_file", "list_files", "grep_search", "run_shell")


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        import os

        os.fsync(f.fileno())
    tmp.replace(path)


def save_once(path, data):
    with Path(path).open("x") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        import os

        os.fsync(f.fileno())


def load_once(path, protocol):
    data = json.loads(Path(path).read_text())
    if data["protocol"] != protocol:
        raise ValueError("Protocol fingerprint mismatch")
    return data


def validate_verdict(language, code, stats):
    if not stats:
        return "environment_error"
    if language == "javascript":
        total = stats.get("numTotalTests", 0)
        if not total or stats.get("numPendingTests", 0) or stats.get("numTodoTests", 0):
            return "environment_error"
        if (
            code == 0
            and stats.get("numPassedTests") == total
            and not stats.get("numFailedTests", 0)
        ):
            return "pass"
        return "fail"
    if not stats.get("tests") or stats.get("skipped", 0):
        return "environment_error"
    return (
        "pass"
        if code == 0 and not stats.get("failures", 0) and not stats.get("errors", 0)
        else "fail"
    )


def verify(task, image, files, folder, expected_cases=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    box = Sandbox(image)
    try:
        box.create({**task.verifier_files, **files})
        inspection = box.inspect()
        atomic(folder / "isolation.json", isolation(inspection))
        result = box.execute(list(task.command), timeout=120)
        (folder / "stdout.txt").write_text(result["stdout"])
        (folder / "stderr.txt").write_text(result["stderr"])
        stats = {}
        if task.language == "javascript":
            r = box.execute(["cat", "/tmp/jest-result.json"])
            if r["returncode"] == 0:
                try:
                    stats = json.loads(r["stdout"])
                    stats["case_ids"] = sorted(
                        a["fullName"]
                        for suite in stats.get("testResults", [])
                        for a in suite.get("assertionResults", [])
                    )
                except ValueError:
                    pass
        else:
            r = box.execute(["cat", "/tmp/junit.xml"])
            if r["returncode"] == 0:
                try:
                    tree = ET.fromstring(r["stdout"])
                    suites = [tree] if tree.tag == "testsuite" else list(tree.iter("testsuite"))
                    stats = {
                        k: sum(int(s.attrib.get(k, 0)) for s in suites)
                        for k in ("tests", "failures", "errors", "skipped")
                    }
                except (ET.ParseError, ValueError):
                    pass
        if task.language == "python" and stats:
            stats["case_ids"] = sorted(
                c.attrib.get("classname", "") + "::" + c.attrib.get("name", "")
                for c in tree.iter("testcase")
            )
        verdict = validate_verdict(task.language, result["returncode"], stats)
        immutable = {
            p: text.encode() for p, text in task.verifier_files.items() if p not in task.editable
        }
        try:
            if box.export(list(immutable)) != immutable:
                verdict = "environment_error"
                stats["test_files_modified"] = True
        except Exception:
            verdict = "environment_error"
            stats["test_files_unreadable"] = True
        if expected_cases is not None and stats.get("case_ids") != expected_cases:
            verdict = "environment_error"
            stats["case_identity_mismatch"] = True
        atomic(
            folder / "verification.json",
            {"returncode": result["returncode"], "stats": stats, "verdict": verdict},
        )
        return verdict, stats
    finally:
        box.close()
        atomic(folder / "cleanup.json", {"removed": True})


def isolation(i):
    h = i["HostConfig"]
    cfg = i["Config"]
    data = {
        "name": i["Name"],
        "image": i["Image"],
        "network": h["NetworkMode"],
        "binds": h.get("Binds"),
        "mounts": i.get("Mounts", []),
        "user": cfg["User"],
        "privileged": h["Privileged"],
        "cap_drop": h.get("CapDrop"),
        "security_opt": h.get("SecurityOpt"),
        "readonly_root": h["ReadonlyRootfs"],
        "memory": h["Memory"],
        "pids": h["PidsLimit"],
        "env_names": [x.split("=", 1)[0] for x in cfg["Env"]],
    }
    assert (
        data["network"] == "none"
        and not data["binds"]
        and not data["privileged"]
        and data["user"] == "1000:1000"
    )
    assert data["readonly_root"] and h["Memory"] == 2 * 1024**3 and h["PidsLimit"] == 256
    assert not any("KEY" in x or "TOKEN" in x or "SECRET" in x for x in data["env_names"])
    return data


def runtime_config():
    config = load_config(BASE)
    main = config.routes["main"]
    if (main.provider_id, main.model_id) != ("cun", "DeepSeek-V4.1-Flash"):
        raise ValueError("Configured model differs from approved model")
    safe = config.safe_summary()
    # Canonical provider id remains exactly as configured; no aliases or fallback.
    return config, safe


def source_hash():
    return digest(
        {
            str(p.relative_to(BASE)): hashlib.sha256(p.read_bytes()).hexdigest()
            for top in ("weyra", "benchmarks/polyglot")
            for p in sorted((BASE / top).rglob("*.py"))
        }
    )


def preflight(root, image, upstream):
    root.mkdir(parents=True, exist_ok=True)
    selected = []
    excluded = []
    rows = []
    counts = Counter()
    all_tasks = discover(upstream)
    for task in all_tasks:
        if counts[task.language] >= 20:
            continue
        folder = root / "preflight" / task.id
        try:
            verdict, stats = verify(task, image, task.solutions, folder)
        except Exception as exc:
            verdict = "environment_error"
            stats = {"exception": type(exc).__name__, "detail": str(exc)[:1000]}
        row = {"id": task.id, "verdict": verdict, "stats": stats}
        rows.append(row)
        atomic(root / "preflight.json", {"tasks": rows, "excluded": excluded})
        if verdict == "pass":
            selected.append(task)
            counts[task.language] += 1
        else:
            excluded.append(row)
        print("preflight", task.id, verdict, dict(counts), flush=True)
    if dict(counts) != {"python": 20, "javascript": 20}:
        raise RuntimeError("Fewer than20 runnable official tasks per language")
    config, safe = runtime_config()
    img = json.loads(command(["docker", "image", "inspect", image])["stdout"])[0]
    upstream_sha = command(["git", "-C", str(upstream), "rev-parse", "HEAD"])["stdout"].strip()
    protocol = {
        "version": "weyra-polyglot-40-v1",
        "dataset_url": "https://github.com/Aider-AI/polyglot-benchmark",
        "dataset_commit": upstream_sha,
        "image_id": img["Id"],
        "image": image,
        "config": safe,
        "system": SYSTEM,
        "tasks": [t.metadata() for t in selected],
        "excluded": excluded,
        "source_hash": source_hash(),
        "reference_cases": {
            t.id: json.loads((root / "preflight" / t.id / "verification.json").read_text())[
                "stats"
            ]["case_ids"]
            for t in selected
        },
        "limits": {
            "total_cny": "100",
            "check_cny": "4",
            "formal_cny": "96",
            "task_cny": "2.4",
            "steps": 40,
            "physical_calls": 80,
            "seconds": 900,
            "verifier_seconds": 120,
        },
        "test_adaptation": "upstream npm-test.sh replaces word-boundary xtest( with test(",
        "scoring": "all original enabled tests pass; no verifier feedback; fixed denominator40",
        "source_license": "upstream Exercism attribution and per-task LICENSE retained in upstream clone",
        "resources": "fresh empty skills/memory per task; online evolution disabled; six container tools only",
    }
    protocol["fingerprint"] = digest(protocol)
    if (root / "protocol.json").exists():
        if json.loads((root / "protocol.json").read_text()) != protocol:
            raise ValueError("Refusing protocol overwrite")
    else:
        save_once(root / "protocol.json", protocol)
    return protocol


def bind_tools(agent, box):
    specs = {n: agent.tools.specs[n] for n in ALLOWED}
    lock = asyncio.Lock()
    for name, spec in specs.items():

        async def handler(args, ctx, operation=name):
            async with lock:
                result = await asyncio.to_thread(box.worker, operation, args)
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
        agent.tools.register(replace(spec, handler=handler))
    agent.tools.specs = {name: agent.tools.specs[name] for name in specs}
    agent.skills = SkillStore(agent.project, skill_dirs=[])
    return agent.tools.declarations()


async def one_task(
    task, root, image, config, budget, protocol, expected_cases=None, phase="formal"
):
    folder = root / "tasks" / task.id
    folder.mkdir(parents=True, exist_ok=True)
    state = folder / "started.json"
    final = folder / "result.json"
    if final.exists():
        return load_once(final, protocol)
    if state.exists():
        load_once(state, protocol)
        return {
            "id": task.id,
            "protocol": protocol,
            "started": True,
            "agent_end": "interrupted_unknown",
            "verifier": "not_run",
            "model_steps": 0,
        }
    budget.begin(task.id, phase)
    save_once(state, {"id": task.id, "protocol": protocol, "started": True, "at": time.time()})
    runtime = config.build_runtime()
    runtime.fallbacks = {}
    runtime.skill_evolution = {"enabled": False}
    runtime.mcp_servers = {}
    box = Sandbox(image)
    agent = None
    started = time.monotonic()
    end = "normal"
    answer = ""
    artifact = {}
    export_error = None
    steps = 0
    requests = []
    calls = 0
    cleanup_errors = []
    try:
        await asyncio.to_thread(box.create, task.files)
        atomic(folder / "agent-isolation.json", isolation(box.inspect()))
        project = folder / "agent-state"
        agent = CodingAgent(
            project,
            runtime,
            budget=budget,
            system=SYSTEM,
            max_steps=40,
            enable_memory=False,
            enable_evolution=False,
            permission_mode="bypassPermissions",
            owns_runtime=False,
        )
        declarations = bind_tools(agent, box)
        atomic(folder / "tools.json", declarations)
        prompt = (
            f"Complete the official {task.language} exercise. Read INSTRUCTIONS.md and the implementation files: "
            + ", ".join(task.editable)
            + ". Save the implementation; official tests are hidden."
        )
        try:
            answer = await asyncio.wait_for(agent.run_once(prompt), timeout=900)
            turn = agent.session.latest("turn_end") or {}
            if turn.get("state") not in ("completed", "success", "done"):
                end = turn.get("state", "normal")
        except BudgetExceeded:
            end = "budget_stopped"
        except TimeoutError:
            end = "timeout"
        except Exception as exc:
            end = "provider_error" if getattr(exc, "kind", None) else "tool_error"
            answer = type(exc).__name__ + ": " + str(exc)[:1000]
        try:
            artifact = await asyncio.to_thread(box.export, list(task.editable))
        except Exception as exc:
            export_error = type(exc).__name__ + ": " + str(exc)[:500]
        messages = agent.session.project()
        steps = sum(m.role == "assistant" for m in messages)
        requests = [r["data"] for r in agent.session.records if r["kind"] == "model_usage"]
        calls = sum(r["kind"] == "tool_intent" for r in agent.session.records)
        agent.session.export_jsonl(folder / "trajectory.jsonl")
    except Exception as exc:
        end = "tool_error"
        export_error = type(exc).__name__ + ": " + str(exc)[:500]
    finally:
        for name, action in (
            ("agent", agent.close(close_runtime=False) if agent is not None else None),
            ("runtime", runtime.close()),
            ("container", asyncio.to_thread(box.close)),
        ):
            if action is None:
                continue
            try:
                await action
            except Exception as exc:
                cleanup_errors.append({"resource": name, "error": type(exc).__name__})
        atomic(
            folder / "agent-cleanup.json", {"removed": not cleanup_errors, "errors": cleanup_errors}
        )
    artifacts = folder / "artifacts"
    artifacts.mkdir(exist_ok=True)
    for path, raw in artifact.items():
        target = artifacts / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    if export_error:
        verdict = "environment_error"
        stats = {"artifact_error": export_error}
    else:
        try:
            verdict, stats = await asyncio.to_thread(
                verify, task, image, artifact, folder / "verification", expected_cases
            )
        except Exception as exc:
            verdict, stats = (
                "environment_error",
                {"exception": type(exc).__name__, "detail": str(exc)[:500]},
            )
    row = {
        "id": task.id,
        "protocol": protocol,
        "started": True,
        "agent_end": end,
        "verifier": verdict,
        "stats": stats,
        "answer": answer,
        "model_steps": steps,
        "tool_calls": calls,
        "physical_requests": len(requests),
        "usage": requests,
        "cost_cny": str(budget.task_cost(phase + "/" + task.id)),
        "seconds": time.monotonic() - started,
        "artifact_sha256": {p: hashlib.sha256(v).hexdigest() for p, v in artifact.items()},
        "cleanup_errors": cleanup_errors,
        "unchanged": all(artifact.get(p) == task.files[p].encode() for p in task.editable),
    }
    save_once(final, row)
    return row


async def smoke(root, config, budget):
    final = root / "check-result.json"
    if final.exists():
        if not json.loads(final.read_text())["passed"]:
            raise RuntimeError("Previous check failed; not retrying")
        return
    state = root / "check-started.json"
    if state.exists():
        raise RuntimeError("Interrupted connection check cannot be automatically repeated")
    save_once(state, {"started": True})
    budget.begin("connection", "check")
    runtime = config.build_runtime()
    runtime.fallbacks = {}
    store = SessionStore(project=root / "connection")
    ctx = RunContext(root / "connection", store, budget)
    passed = False
    result = {}
    try:
        model = runtime.resolve("main")
        request = PreparedRequest(
            model,
            "Return exactly READY.",
            (Message("user", "Reply READY."),),
            parameters={"max_output_tokens": 1024},
        )
        response = await asyncio.wait_for(runtime.complete(request, ctx), timeout=120)
        passed = response.stop_reason == "stop" and "READY" in response.content
        result = {
            "passed": passed,
            "content": response.content,
            "stop_reason": response.stop_reason,
        }
    except Exception as e:
        result = {
            "passed": False,
            "error": type(e).__name__,
            "kind": getattr(e, "kind", None),
            "detail": str(e)[:500],
        }
    finally:
        store.export_jsonl(root / "check-trajectory.jsonl")
        await ctx.close()
        store.close()
        await runtime.close()
        result["cost_cny"] = str(budget.phase_cost("check"))
        save_once(final, result)
    if not passed:
        raise RuntimeError("Original configured model connection check failed")


async def run(root, image, upstream):
    p = json.loads((root / "protocol.json").read_text())
    fingerprint = p["fingerprint"]
    copy = dict(p)
    copy.pop("fingerprint")
    if digest(copy) != fingerprint:
        raise ValueError("Protocol corruption")
    config, safe = runtime_config()
    if safe != p["config"] or source_hash() != p["source_hash"]:
        raise ValueError("Config/source changed after freeze")
    if (
        json.loads(command(["docker", "image", "inspect", image])["stdout"])[0]["Id"]
        != p["image_id"]
    ):
        raise ValueError("Image changed")
    all_tasks = {t.id: t for t in discover(upstream)}
    tasks = [all_tasks[r["id"]] for r in p["tasks"]]
    if [t.metadata() for t in tasks] != p["tasks"]:
        raise ValueError("Dataset changed")
    ledger = SessionStore(project=root / "ledger", session_id="polyglot40-budget")
    budget = CampaignBudget()
    budget.attach(ledger)
    rows = []
    try:
        await smoke(root, config, budget)
        for task in tasks:
            row = await one_task(
                task, root, image, config, budget, fingerprint, p["reference_cases"][task.id]
            )
            rows.append(row)
            costs = {phase: str(budget.phase_cost(phase)) for phase in ("formal", "check")}
            atomic(root / "summary.json", summarize(rows, costs))
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
    finally:
        ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run", "summary"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--image", default="weyra-polyglot:20261023")
    a = parser.parse_args()
    a.output = a.output.resolve()
    a.output.mkdir(parents=True, exist_ok=True)
    with (a.output / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if a.mode == "preflight":
            preflight(a.output, a.image, a.upstream.resolve())
        elif a.mode == "run":
            asyncio.run(run(a.output, a.image, a.upstream.resolve()))
        else:
            print((a.output / "summary.json").read_text())


if __name__ == "__main__":
    main()

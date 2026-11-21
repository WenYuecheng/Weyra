"""Resumable, isolated Polyglot campaign for the official GLM Flash API."""

from __future__ import annotations


import asyncio


from contextlib import aclosing, contextmanager

from dataclasses import dataclass, replace

import fcntl

import hashlib

import json


import sqlite3

import time


from weyra.coding.agent import CodingAgent

from weyra.contracts import Message, ModelInfo, PreparedRequest, ToolResult, canonical

from weyra.core.model_runtime import ModelRuntime

from weyra.core.state import Budget, BudgetExceeded, RunContext

from weyra.llm.providers import ModelError, OpenAIProvider, ProviderRegistry


from .glm_verify import VERIFIER_VERSION, feedback, verify_task

from .runner import BASE, atomic, isolation, source_hash as source_hash

from .sandbox import Sandbox, command

MODEL = "glm-5.3-flash"

ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/"

TASK_TOKENS, TASK_CALLS, TASK_SECONDS = 48_000, 12, 480
OUTPUT, STEPS_PER_ROUND = 4_096, 6

KEY_FILE = BASE / ".weyra/experiments/glm-flash.key"

SYSTEM = (
    "Implement the requested behavior in the named implementation files. "
    "Use project-relative paths. The initial file contents are supplied with the task. "
    "Save the implementation with a tool before finishing. "
    "Use a complete write for a short file, or a unique edit to preserve unrelated code. "
    "Keep tool calls concise. External tests run after the turn."
)

BASE_SKILL = (
    "Read the requirements and starter interfaces, preserve the public API, "
    "and implement the simplest correct algorithm. When a verifier reports a failure, "
    "identify the underlying issue, fix it generally, and save the corrected files."
)

TOOL_NAMES = ("read_file", "write_file", "edit_file", "list_files")

OUTCOMES = {"pass", "fail", "resource_exhausted"}

INFRA_ERRORS = (OSError, TimeoutError, RuntimeError, ModelError)


@dataclass(frozen=True)
class InferenceSettings:
    """Model and execution limits shared by every arm of a comparison."""

    context_window: int = 32_768
    output_tokens: int = OUTPUT
    reasoning_effort: str = "low"
    temperature: float = 0.2
    top_p: float | None = None
    steps: int = STEPS_PER_ROUND
    seconds: int = TASK_SECONDS


DEFAULT_SETTINGS = InferenceSettings()


class ReservationDenied(BudgetExceeded):
    """A local evaluation limit refused a request before it reached the provider."""

    def __init__(self, reason: str, *, used: int, requested: int, limit: int):
        self.details = {"reason": reason, "used": used, "requested": requested, "limit": limit}
        super().__init__(reason)


class Ledger:
    """Committed reservations survive interruption and cap every physical request."""

    def __init__(self, path, limit, token_limit=TASK_TOKENS, call_limit=TASK_CALLS):
        self.path, self.limit, self.token_limit, self.call_limit = (
            path,
            limit,
            token_limit,
            call_limit,
        )

    def __enter__(self):
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY, unit TEXT NOT NULL, reserved INTEGER NOT NULL, actual INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, cache_tokens INTEGER
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS usage_unit ON usage(unit)")
        return self

    def __exit__(self, *exc):
        self.db.close()

    def totals(self, unit=None):
        query = "SELECT COALESCE(SUM(COALESCE(actual,reserved)),0),COUNT(*),COUNT(*)-COUNT(actual) FROM usage"
        return self.db.execute(
            query + (" WHERE unit=?" if unit is not None else ""), () if unit is None else (unit,)
        ).fetchone()

    def reserve(self, unit, amount):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            task_spent, task_requests, _ = self.totals(unit)
            if task_requests >= self.call_limit:
                raise ReservationDenied(
                    "task_requests", used=task_requests, requested=1, limit=self.call_limit
                )
            if task_spent + amount > self.token_limit:
                raise ReservationDenied(
                    "task_tokens", used=task_spent, requested=amount, limit=self.token_limit
                )
            phase_spent = self.totals()[0]
            if phase_spent + amount > self.limit:
                raise ReservationDenied(
                    "campaign_tokens", used=phase_spent, requested=amount, limit=self.limit
                )
            row = self.db.execute("INSERT INTO usage(unit,reserved) VALUES (?,?)", (unit, amount))
            self.db.execute("COMMIT")
            return row.lastrowid
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def settle(self, request_id, actual, usage=None):
        if actual is not None:
            self.db.execute(
                "UPDATE usage SET actual=?,input_tokens=?,output_tokens=?,cache_tokens=? WHERE id=? AND actual IS NULL",
                (
                    actual,
                    getattr(usage, "input_tokens", None),
                    getattr(usage, "output_tokens", None),
                    getattr(usage, "cache_read_tokens", None),
                    request_id,
                ),
            )


class GuardedFlash(OpenAIProvider):
    def __init__(self, client, ledger, unit):
        super().__init__("zhipu", base_url=ENDPOINT, client=client)
        self.ledger, self.unit = ledger, unit
        self.budget_denial = None

    async def stream(self, request, context):
        # Use the full history and schema, including native thinking. Unknown usage
        # keeps the conservative reservation. This is a local limit, not a billing control.
        envelope = {
            "system": request.system,
            "messages": [m.to_dict() for m in request.messages],
            "tools": request.tools,
        }
        bound = (
            len(canonical(envelope).encode())
            + 256
            + 48 * (len(request.messages) + len(request.tools))
            + request.parameters.get("max_output_tokens", OUTPUT)
        )
        try:
            request_id = self.ledger.reserve(self.unit, bound)
        except ReservationDenied as error:
            self.budget_denial = error.details
            raise ModelError("evaluation_budget", str(error)) from error
        usage = None
        stream = super().stream(request, context)
        try:
            async with aclosing(stream):
                async for event in stream:
                    if event.kind == "response":
                        usage = event.data.usage
                    yield event
        finally:
            self.ledger.settle(request_id, usage.total_tokens if usage else None, usage)


def runtime_for(client, ledger, unit, settings=DEFAULT_SETTINGS):
    registry = ProviderRegistry()
    provider = GuardedFlash(client, ledger, unit)
    registry.register("zhipu", provider)
    model = ModelInfo(
        "zhipu",
        MODEL,
        settings.context_window,
        settings.output_tokens,
        True,
        True,
        True,
        reasoning_format="openai",
    )
    runtime = ModelRuntime(registry, {"main": model})
    parameters = {
        "max_output_tokens": settings.output_tokens,
        "reasoning_effort": settings.reasoning_effort,
        "temperature": settings.temperature,
    }
    if settings.top_p is not None:
        parameters["top_p"] = settings.top_p
    runtime.route_parameters["main"] = parameters
    return runtime, provider


def compare(rows, baseline, treatment):
    counts = {"paired": 0, "wins": 0, "losses": 0, "ties": 0, "net_passes": 0}
    for row in rows:
        before = row.get(baseline, {}).get("status")
        after = row.get(treatment, {}).get("status")
        if before not in OUTCOMES or after not in OUTCOMES:
            continue
        counts["paired"] += 1
        change = int(after == "pass") - int(before == "pass")
        counts[{1: "wins", -1: "losses", 0: "ties"}[change]] += 1
        counts["net_passes"] += change
    return counts


@contextmanager
def exclusive(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def read(path):
    return json.loads(path.read_text())


def freeze(path, value):
    if path.exists():
        if read(path) != value:
            raise ValueError(f"Frozen configuration changed: {path.name}")
    else:
        atomic(path, value)


def image_for(language):
    return (
        "weyra-polyglot:20261023"
        if language in ("python", "javascript")
        else f"weyra-polyglot-glm:{language}-v1"
    )


def image_ids(languages):
    result = {}
    for language in languages:
        image = image_for(language)
        inspect = command(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=20)
        if not inspect["returncode"]:
            result[language] = inspect["stdout"].strip()
    return result


def check_identity(task, image):
    identity = {"image": image, "task": task.metadata()}
    if task.language not in {"python", "javascript"}:
        identity["verifier_version"] = VERIFIER_VERSION
    return identity


def preflight_one(task, image, root):
    baseline = {path: task.files[path] for path in task.editable}
    if not task.solutions:
        return {"status": "reference_missing", "cases": []}
    reference, stats = verify_task(task, image, task.solutions, root / "reference")
    if reference != "pass":
        return {"status": "reference_" + reference, "cases": stats.get("case_ids", [])}
    cases = stats["case_ids"]
    empty, _ = verify_task(task, image, baseline, root / "stub", cases)
    if empty == "pass":
        return {"status": "stub_passes", "cases": cases}
    if empty != "fail":
        return {"status": "stub_" + empty, "cases": cases}
    return {"status": "eligible", "cases": cases}


def register_tools(agent, box, task):
    specs = {name: agent.tools.specs[name] for name in TOOL_NAMES}
    lock = asyncio.Lock()
    allowed = set(task.files)
    editable = set(task.editable)
    for name, spec in specs.items():

        async def handler(args, context, operation=name):
            path = args.get("path", ".")
            visible = editable if operation in {"edit_file", "write_file"} else allowed
            if operation == "list_files":
                if path != ".":
                    return ToolResult("", operation, "Use the listed files.", status="error")
                return ToolResult("", operation, "\n".join(sorted(allowed)))
            if path not in visible:
                return ToolResult(
                    "", operation, "Path is outside the task file set.", status="error"
                )
            async with lock:
                result = await asyncio.to_thread(box.worker, operation, args)
            return ToolResult(
                "",
                operation,
                result.get("content", ""),
                status=result.get("status", "success"),
                structured=result.get("structured"),
            )

        agent.tools.register(replace(spec, handler=handler))
    agent.tools.specs = {name: agent.tools.specs[name] for name in specs}


async def minimal_turn(agent, runtime, text, history, steps=STEPS_PER_ROUND):
    context = RunContext(agent.project, agent.session, agent.budget, agent.permission_mode)
    history.append(Message("user", text))
    try:
        for _ in range(steps):
            response = await runtime.complete(
                PreparedRequest(
                    runtime.resolve(),
                    agent.context_manager.system,
                    tuple(history),
                    tuple(agent.tools.declarations()),
                ),
                context,
                attempt_budget={"limit": 1, "attempts": 0},
            )
            history.append(
                Message(
                    "assistant",
                    response.content,
                    tool_calls=response.tool_calls,
                    provider_data=response.provider_data,
                )
            )
            if not response.tool_calls or response.stop_reason in {"length", "max_tokens"}:
                return
            results = await agent.tools.execute_batch(response.tool_calls, context)
            history.extend(
                Message("tool", result.content, tool_call_id=result.call_id, name=result.name)
                for result in results
            )
    finally:
        await context.close()


def fresh_repair_prompt(task, files: dict[str, bytes], diagnostic: str) -> str:
    """Rebuild the repair context from the task contract and the verified artifact."""
    implementation = {name: raw.decode("utf-8", errors="replace") for name, raw in files.items()}
    return (
        f"{task.instruction}\n\n"
        f"Current saved implementation (JSON by path):\n"
        f"{json.dumps(implementation, ensure_ascii=False)}\n\n"
        f"Verifier diagnostic:\n{diagnostic}\n\n"
        "Review the contract and implementation together. Fix the general cause while "
        f"preserving the public interface. Save work only in: {', '.join(task.editable)}."
    )


async def solve(
    task,
    arm,
    skill,
    expected,
    image,
    directory,
    ledger,
    client,
    unit,
    *,
    round_limit=2,
    settings=DEFAULT_SETTINGS,
):
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    box, agent = Sandbox(image), None
    runtime, provider = runtime_for(client, ledger, unit, settings)
    rounds, verdict = [], "infrastructure_error"
    try:
        async with asyncio.timeout(settings.seconds):
            await asyncio.to_thread(box.create, task.files)
            isolation(await asyncio.to_thread(box.inspect))
            agent = CodingAgent(
                directory / "agent",
                runtime,
                system=f"{SYSTEM}\nSkill: {skill}",
                permission_mode="acceptEdits",
                budget=Budget(),
                context_policy={"headroom_tokens": 4096, "summary_max_tokens": 2048},
                max_steps=settings.steps,
                enable_memory=False,
                enable_evolution=False,
                owns_runtime=False,
            )
            register_tools(agent, box, task)
            for path in task.editable:
                await asyncio.to_thread(box.worker, "read_file", {"path": path, "limit": 1})
            initial = "\n\n".join(f"File {p}:\n{task.files[p]}" for p in task.editable)
            text = (
                f"{task.instruction}\n\n{initial}\n\nSave work only in: {', '.join(task.editable)}."
            )
            minimal_history = []
            for attempt in range(1, round_limit + 1):
                model_error = None
                try:
                    if arm == "A":
                        await minimal_turn(agent, runtime, text, minimal_history, settings.steps)
                    else:
                        await agent.run_once(text)
                except (ModelError, BudgetExceeded, RuntimeError) as exc:
                    model_error = exc
                files = await asyncio.to_thread(box.export, task.editable)
                location = directory / str(attempt)
                score, stats = await asyncio.to_thread(
                    verify_task, task, image, files, location, expected
                )
                fprints = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
                row = {
                    "round": attempt,
                    "verdict": score,
                    "case_count": len(stats.get("case_ids", [])),
                    "file_hashes": fprints,
                }
                if model_error is not None:
                    row["model_error"] = getattr(model_error, "kind", type(model_error).__name__)
                    row["http_status"] = getattr(model_error, "status_code", None)
                rounds.append(row)
                for name, raw in files.items():
                    target = location / "implementation" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                if score == "pass":
                    verdict = "pass"
                    break
                if provider.budget_denial:
                    verdict = (
                        "infrastructure_error"
                        if provider.budget_denial["reason"] == "campaign_tokens"
                        else "resource_exhausted"
                    )
                    break
                if score == "environment_error" or (
                    isinstance(model_error, ModelError)
                    and (
                        model_error.kind in {"transient", "auth", "budget"}
                        or model_error.status_code is not None
                    )
                ):
                    verdict = "infrastructure_error"
                    break
                verdict = "fail"
                if attempt < round_limit:
                    diagnostic = feedback(location, expected)
                    if arm == "R":
                        agent.clear_session()
                        text = fresh_repair_prompt(task, files, diagnostic)
                    else:
                        text = (
                            "The verifier found failures. Repair the existing implementation and save files. "
                            "Preserve the public interface and handle the general case.\n"
                            + diagnostic
                        )
    except TimeoutError:
        verdict = "resource_exhausted" if ledger.totals(unit)[1] else "infrastructure_error"
    except (ValueError, *INFRA_ERRORS) as exc:
        atomic(
            directory / "error.json",
            {"type": type(exc).__name__, "kind": getattr(exc, "kind", None)},
        )
    finally:
        if agent is not None:
            if arm == "A" and "minimal_history" in locals():
                atomic(
                    directory / "history.json", [message.to_dict() for message in minimal_history]
                )
            agent.session.export_jsonl(directory / "trajectory.jsonl")
            await agent.close(close_runtime=False)
        await asyncio.to_thread(box.close)
    tokens, requests, unknown = ledger.totals(unit)
    result = {
        "status": verdict,
        "rounds": rounds,
        "accounted_tokens": tokens,
        "requests": requests,
        "unknown_usage": unknown,
        "seconds": round(time.monotonic() - started, 3),
    }
    if provider.budget_denial:
        result["budget_denial"] = provider.budget_denial
    atomic(directory / "result.json", result)
    return result


async def run_arm(
    root,
    task,
    arm,
    skill,
    check,
    ledger,
    client,
    *,
    round_limit=2,
    settings=DEFAULT_SETTINGS,
    resume_infrastructure=False,
):
    unit = f"{task.id}/{arm}"
    for index in (1, 2):
        attempt = root / task.id / arm / f"attempt-{index}"
        path = attempt / "result.json"
        if path.exists():
            result = read(path)
            if resume_infrastructure and result["status"] == "infrastructure_error" and index == 1:
                continue
        elif attempt.exists():
            result = {"status": "infrastructure_error", "rounds": [], "interrupted": True}
            atomic(path, result)
        else:
            result = await solve(
                task,
                arm,
                skill,
                check["cases"],
                check["image"],
                attempt,
                ledger,
                client,
                unit,
                round_limit=round_limit,
                settings=settings,
            )
        if result["status"] != "infrastructure_error" or campaign_stop_reason(result):
            return result
        spent, calls, _ = ledger.totals(unit)
        if spent >= ledger.token_limit or calls >= ledger.call_limit:
            return result
        if index == 1 and rate_limited(result):
            await asyncio.sleep(5)
    return result


def provider_blocked(result):
    return any(
        row.get("http_status") in {400, 401, 402, 403, 404}
        or row.get("model_error") in {"auth", "budget"}
        for row in result["rounds"]
    )


def campaign_stop_reason(result: dict) -> str | None:
    """Return the cause of a campaign-wide stop, if any."""
    if result.get("budget_denial", {}).get("reason") == "campaign_tokens":
        return "campaign_budget"
    return "provider_status" if provider_blocked(result) else None


def rate_limited(result):
    return any(row.get("http_status") == 429 for row in result["rounds"])


def result_for(root, task_id, arm):
    locations = sorted((root / task_id / arm).glob("attempt-*/result.json"), reverse=True)
    return read(locations[0]) if locations else None

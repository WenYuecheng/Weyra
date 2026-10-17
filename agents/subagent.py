"""Session-owned child turns with bounded concurrency and durable results."""

from __future__ import annotations

import asyncio
import copy
import inspect
from dataclasses import asdict
from pathlib import Path

from .contracts import ChildResult, new_id


class ChildManager:
    def __init__(self, parent_agent, max_concurrency=3, max_depth=1):
        if max_concurrency < 1 or max_depth < 0:
            raise ValueError("Invalid child execution limits")
        self.parent = parent_agent
        self.max_depth = max_depth
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._tasks = {}
        self._results = []
        self._closed = False
        archived = {}
        for entry in getattr(parent_agent.session, "records", []):
            data = entry["data"]
            if entry["kind"] == "child_task":
                archived[data["task_id"]] = data
            elif entry["kind"] == "child_result" and data["task_id"] in archived:
                previous = archived[data["task_id"]]
                self._tasks[data["task_id"]] = {
                    **previous,
                    "result": copy.deepcopy(data),
                    "state": data["state"],
                    "child": None,
                    "task": None,
                    "queued": [],
                }

    def _view(self, record):
        if record["result"] is not None:
            return copy.deepcopy(record["result"])
        return {key: record[key] for key in ("task_id", "state", "owner", "mode", "role")}

    def list(self):
        return [self._view(record) for record in self._tasks.values()]

    async def spawn(self, prompt, mode="spawn", background=False, role="", allowed_tools=None):
        if mode not in ("spawn", "fork"):
            raise ValueError("Child mode must be spawn or fork")
        if self.parent.depth >= self.max_depth:
            raise ValueError("Child depth limit reached")
        record = self._start(prompt, mode, role, allowed_tools)
        if background:
            return self._view(record)
        return await self.wait(record["task_id"], timeout=None)

    def _start(self, prompt, mode, role, allowed_tools, child=None):
        if self._closed:
            raise RuntimeError("Child manager is closed")
        context = self.parent.context
        record = {
            "task_id": new_id(),
            "state": "pending",
            "owner": context.id if context else None,
            "mode": mode,
            "role": role,
            "allowed_tools": copy.deepcopy(allowed_tools),
            "prompt": prompt,
            "child": child,
            "result": None,
            "task": None,
            "queued": [],
        }
        self.parent.session.append("child_task", {**self._view(record), "prompt": prompt}, True)
        self._tasks[record["task_id"]] = record
        record["task"] = asyncio.create_task(self._run(record))
        return record

    def _finish(self, record, state, summary="", usage=0, unresolved=None):
        if record["result"] is not None:
            return
        record["state"] = state
        evidence, artifacts = [], []
        child = record.get("child")
        if child is not None:
            session = child.session
            session_id = getattr(session, "session_id", "child")
            evidence.append("session:" + session_id)
            records = getattr(session, "records", [])[record.get("history_start", 0) :]
            intents = {
                event["data"].get("call_id"): event["data"]
                for event in records
                if event["kind"] == "tool_intent"
            }
            for event in records:
                data = event["data"]
                if event["kind"] != "tool_result" or not data.get("started"):
                    continue
                evidence.append(f"{session_id}#tool:{data['call_id']}")
                if data.get("output_ref"):
                    artifacts.append(data["output_ref"])
                intent = intents.get(data["call_id"], {})
                path = intent.get("arguments", {}).get("path")
                if (
                    path
                    and data.get("status") == "success"
                    and data.get("name") in {"write_file", "edit_file"}
                ):
                    artifacts.append(str((Path(child.project) / path).resolve()))
        result = asdict(
            ChildResult(
                record["task_id"],
                state,
                summary=summary,
                evidence=evidence,
                artifacts=list(dict.fromkeys(artifacts)),
                unresolved=unresolved or [],
                usage=usage,
            )
        )
        if hasattr(self.parent.session, "record_child_result"):
            self.parent.session.record_child_result(result)
        else:
            self.parent.session.append("child_result", result, True)
        record["result"] = result
        self._results.append(copy.deepcopy(result))

    async def _run(self, record):
        try:
            async with self._semaphore:
                if record["result"] is not None:
                    return
                record["state"] = "running"
                if record["child"] is None:
                    record["child"] = await self.parent.spawn_child(
                        record["mode"], record["role"], record["allowed_tools"]
                    )
                child = record["child"]
                child._delegation_task_id = record["task_id"]
                for kind, text in record["queued"]:
                    await self._deliver(child, text, kind)
                record["queued"].clear()
                before = len(getattr(child.session, "records", []))
                record["history_start"] = before
                summary = await child.run_once(record["prompt"])
                usage = self._usage(child, before)
                unfinished = [
                    entry["data"].get("state")
                    for entry in getattr(child.session, "records", [])[before:]
                    if entry["kind"] == "turn_end" and entry["data"].get("state") != "completed"
                ]
                self._finish(
                    record,
                    "failed" if unfinished else "completed",
                    str(summary),
                    usage,
                    unresolved=[f"Child turn ended {state}" for state in unfinished],
                )
        except asyncio.CancelledError:
            self._finish(record, "cancelled", unresolved=["Child turn was cancelled"])
        except Exception as exc:
            self._finish(record, "failed", unresolved=[type(exc).__name__])

    @staticmethod
    def _usage(child, start):
        records = getattr(child.session, "records", [])[start:]
        ledger = [record["data"] for record in records if record.get("kind") == "model_usage"]
        if ledger:
            return sum(
                data.get("accounted_tokens") or data.get("total_tokens") or 0 for data in ledger
            )
        total = 0
        for record in records:
            if record.get("kind") != "attempt_end":
                continue
            usage = record.get("data", {}).get("usage", 0)
            if isinstance(usage, int):
                total += usage
            elif isinstance(usage, dict):
                total += usage.get("total_tokens") or (
                    (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
                )
        return total

    async def wait(self, task_id, timeout=30):
        record = self._tasks[task_id]
        if record["result"] is not None:
            return self._view(record)
        try:
            await asyncio.wait_for(asyncio.shield(record["task"]), timeout)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            if record["result"] is None:
                raise
        return self._view(record)

    async def _deliver(self, child, text, kind):
        result = getattr(child, "steer" if kind == "steer" else "follow_up")(text)
        if inspect.isawaitable(result):
            await result

    async def send(self, task_id, text, kind="follow_up"):
        if kind == "steering":
            kind = "steer"
        if kind not in ("steer", "follow_up"):
            raise ValueError("Unknown child message kind")
        record = self._tasks[task_id]
        child = record["child"]
        if record["state"] in ("pending", "running"):
            if child is None:
                record["queued"].append((kind, text))
            else:
                await self._deliver(child, text, kind)
            return self._view(record)
        if child is None:
            raise ValueError("Child did not initialize")
        # Resolve historical task handles to the currently active turn of this child.
        active = next(
            (
                item
                for item in reversed(list(self._tasks.values()))
                if item["child"] is child and item["state"] in ("pending", "running")
            ),
            None,
        )
        if active is not None:
            await self._deliver(child, text, kind)
            return self._view(active)
        return self._view(
            self._start(text, record["mode"], record["role"], record["allowed_tools"], child)
        )

    async def interrupt(self, task_id):
        record = self._tasks[task_id]
        if record["result"] is not None:
            return self._view(record)
        # Mark durably before cancellation so a provider ignoring cancellation cannot publish a late answer.
        self._finish(record, "cancelled", unresolved=["Child turn was cancelled"])
        record["task"].cancel()
        child = record["child"]
        if child is not None:
            result = child.abort()
            if inspect.isawaitable(result):
                await result
        await asyncio.gather(record["task"], return_exceptions=True)
        return self._view(record)

    async def cancel_owner(self, parent_turn_id):
        await asyncio.gather(
            *(
                self.interrupt(key)
                for key, record in self._tasks.items()
                if record["owner"] == parent_turn_id and record["result"] is None
            )
        )

    def drain_results(self):
        # Production consumers acknowledge delivery only with the parent message commit.
        if hasattr(self.parent.session, "pending_child_results"):
            return self.parent.session.pending_child_results()
        results, self._results = self._results, []
        return results

    async def close(self):
        self._closed = True
        errors = await asyncio.gather(
            *(self.interrupt(key) for key in self._tasks), return_exceptions=True
        )
        children = {
            id(record["child"]): record["child"]
            for record in self._tasks.values()
            if record["child"] is not None
        }
        errors += await asyncio.gather(
            *(child.close(close_runtime=False) for child in children.values()),
            return_exceptions=True,
        )
        if any(isinstance(error, BaseException) for error in errors):
            raise RuntimeError("Child cleanup failed after draining all children")

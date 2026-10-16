"""Single-writer JSONL facts and branch-aware model projections."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path

from .contracts import Message, canonical, digest, new_id


class SessionError(RuntimeError):
    pass


class SessionStore:
    def __init__(self, path: str | Path, project: str | Path):
        self.path = Path(path).resolve()
        self.project_path = Path(project).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            raise SessionError("Session already has a writer") from None
        self._records: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self.leaf: str | None = None
        self.failed = False
        self.closed = False
        try:
            self._load()
            if not self.records:
                self.append(
                    "session", {"format": 1, "project": str(self.project_path)}, checkpoint=True
                )
            elif self.records[0]["data"].get("project") != str(self.project_path):
                raise SessionError("Session belongs to another project")
        except BaseException:
            self.close()
            raise

    def _load(self):
        self._file.seek(0)
        lines = self._file.readlines()
        offset = 0
        for index, line in enumerate(lines):
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if index == len(lines) - 1 and not line.endswith(b"\n"):
                    self._file.truncate(offset)
                    break
                raise SessionError(f"Corrupt session record {index + 1}") from None
            if not isinstance(entry, dict) or not {"id", "parent", "kind", "data"} <= entry.keys():
                raise SessionError(f"Invalid session record {index + 1}")
            if entry["id"] in self._by_id or (
                entry["parent"] and entry["parent"] not in self._by_id
            ):
                raise SessionError("Invalid session ancestry")
            self._records.append(entry)
            self._by_id[entry["id"]] = entry
            self.leaf = entry["id"]
            offset += len(line)
            if not line.endswith(b"\n"):
                self._file.seek(0, os.SEEK_END)
                self._file.write(b"\n")
        self._file.seek(0, os.SEEK_END)

    def append(self, kind: str, data: dict, checkpoint: bool = False) -> dict:
        if self.closed or self.failed:
            raise SessionError("Session is closed or its persistence has failed")
        if kind == "model_request":
            data = json.loads(canonical(data))
            for key in ("system", "tools"):
                if key in data:
                    data[key + "_ref"] = self.put_blob(data.pop(key))
        record = {"id": new_id(), "parent": self.leaf, "kind": kind, "data": data}
        encoded = (canonical(record) + "\n").encode("utf-8")
        try:
            self._file.write(encoded)
            self._file.flush()
            if checkpoint:
                os.fsync(self._file.fileno())
        except OSError:
            self.failed = True
            raise
        # Detach values from caller mutation, as disk is the fact source.
        record = json.loads(encoded)
        self._records.append(record)
        self._by_id[record["id"]] = record
        self.leaf = record["id"]
        return json.loads(canonical(record))

    def add_message(self, message: Message) -> dict:
        return self.append("message", message.to_dict(), checkpoint=True)

    @property
    def records(self):
        return json.loads(canonical(self._records))

    def put_blob(self, value):
        directory = self.path.parent / "blobs"
        if not directory.resolve().is_relative_to(self.path.parent):
            raise SessionError("Request blob directory escapes session storage")
        directory.mkdir(exist_ok=True)
        key = digest(value)
        target = directory / (key + ".json")
        if not target.exists():
            descriptor, temporary = tempfile.mkstemp(dir=directory)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(canonical(value))
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return key

    def expand_request(self, record):
        data = json.loads(canonical(record.get("data", record)))
        for key in ("system", "tools"):
            ref = data.pop(key + "_ref", None)
            if ref:
                if len(ref) != 64 or any(char not in "0123456789abcdef" for char in ref):
                    raise SessionError("Invalid request blob reference")
                value = json.loads((self.path.parent / "blobs" / (ref + ".json")).read_text())
                if digest(value) != ref:
                    raise SessionError("Request blob content mismatch")
                data[key] = value
        return data

    def chain(self, leaf: str | None = None) -> list[dict]:
        cursor = self.leaf if leaf is None else leaf
        records = []
        while cursor:
            record = self._by_id[cursor]
            records.append(record)
            cursor = record["parent"]
        return json.loads(canonical(list(reversed(records))))

    def project(self, leaf: str | None = None) -> list[Message]:
        messages = []
        for record in self.chain(leaf):
            if record["kind"] == "message":
                messages.append(Message.from_dict(record["data"]))
            elif record["kind"] in {"compaction", "seed"}:
                messages = [Message.from_dict(item) for item in record["data"]["messages"]]
        return messages

    def completed_view(self) -> list[Message]:
        for record in reversed(self.chain()):
            if record["kind"] == "turn_end":
                return self.project(record["id"])
        return []

    def latest(self, kind: str) -> dict | None:
        value = next((r["data"] for r in reversed(self.chain()) if r["kind"] == kind), None)
        return json.loads(canonical(value)) if value is not None else None

    def branch(self, record_id: str) -> str:
        if record_id not in self._by_id:
            raise SessionError("Unknown branch point")
        view = self.project(record_id)
        pending = set()
        for message in view:
            pending.update(call.id for call in message.tool_calls)
            if message.tool_call_id:
                pending.discard(message.tool_call_id)
        if pending:
            raise SessionError("Cannot branch inside an incomplete tool batch")
        previous = self.leaf
        self.leaf = record_id
        try:
            return self.append("branch", {"from": record_id}, checkpoint=True)["id"]
        except BaseException:
            self.leaf = previous
            raise

    def fork(self, path: str | Path) -> SessionStore:
        view = self.project()
        pending = {call.id for m in view for call in m.tool_calls}
        pending -= {m.tool_call_id for m in view if m.tool_call_id}
        if pending:
            raise SessionError("Cannot fork an incomplete tool batch")
        target = Path(path)
        if target.exists():
            raise SessionError("Fork destination already exists")
        child = SessionStore(target, self.project_path)
        child.append(
            "seed",
            {
                "messages": [m.to_dict() for m in view],
                "source_session": self.path.name,
                "source_leaf": self.leaf,
            },
            True,
        )
        for kind in ("baseline", "goal"):
            value = self.latest(kind)
            if value:
                child.append(kind, value, True)
        return child

    def recover_interrupted(self):
        active = {}
        intents = {}
        results = {}
        child_tasks = {}
        for record in self.chain():
            data = record["data"]
            if record["kind"] == "turn_start":
                active[data["id"]] = data
            elif record["kind"] == "turn_end":
                active.pop(data["id"], None)
            elif record["kind"] == "tool_intent":
                intents[data.get("call_id", data.get("id"))] = data
            elif record["kind"] == "tool_result":
                intents.pop(data.get("call_id"), None)
                results[data.get("call_id")] = data
            elif record["kind"] == "child_task":
                child_tasks[data["task_id"]] = data
            elif record["kind"] == "child_result":
                child_tasks.pop(data["task_id"], None)
        existing = {m.tool_call_id for m in self.project() if m.tool_call_id}
        for message in list(self.project()):
            for call in message.tool_calls:
                if call.id not in existing:
                    result = results.get(call.id)
                    self.add_message(
                        Message(
                            "tool",
                            result.get("content", "Outcome unknown")
                            if result
                            else "Outcome unknown after process interruption; do not automatically retry.",
                            source="recovery",
                            tool_call_id=call.id,
                            name=call.name,
                            provider_data={
                                "tool_status": result.get("status", "unknown")
                                if result
                                else "unknown"
                            },
                        )
                    )
                    if result is None:
                        self.append(
                            "tool_result",
                            {
                                "call_id": call.id,
                                "name": call.name,
                                "status": "unknown",
                                "started": call.id in intents,
                                "effect": "unknown",
                            },
                            True,
                        )
        for turn_id in active:
            self.append(
                "turn_end",
                {"id": turn_id, "state": "cancelled", "reason": "process_interrupted"},
                True,
            )
        for task_id in child_tasks:
            self.append(
                "child_result",
                {
                    "task_id": task_id,
                    "state": "cancelled",
                    "summary": "",
                    "evidence": [],
                    "artifacts": [],
                    "unresolved": ["process_interrupted"],
                    "usage": 0,
                },
                True,
            )

    def checkpoint(self):
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self):
        if not self.closed:
            try:
                if not self.failed:
                    self.checkpoint()
            finally:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                self._file.close()
                self.closed = True

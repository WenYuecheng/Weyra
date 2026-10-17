"""SQLite facts, durable inboxes, and branch-aware model projections."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .contracts import Message, canonical, digest, new_id


class SessionError(RuntimeError):
    pass


class SessionStore:
    """One writer per session; every committed mutation is durable in state.db.

    ``path`` accepts a session ID or a legacy JSONL filename. New callers should
    use ``session_id`` explicitly. JSONL is only ever read for migration.
    """

    def __init__(self, path=None, project=".", *, session_id=None, _seed_events=None):
        self.project_path = Path(project).resolve()
        state = self.project_path / ".weyra"
        if not state.resolve().is_relative_to(self.project_path):
            raise SessionError("State directory escapes project")
        state.mkdir(parents=True, exist_ok=True)
        self.path = state / "state.db"
        if self.path.is_symlink():
            raise SessionError("State database must not be a symlink")
        self.failed = False
        self.closed = False
        self._lock = threading.RLock()
        self._depth = 0
        self._records = []
        self._by_id = {}
        self.leaf = None
        self._writer = None
        self._db = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=30
        )
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=FULL")
            self._schema()
            source = (
                Path(path).resolve() if path is not None and Path(path).suffix == ".jsonl" else None
            )
            alias = str(source) if source else None
            previous = (
                self._db.execute("SELECT session_id FROM aliases WHERE path=?", (alias,)).fetchone()
                if alias
                else None
            )
            imported = None
            if previous:
                if session_id is not None and session_id != previous[0]:
                    raise SessionError("Legacy path already belongs to another session")
                session_id = previous[0]
            elif source and source.exists():
                imported = self._read_legacy(source)
                ledger = self._db.execute(
                    "SELECT session_id FROM imports WHERE source_hash=?", (imported[2],)
                ).fetchone()
                if ledger:
                    if session_id is not None and session_id != ledger[0]:
                        raise SessionError("Legacy content already belongs to another session")
                    session_id = ledger[0]
                    imported = None
            if session_id is None:
                if path is not None and source is None and Path(path).resolve() == self.path:
                    rows = self._db.execute(
                        "SELECT session_id FROM sessions ORDER BY updated DESC"
                    ).fetchall()
                    if len(rows) != 1:
                        raise SessionError(
                            "Choose an explicit session_id when reopening a database"
                        )
                    session_id = rows[0][0]
                else:
                    session_id = (
                        source.stem if source else str(path) if path is not None else new_id()
                    )
            if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
                raise SessionError("Invalid session ID")
            self.session_id = session_id
            locks = state / "locks"
            if locks.is_symlink():
                raise SessionError("Session lock directory must not be a symlink")
            locks.mkdir(exist_ok=True)
            lock_path = locks / (hashlib.sha256(session_id.encode()).hexdigest() + ".lock")
            if lock_path.is_symlink():
                raise SessionError("Session lock must not be a symlink")
            self._writer = lock_path.open("a+b")
            try:
                fcntl.flock(self._writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SessionError("Session already has a writer") from None
            with self.transaction():
                existing = self._db.execute(
                    "SELECT project,leaf FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if existing:
                    if _seed_events is not None:
                        raise SessionError("Fork destination already exists")
                    if existing[0] != str(self.project_path):
                        raise SessionError("Session belongs to another project")
                    if imported:
                        raise SessionError("Import destination already exists")
                    self._load()
                else:
                    self._db.execute(
                        "INSERT INTO sessions(session_id,project) VALUES(?,?)",
                        (session_id, str(self.project_path)),
                    )
                    if imported:
                        records, blobs, source_hash = imported
                        if records[0]["data"].get("project") != str(self.project_path):
                            raise SessionError("Session belongs to another project")
                        for key, value in blobs.items():
                            self._db.execute(
                                "INSERT OR IGNORE INTO blobs(hash,data) VALUES(?,?)",
                                (key, canonical(value)),
                            )
                        for record in records:
                            self._insert(record)
                        self._db.execute(
                            "INSERT INTO imports(source_hash,session_id,source_path) VALUES(?,?,?)",
                            (source_hash, session_id, alias),
                        )
                        self._restore_inbox()
                    else:
                        self.append(
                            "session",
                            {
                                "format": 2,
                                "project": str(self.project_path),
                                "session_id": session_id,
                            },
                        )
                if _seed_events is not None:
                    self.append_many(_seed_events)
                if alias:
                    self._db.execute(
                        "INSERT OR IGNORE INTO aliases(path,session_id) VALUES(?,?)",
                        (alias, session_id),
                    )
        except BaseException:
            self._db.close()
            if self._writer:
                self._writer.close()
            self.closed = True
            raise

    def _schema(self):
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, project TEXT NOT NULL, leaf TEXT,
                created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                id TEXT NOT NULL, parent TEXT, kind TEXT NOT NULL, data TEXT NOT NULL,
                request_id TEXT, task_id TEXT, turn_id TEXT,
                UNIQUE(session_id,id),
                FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                FOREIGN KEY(session_id,parent) REFERENCES events(session_id,id)
            );
            CREATE INDEX IF NOT EXISTS events_kind ON events(session_id,kind,seq);
            CREATE INDEX IF NOT EXISTS events_request ON events(session_id,request_id);
            CREATE INDEX IF NOT EXISTS events_task ON events(session_id,task_id);
            CREATE INDEX IF NOT EXISTS events_turn ON events(session_id,turn_id);
            CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aliases (path TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id));
            CREATE TABLE IF NOT EXISTS imports (source_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), source_path TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS child_inbox (
                session_id TEXT NOT NULL REFERENCES sessions(session_id), task_id TEXT NOT NULL,
                result TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id,task_id)
            );
        """
        )

    @classmethod
    def list_sessions(cls, project):
        path = Path(project).resolve() / ".weyra" / "state.db"
        if not path.exists():
            return []
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT session_id,project,leaf,created,updated FROM sessions ORDER BY updated DESC,session_id"
                )
            ]

    @staticmethod
    def _read_legacy(source):
        raw = source.read_bytes()
        records, seen, blobs = [], set(), {}
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if index == len(lines) - 1 and not line.endswith(b"\n"):
                    break
                raise SessionError(f"Corrupt session record {index + 1}") from None
            if (
                not isinstance(record, dict)
                or not {"id", "parent", "kind", "data"} <= record.keys()
            ):
                raise SessionError(f"Invalid session record {index + 1}")
            rid, parent = record["id"], record["parent"]
            if (
                not isinstance(rid, str)
                or not rid
                or not isinstance(record["kind"], str)
                or not isinstance(record["data"], dict)
            ):
                raise SessionError("Invalid session record")
            if (
                rid in seen
                or (not records and (parent is not None or record["kind"] != "session"))
                or (records and (not isinstance(parent, str) or parent not in seen))
            ):
                raise SessionError("Invalid session ancestry")
            seen.add(rid)
            records.append(record)
            if record["kind"] == "model_request":
                for key in ("system_ref", "tools_ref"):
                    ref = record["data"].get(key)
                    if ref is not None:
                        SessionStore._validate_ref(ref)
                        blob = source.parent / "blobs" / (ref + ".json")
                        if not blob.resolve().is_relative_to(source.parent):
                            raise SessionError("Request blob escapes session storage")
                        try:
                            value = json.loads(blob.read_text(encoding="utf-8"))
                        except (OSError, ValueError) as error:
                            raise SessionError("Missing or invalid request blob") from error
                        if digest(value) != ref:
                            raise SessionError("Request blob content mismatch")
                        blobs[ref] = value
        if not records:
            raise SessionError("Legacy session has no valid root")
        return records, blobs, hashlib.sha256(raw).hexdigest()

    def _load(self):
        self._records = [
            {"id": rid, "parent": parent, "kind": kind, "data": json.loads(data)}
            for rid, parent, kind, data in self._db.execute(
                "SELECT id,parent,kind,data FROM events WHERE session_id=? ORDER BY seq",
                (self.session_id,),
            )
        ]
        self._by_id = {r["id"]: r for r in self._records}
        self.leaf = self._db.execute(
            "SELECT leaf FROM sessions WHERE session_id=?", (self.session_id,)
        ).fetchone()[0]

    @contextmanager
    def transaction(self):
        """Group appends and inbox changes in one writer transaction.

        A nested failure marks the entire outer transaction for rollback, even
        if the caller catches the nested exception.
        """
        with self._lock:
            if self.closed or self.failed:
                raise SessionError("Session is closed or its persistence has failed")
            outer = self._depth == 0
            if outer:
                self._db.execute("BEGIN IMMEDIATE")
                snapshot = (list(self._records), dict(self._by_id), self.leaf)
                self._rollback_only = False
            self._depth += 1
            try:
                yield self
                if outer:
                    if self._rollback_only:
                        raise SessionError("Nested transaction failed")
                    self._db.execute("COMMIT")
            except BaseException as error:
                self._rollback_only = True
                code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                if code in {
                    sqlite3.SQLITE_FULL,
                    sqlite3.SQLITE_IOERR,
                    sqlite3.SQLITE_CORRUPT,
                    sqlite3.SQLITE_NOTADB,
                    sqlite3.SQLITE_CANTOPEN,
                    sqlite3.SQLITE_READONLY,
                }:
                    self.failed = True
                if outer:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                    self._records, self._by_id, self.leaf = snapshot
                raise
            finally:
                self._depth -= 1

    def _insert(self, record):
        data = record["data"]
        self._db.execute(
            "INSERT INTO events(session_id,id,parent,kind,data,request_id,task_id,turn_id) VALUES(?,?,?,?,?,?,?,?)",
            (
                self.session_id,
                record["id"],
                record["parent"],
                record["kind"],
                canonical(data),
                data.get(
                    "request_id",
                    data.get("id") if record["kind"] == "model_request" else None,
                ),
                data.get("task_id"),
                data.get(
                    "turn_id",
                    (data.get("id") if record["kind"] in {"turn_start", "turn_end"} else None),
                ),
            ),
        )
        self._db.execute(
            "UPDATE sessions SET leaf=?,updated=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE session_id=?",
            (record["id"], self.session_id),
        )
        self._records.append(record)
        self._by_id[record["id"]] = record
        self.leaf = record["id"]

    def append(self, kind: str, data: dict, checkpoint: bool = False) -> dict:
        with self.transaction():
            data = json.loads(canonical(data))
            if kind == "model_request":
                for key in ("system", "tools"):
                    if key in data:
                        data[key + "_ref"] = self.put_blob(data.pop(key))
            record = {"id": new_id(), "parent": self.leaf, "kind": kind, "data": data}
            self._insert(record)
            return json.loads(canonical(record))

    def append_many(self, entries):
        with self.transaction():
            return [
                (
                    self.append(item["kind"], item["data"])
                    if isinstance(item, dict)
                    else self.append(*item)
                )
                for item in entries
            ]

    def add_message(self, message: Message) -> dict:
        return self.append("message", message.to_dict(), checkpoint=True)

    @property
    def records(self):
        with self._lock:
            return json.loads(canonical(self._records))

    def export_jsonl(self, path=None):
        """Explicit diagnostic export; never used as the live persistence store."""
        text = "".join(canonical(record) + "\n" for record in self.records)
        if path is not None:
            target = Path(path).resolve()
            if target == self.path.resolve():
                raise SessionError("Cannot export over the live database")
            with target.open("x", encoding="utf-8") as output:
                output.write(text)
        return text

    def put_blob(self, value):
        with self.transaction():
            key = digest(value)
            self._db.execute(
                "INSERT OR IGNORE INTO blobs(hash,data) VALUES(?,?)",
                (key, canonical(value)),
            )
            return key

    @staticmethod
    def _validate_ref(ref):
        if (
            not isinstance(ref, str)
            or len(ref) != 64
            or any(c not in "0123456789abcdef" for c in ref)
        ):
            raise SessionError("Invalid request blob reference")

    def expand_request(self, record):
        with self._lock:
            data = json.loads(canonical(record.get("data", record)))
            for key in ("system", "tools"):
                ref = data.pop(key + "_ref", None)
                if ref is not None:
                    self._validate_ref(ref)
                    row = self._db.execute("SELECT data FROM blobs WHERE hash=?", (ref,)).fetchone()
                    if not row:
                        raise SessionError("Missing request blob")
                    value = json.loads(row[0])
                    if digest(value) != ref:
                        raise SessionError("Request blob content mismatch")
                    data[key] = value
            return data

    def consume_input(self, item):
        with self.transaction():
            if any(
                r["kind"] == "input_consumed" and r["data"]["id"] == item["id"]
                for r in self.chain()
            ):
                return None
            message = Message(
                "user",
                item.get("text", item.get("content", "")),
                id=item["id"],
                source=item.get("source", item.get("kind", "user")),
            )
            record = next(
                (
                    r
                    for r in self.chain()
                    if r["kind"] == "message" and r["data"].get("id") == item["id"]
                ),
                None,
            )
            if record is None:
                record = self.add_message(message)
            self.append("input_consumed", {"id": item["id"]})
            return record

    def record_child_result(self, result):
        data = asdict(result) if is_dataclass(result) else json.loads(canonical(result))
        with self.transaction():
            if self._db.execute(
                "SELECT 1 FROM child_inbox WHERE session_id=? AND task_id=?",
                (self.session_id, data["task_id"]),
            ).fetchone():
                return False
            self.append("child_result", data)
            self._db.execute(
                "INSERT INTO child_inbox(session_id,task_id,result) VALUES(?,?,?)",
                (self.session_id, data["task_id"], canonical(data)),
            )
            return True

    def pending_child_results(self):
        with self._lock:
            return [
                json.loads(row[0])
                for row in self._db.execute(
                    "SELECT result FROM child_inbox WHERE session_id=? AND acknowledged=0 ORDER BY rowid",
                    (self.session_id,),
                )
            ]

    def accept_child_result(self, task_id, message):
        with self.transaction():
            row = self._db.execute(
                "SELECT acknowledged FROM child_inbox WHERE session_id=? AND task_id=?",
                (self.session_id, task_id),
            ).fetchone()
            if not row or row[0]:
                return False
            self.add_message(
                message if isinstance(message, Message) else Message.from_dict(message)
            )
            self.acknowledge_child_result(task_id)
            return True

    def acknowledge_child_result(self, task_id):
        with self.transaction():
            changed = self._db.execute(
                "UPDATE child_inbox SET acknowledged=1 WHERE session_id=? AND task_id=? AND acknowledged=0",
                (self.session_id, task_id),
            ).rowcount
            if changed:
                self.append("child_result_ack", {"task_id": task_id})
            return bool(changed)

    def _restore_inbox(self):
        for record in self.chain():
            data = record["data"]
            if record["kind"] == "child_result":
                self._db.execute(
                    "INSERT OR IGNORE INTO child_inbox(session_id,task_id,result) VALUES(?,?,?)",
                    (self.session_id, data["task_id"], canonical(data)),
                )
            elif record["kind"] == "child_result_ack":
                self._db.execute(
                    "UPDATE child_inbox SET acknowledged=1 WHERE session_id=? AND task_id=?",
                    (self.session_id, data["task_id"]),
                )

    def chain(self, leaf: str | None = None) -> list[dict]:
        with self._lock:
            cursor = self.leaf if leaf is None else leaf
            records = []
            while cursor:
                if cursor not in self._by_id:
                    raise SessionError("Unknown session ancestry")
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
        with self.transaction():
            if record_id not in self._by_id:
                raise SessionError("Unknown branch point")
            pending = set()
            for message in self.project(record_id):
                pending.update(call.id for call in message.tool_calls)
                if message.tool_call_id:
                    pending.discard(message.tool_call_id)
            if pending:
                raise SessionError("Cannot branch inside an incomplete tool batch")
            self.leaf = record_id
            return self.append("branch", {"from": record_id}, checkpoint=True)["id"]

    def fork(self, path=None, *, session_id=None) -> SessionStore:
        with self._lock:
            view = self.project()
            pending = {call.id for m in view for call in m.tool_calls}
            pending -= {m.tool_call_id for m in view if m.tool_call_id}
            if pending:
                raise SessionError("Cannot fork an incomplete tool batch")
            target_id = session_id or (
                Path(path).stem
                if path and Path(path).suffix == ".jsonl"
                else str(path)
                if path
                else new_id()
            )
            if self._db.execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (target_id,)
            ).fetchone():
                raise SessionError("Fork destination already exists")
            entries = [
                (
                    "seed",
                    {
                        "messages": [m.to_dict() for m in view],
                        "source_session": self.session_id,
                        "source_leaf": self.leaf,
                    },
                )
            ]
            for kind in ("baseline", "goal"):
                value = self.latest(kind)
                if value:
                    entries.append((kind, value))
            return SessionStore(path, self.project_path, session_id=target_id, _seed_events=entries)

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
                            (
                                result.get("content", "Outcome unknown")
                                if result
                                else "Outcome unknown after process interruption; do not automatically retry."
                            ),
                            source="recovery",
                            tool_call_id=call.id,
                            name=call.name,
                            provider_data={
                                "tool_status": (
                                    result.get("status", "unknown") if result else "unknown"
                                )
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
            self.record_child_result(
                {
                    "task_id": task_id,
                    "state": "cancelled",
                    "summary": "",
                    "evidence": [],
                    "artifacts": [],
                    "unresolved": ["process_interrupted"],
                    "usage": 0,
                }
            )

    def checkpoint(self):
        with self._lock:
            if self.closed:
                raise SessionError("Session is closed")
            if not self._depth:
                self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def close(self):
        with self._lock:
            if self.closed:
                return
            if self._depth:
                raise SessionError("Cannot close during a transaction")
            try:
                if not self.failed:
                    self.checkpoint()
            finally:
                self._db.close()
                if self._writer:
                    fcntl.flock(self._writer.fileno(), fcntl.LOCK_UN)
                    self._writer.close()
                self.closed = True

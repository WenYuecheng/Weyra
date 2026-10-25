"""Durability, migration, and inbox boundaries for the authoritative SQLite journal."""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from weyra.contracts import ChildResult, Message, canonical, digest
from weyra.session import SessionError, SessionStore


def test_database_is_authoritative_and_session_id_resumes(tmp_path):
    store = SessionStore(project=tmp_path, session_id="explicit")
    store.add_message(Message("user", "saved"))
    assert store.path == tmp_path / ".weyra" / "state.db"
    assert store.session_id == "explicit"
    store.close()
    store = SessionStore(project=tmp_path, session_id="explicit")
    assert [m.content for m in store.project()] == ["saved"]
    assert SessionStore.list_sessions(tmp_path)[0]["session_id"] == "explicit"
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    assert not list(tmp_path.rglob("*.jsonl"))
    store.close()


def test_transaction_rolls_back_events_blobs_and_leaf(tmp_path):
    store = SessionStore(project=tmp_path)
    original = store.records
    with pytest.raises(ValueError):
        with store.transaction():
            store.append("model_request", {"system": "rollback", "tools": []})
            store.add_message(Message("user", "rollback"))
            raise ValueError("crash")
    assert store.records == original
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
    store.append_many([("message", Message("user", "one").to_dict()), ("done", {})])
    assert store.project()[0].content == "one"
    store.close()


def test_writer_lock_crossprocess_and_independent_sessions(tmp_path):
    store = SessionStore(project=tmp_path, session_id="writer")
    with pytest.raises(SessionError, match="writer"):
        SessionStore(project=tmp_path, session_id="writer")
    script = "from weyra.session import SessionStore; import sys; SessionStore(project=sys.argv[1],session_id='writer')"
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True)
    assert result.returncode != 0 and b"writer" in result.stderr
    other = SessionStore(project=tmp_path, session_id="other")
    other.add_message(Message("user", "independent"))
    other.close()
    store.close()
    SessionStore(project=tmp_path, session_id="writer").close()


def test_threads_serialize_parent_chain(tmp_path):
    store = SessionStore(project=tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: store.add_message(Message("user", str(n))), range(40)))
    assert len(store.chain()) == 41
    assert len({m.content for m in store.project()}) == 40
    store.close()


def test_consumption_and_inbox_are_atomic_idempotent_and_durable(tmp_path):
    store = SessionStore(project=tmp_path, session_id="inbox")
    item = {"id": "input", "kind": "follow_up", "text": "hello"}
    store.append("input_queued", item)
    store.consume_input(item)
    store.consume_input(item)
    assert len(store.project()) == 1
    assert store.latest("input_consumed")["id"] == "input"
    store.record_child_result(ChildResult("task", "done", "evidence"))
    store.record_child_result(ChildResult("task", "done", "evidence"))
    assert len(store.pending_child_results()) == 1
    store.close()
    store = SessionStore(project=tmp_path, session_id="inbox")
    assert store.pending_child_results()[0]["summary"] == "evidence"
    with pytest.raises(ValueError):
        with store.transaction():
            store.accept_child_result("task", Message("user", "child evidence", source="child"))
            raise ValueError("crash before ACK commit")
    assert len(store.pending_child_results()) == 1
    assert len(store.project()) == 1
    store.accept_child_result("task", Message("user", "child evidence", source="child"))
    store.accept_child_result("task", Message("user", "duplicate", source="child"))
    assert not store.pending_child_results()
    assert len(store.project()) == 2
    store.close()


def test_foreground_ack_does_not_add_parent_message(tmp_path):
    store = SessionStore(project=tmp_path)
    store.record_child_result(ChildResult("foreground", "done"))
    store.acknowledge_child_result("foreground")
    assert not store.pending_child_results()
    assert store.project() == []
    store.close()


def legacy(tmp_path, records, tail=b""):
    source = tmp_path / "old.jsonl"
    source.write_bytes(b"".join((canonical(r) + "\n").encode() for r in records) + tail)
    return source


def root(tmp_path):
    return {
        "id": "root",
        "parent": None,
        "kind": "session",
        "data": {"format": 1, "project": str(tmp_path)},
    }


def test_legacy_import_preserves_source_torn_tail_and_hash_ledger(tmp_path):
    records = [
        root(tmp_path),
        {
            "id": "m",
            "parent": "root",
            "kind": "message",
            "data": Message("user", "legacy").to_dict(),
        },
    ]
    source = legacy(tmp_path, records, b'{"unfinished"')
    original = source.read_bytes()
    store = SessionStore(source, tmp_path)
    session_id = store.session_id
    assert store.records == json.loads(canonical(records))
    assert source.read_bytes() == original
    store.add_message(Message("assistant", "new"))
    store.close()
    store = SessionStore(source, tmp_path)
    assert store.session_id == session_id
    assert len(store.project()) == 2
    assert source.read_bytes() == original
    store.close()


@pytest.mark.parametrize("corruption", ["interior", "duplicate", "orphan", "second_root"])
def test_legacy_rejects_corruption_without_partial_import(tmp_path, corruption):
    records = [root(tmp_path)]
    bad = {"id": "m", "parent": "root", "kind": "message", "data": {}}
    if corruption == "duplicate":
        bad["id"] = "root"
    if corruption == "orphan":
        bad["parent"] = "missing"
    if corruption == "second_root":
        bad["parent"] = None
    records.append(bad)
    source = legacy(tmp_path, records)
    if corruption == "interior":
        source.write_bytes(b"broken\n" + source.read_bytes())
    original = source.read_bytes()
    with pytest.raises(SessionError):
        SessionStore(source, tmp_path)
    assert source.read_bytes() == original
    assert SessionStore.list_sessions(tmp_path) == []


def test_legacy_request_blobs_import_into_database(tmp_path):
    value = "system rules"
    key = digest(value)
    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / (key + ".json")).write_text(canonical(value))
    source = legacy(
        tmp_path,
        [
            root(tmp_path),
            {
                "id": "req",
                "parent": "root",
                "kind": "model_request",
                "data": {"system_ref": key},
            },
        ],
    )
    store = SessionStore(source, tmp_path)
    (tmp_path / "blobs" / (key + ".json")).unlink()
    assert store.expand_request(store.records[-1])["system"] == value
    store.close()


def test_fork_is_independent_in_same_database(tmp_path):
    store = SessionStore(project=tmp_path, session_id="parent")
    first = store.add_message(Message("user", "base"))
    store.add_message(Message("assistant", "discard"))
    store.branch(first["id"])
    store.add_message(Message("assistant", "branch"))
    child = store.fork("child")
    assert child.path == store.path
    assert child.session_id == "child"
    child.add_message(Message("user", "child only"))
    assert len(child.project()) == 3 and len(store.project()) == 2
    child.close()
    store.close()
    store = SessionStore(project=tmp_path, session_id="parent")
    assert [m.content for m in store.project()] == ["base", "branch"]
    assert len(store.records) > len(store.chain())
    store.close()


def test_import_rejects_other_project_and_deduplicates_copy(tmp_path):
    source = legacy(tmp_path, [root(tmp_path)])
    other = tmp_path / "other"
    with pytest.raises(SessionError, match="another project"):
        SessionStore(source, other)
    assert SessionStore.list_sessions(other) == []
    store = SessionStore(source, tmp_path)
    original_id = store.session_id
    store.close()
    duplicate = tmp_path / "copy.jsonl"
    duplicate.write_bytes(source.read_bytes())
    imported = SessionStore(duplicate, tmp_path)
    assert imported.session_id == original_id
    assert len(SessionStore.list_sessions(tmp_path)) == 1
    imported.close()


def test_queue_admission_failure_leaves_both_facts_uncommitted(tmp_path, monkeypatch):
    store = SessionStore(project=tmp_path)
    original = store.append

    def append(kind, data, checkpoint=False):
        if kind == "input_consumed":
            raise OSError("disk fault")
        return original(kind, data, checkpoint)

    monkeypatch.setattr(store, "append", append)
    with pytest.raises(OSError):
        store.consume_input({"id": "queued", "text": "must remain pending"})
    assert not store.project()
    assert store.latest("input_consumed") is None
    store.close()


def test_inbox_sql_failure_rolls_back_child_result(tmp_path):
    store = SessionStore(project=tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER refuse_inbox BEFORE INSERT ON child_inbox BEGIN SELECT RAISE(ABORT,'disk fault'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.record_child_result(ChildResult("child", "done"))
    assert store.latest("child_result") is None
    assert not store.pending_child_results()
    store.close()


def test_ack_sql_failure_rolls_back_parent_message_and_ack(tmp_path):
    store = SessionStore(project=tmp_path)
    store.record_child_result(ChildResult("child", "done"))
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER refuse_ack BEFORE INSERT ON events WHEN NEW.kind='child_result_ack' BEGIN SELECT RAISE(ABORT,'disk fault'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.accept_child_result("child", Message("user", "result"))
    assert not store.project()
    assert len(store.pending_child_results()) == 1
    store.close()


@pytest.mark.parametrize("commit", [False, True])
def test_process_exit_keeps_only_committed_transaction(tmp_path, commit):
    script = """
from weyra.session import SessionStore
from weyra.contracts import Message
import os, sys
store = SessionStore(project=sys.argv[1], session_id="crash")
with store.transaction():
    store.add_message(Message("user", "durable"))
    store.append("input_consumed", {"id": "input"})
    if sys.argv[2] == "False":
        os._exit(7)
os._exit(7)
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(commit)], timeout=10)
    assert result.returncode == 7
    store = SessionStore(project=tmp_path, session_id="crash")
    assert len(store.project()) == int(commit)
    assert (store.latest("input_consumed") is not None) == commit
    store.close()


def test_nested_exception_cannot_commit_partial_outer_transaction(tmp_path):
    store = SessionStore(project=tmp_path)
    with pytest.raises(SessionError, match="Nested transaction failed"):
        with store.transaction():
            try:
                with store.transaction():
                    store.add_message(Message("user", "rollback"))
                    raise ValueError("failure")
            except ValueError:
                pass
    assert not store.project()
    store.close()


def test_branch_failure_restores_original_leaf(tmp_path):
    store = SessionStore(project=tmp_path)
    first = store.add_message(Message("user", "base"))
    store.add_message(Message("assistant", "latest"))
    leaf = store.leaf
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER refuse_branch BEFORE INSERT ON events WHEN NEW.kind='branch' BEGIN SELECT RAISE(ABORT,'disk fault'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.branch(first["id"])
    assert store.leaf == leaf
    assert [m.content for m in store.project()] == ["base", "latest"]
    store.close()


def test_other_threads_cannot_observe_uncommitted_projection(tmp_path):
    import threading
    from concurrent.futures import TimeoutError

    store = SessionStore(project=tmp_path)
    inserted, release = threading.Event(), threading.Event()

    def writer():
        with store.transaction():
            store.add_message(Message("user", "pending commit"))
            inserted.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        assert inserted.wait(5)
        reading = pool.submit(store.project)
        try:
            with pytest.raises(TimeoutError):
                reading.result(timeout=0.05)
        finally:
            release.set()
        writing.result(timeout=5)
        assert reading.result(timeout=5)[0].content == "pending commit"
    store.close()


def test_failed_fork_does_not_publish_an_empty_session(tmp_path):
    store = SessionStore(project=tmp_path, session_id="parent")
    store.add_message(Message("user", "seed"))
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER refuse_seed BEFORE INSERT ON events WHEN NEW.kind='seed' BEGIN SELECT RAISE(ABORT,'disk fault'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.fork("child")
    assert [row["session_id"] for row in SessionStore.list_sessions(tmp_path)] == ["parent"]
    store.close()


def test_disk_full_marks_store_failed_without_advancing_projection(tmp_path):
    store = SessionStore(project=tmp_path)
    old_leaf = store.leaf
    pages = store._db.execute("PRAGMA page_count").fetchone()[0]
    store._db.execute(f"PRAGMA max_page_count={pages}")
    with pytest.raises(sqlite3.OperationalError, match="full"):
        store.append("large", {"payload": "x" * 1_000_000})
    assert store.failed
    assert store.leaf == old_leaf
    assert store.latest("large") is None
    with pytest.raises(SessionError, match="persistence has failed"):
        store.add_message(Message("user", "must stop"))
    store.close()


def test_id_is_data_and_cannot_inject_sql(tmp_path):
    session_id = "x'; DROP TABLE sessions; --"
    store = SessionStore(project=tmp_path, session_id=session_id)
    store.add_message(Message("user", "safe"))
    store.close()
    store = SessionStore(project=tmp_path, session_id=session_id)
    assert store.project()[0].content == "safe"
    assert SessionStore.list_sessions(tmp_path)[0]["session_id"] == session_id
    store.close()

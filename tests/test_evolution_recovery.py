import json
from pathlib import Path
from types import SimpleNamespace as NS

from weyra.frontmatter import atomic, sha
from weyra.skill_evolution import EvolutionStore
from weyra.skills import SkillStore


def fixture(tmp_path):
    path = tmp_path / ".weyra" / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: demo\nversion: 1.0.0\ncustom: keep\n---\noriginal")
    skills = SkillStore(tmp_path, skill_dirs=[])
    return skills, path


def test_default_evolution_and_new_skill_paths(tmp_path):
    store = EvolutionStore(SkillStore(tmp_path, skill_dirs=[]))
    result = store.create("new", "new method", authorized=True, event_id="new")
    assert result["status"] == "updated"
    assert store.root == tmp_path / ".weyra" / "skill-evolution"
    assert (tmp_path / ".weyra" / "skills" / "new" / "SKILL.md").exists()
    assert not (tmp_path / "skills" / "new" / "SKILL.md").exists()


def test_recovers_published_update_when_terminal_audit_failed(tmp_path, monkeypatch):
    skills, path = fixture(tmp_path)
    store = EvolutionStore(skills)
    original_record = store._record

    def record(name, event):
        if event["status"] == "updated":
            raise OSError("terminal audit unavailable")
        return original_record(name, event)

    monkeypatch.setattr(store, "_record", record)
    result = store.apply(
        "demo",
        [{"type": "append", "text": "published"}],
        event_id="event",
        base_hash=skills.load("demo").content_hash,
        authorized=True,
        knowledge_note={"kind": "boundary", "text": "Preserve exact names"},
    )
    assert result["status"] == "updated" and result["auxiliary_errors"]
    published = path.read_bytes()
    resumed = EvolutionStore(skills)
    terminal = resumed.history("demo")[-1]
    assert terminal["status"] == "updated" and terminal["recovered"] is True
    assert Path(terminal["skill_path"]) == path.resolve()
    assert Path(terminal["snapshot_path"]).read_bytes() == published
    assert resumed.knowledge("demo")[-1]["event_id"] == "event"
    assert skills.load("demo").version == "1.0.1"
    assert skills.load("demo").metadata["custom"] == "keep"
    again = EvolutionStore(skills)
    assert len([event for event in again.history("demo") if event["status"] == "updated"]) == 1
    assert path.read_bytes() == published


def prepared(store, name, path, target, base=None, event_id="incomplete"):
    folder = store._dir(name)
    event = {
        "status": "prepared",
        "name": name,
        "event_id": event_id,
        "version": "1.0.1",
        "base_hash": base,
        "content_hash": sha(target),
        "snapshot_hash": sha(target),
        "skill_path": str(path.resolve()),
        "snapshot_path": str(folder / "1.0.1.md"),
    }
    store._record(name, event)
    return event


def test_prepared_without_publication_does_not_apply_or_replay(tmp_path):
    skills, path = fixture(tmp_path)
    store = EvolutionStore(skills)
    original = path.read_text()
    prepared(store, "demo", path, "target", sha(original))
    resumed = EvolutionStore(skills)
    assert resumed.history("demo")[-1]["status"] == "interrupted"
    assert path.read_text() == original
    assert len(EvolutionStore(skills).history("demo")) == 2


def test_recovery_conflict_is_visible_without_overwrite(tmp_path):
    skills, path = fixture(tmp_path)
    store = EvolutionStore(skills)
    prepared(store, "demo", path, "target", sha(path.read_text()))
    path.write_text(path.read_text() + "\nexternal edit")
    original = path.read_bytes()
    resumed = EvolutionStore(skills)
    assert resumed.history("demo")[-1]["status"] == "conflict"
    assert path.read_bytes() == original


def test_legacy_prepared_without_path_does_not_guess_same_name_scope(tmp_path):
    skills, path = fixture(tmp_path)
    store = EvolutionStore(skills)
    folder = store._dir("demo")
    store._record(
        "demo",
        {
            "status": "prepared",
            "name": "demo",
            "event_id": "legacy",
            "content_hash": sha(path.read_text()),
            "version": "1.0.1",
        },
    )
    original = path.read_bytes()
    EvolutionStore(skills)
    records = [json.loads(line) for line in (folder / "provenance.jsonl").read_text().splitlines()]
    assert records[-1]["status"] == "interrupted" and "path" in records[-1]["reason"]
    assert path.read_bytes() == original
    assert not (folder / "1.0.1.md").exists()


def test_audit_is_fsynced_and_mirrored_to_session(tmp_path, monkeypatch):
    calls = []
    records = []
    monkeypatch.setattr("weyra.skill_evolution.os.fsync", lambda fd: calls.append(fd))
    store = EvolutionStore(
        SkillStore(tmp_path, skill_dirs=[]),
        session=NS(append=lambda kind, data, **kw: records.append((kind, data, kw))),
    )
    result = store.create("new", "method", authorized=True, event_id="event")
    assert result["status"] == "updated"
    assert len(calls) >= 2
    assert [record[1]["status"] for record in records] == ["prepared", "updated"]
    assert all(record[0] == "skill_evolution" and record[2]["checkpoint"] for record in records)


def test_user_and_project_same_name_recovery_uses_recorded_path(tmp_path, monkeypatch):
    user = tmp_path / "user-skills" / "demo" / "SKILL.md"
    user.parent.mkdir(parents=True)
    user.write_text("---\nname: demo\nversion: 1.0.0\n---\nuser")
    skills = SkillStore(tmp_path, skill_dirs=[user.parent.parent])
    store = EvolutionStore(skills)
    original = store._record

    def fail(name, event):
        if event["status"] == "updated":
            raise OSError("audit failed")
        original(name, event)

    monkeypatch.setattr(store, "_record", fail)
    store.apply(
        "demo",
        [{"type": "append", "text": "published"}],
        base_hash=skills.load("demo").content_hash,
        event_id="user-event",
        authorized=True,
    )
    user_bytes = user.read_bytes()
    project = tmp_path / ".weyra" / "skills" / "demo" / "SKILL.md"
    project.parent.mkdir(parents=True)
    project.write_text("---\nname: demo\nversion: 1.0.0\n---\nproject")
    resumed = EvolutionStore(skills)
    assert user.read_bytes() == user_bytes
    assert skills.load("demo").body == "project"
    recovery = [r for r in resumed.recovery_results if r["event_id"] == "user-event"][0]
    assert recovery["status"] == "updated" and recovery["skill_path"] == str(user.resolve())


def test_recovery_snapshot_failure_remains_retryable_without_touching_skill(tmp_path, monkeypatch):
    skills, path = fixture(tmp_path)
    store = EvolutionStore(skills)
    original_record = store._record

    def record(name, event):
        if event["status"] == "updated":
            raise OSError("audit unavailable")
        original_record(name, event)

    monkeypatch.setattr(store, "_record", record)
    store.apply(
        "demo",
        [{"type": "append", "text": "published"}],
        event_id="retry-recovery",
        base_hash=skills.load("demo").content_hash,
        authorized=True,
    )
    published = path.read_bytes()

    def failing_snapshot(target, text):
        if Path(target).name == "1.0.1.md":
            raise OSError("snapshot temporarily unavailable")
        atomic(target, text)

    with monkeypatch.context() as patch:
        patch.setattr("weyra.skill_evolution.atomic", failing_snapshot)
        failed = EvolutionStore(skills)
    assert failed.recovery_results[-1]["auxiliary_errors"]
    resumed = EvolutionStore(skills)
    assert resumed.history("demo")[-1]["status"] == "updated"
    assert path.read_bytes() == published


def test_create_preparation_audit_failure_does_not_publish(tmp_path, monkeypatch):
    store = EvolutionStore(SkillStore(tmp_path, skill_dirs=[]))
    monkeypatch.setattr(
        store, "_record", lambda *args: (_ for _ in ()).throw(OSError("audit unavailable"))
    )
    result = store.create("new", "method", authorized=True, event_id="new")
    assert result["status"] == "rejected" and result["auxiliary_errors"]
    assert not (tmp_path / ".weyra" / "skills" / "new" / "SKILL.md").exists()


def test_torn_audit_tail_does_not_hide_recovery_terminal_record(tmp_path):
    skills, path = fixture(tmp_path)
    store = EvolutionStore(skills)
    prepared(store, "demo", path, path.read_text() + "\npublished", sha(path.read_text()))
    path.write_text(path.read_text() + "\npublished")
    audit = store._dir("demo") / "provenance.jsonl"
    with audit.open("a") as output:
        output.write('{"status":')
    resumed = EvolutionStore(skills)
    assert resumed.history("demo")[-1]["status"] == "updated"
    assert resumed.auxiliary_errors
    assert len([e for e in EvolutionStore(skills).history("demo") if e["status"] == "updated"]) == 1


def test_completed_event_id_is_not_published_twice(tmp_path):
    skills, path = fixture(tmp_path)
    store = EvolutionStore(skills)
    args = dict(
        name="demo",
        edits=[{"type": "append", "text": "published"}],
        event_id="same",
        base_hash=skills.load("demo").content_hash,
        authorized=True,
    )
    first = store.apply(**args)
    published = path.read_bytes()
    second = store.apply(**args)
    assert first == second and path.read_bytes() == published
    assert len([e for e in store.history("demo") if e["status"] == "updated"]) == 1


def test_file_audit_reconciles_missing_sqlite_terminal_after_session_failure(tmp_path):
    skills, path = fixture(tmp_path)

    class Session:
        def __init__(self, fail=False):
            self.records = []
            self.fail = fail

        def append(self, kind, data, **kw):
            if self.fail and data["status"] == "updated":
                raise RuntimeError("sqlite unavailable")
            self.records.append({"kind": kind, "data": dict(data)})

    failed = Session(fail=True)
    store = EvolutionStore(skills, session=failed)
    result = store.apply(
        "demo",
        [{"type": "append", "text": "published"}],
        event_id="session-failure",
        base_hash=skills.load("demo").content_hash,
        authorized=True,
    )
    assert result["status"] == "updated" and result["auxiliary_errors"]
    published = path.read_bytes()
    healthy = Session()
    resumed = EvolutionStore(skills, session=healthy)
    assert [r["data"]["status"] for r in healthy.records] == ["prepared", "updated"]
    assert not resumed.recovery_results and path.read_bytes() == published

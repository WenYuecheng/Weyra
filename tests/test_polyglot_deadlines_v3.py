"""Offline deadline and cleanup regressions for isolated benchmark work."""
import base64
import json
import time
from types import SimpleNamespace as NS

import pytest

from benchmarks.polyglot import grading_v2, sandbox


def test_every_sandbox_operation_clamps_to_absolute_deadline(monkeypatch):
    now, calls = [100.0], []

    def command(argv, *, data=None, timeout=120):
        calls.append((argv[1], timeout))
        now[0] += 1
        if argv[1] == "inspect":
            return {"returncode": 0, "stdout": "[{}]", "stderr": ""}
        return {"returncode": 0, "stdout": '{"status":"success"}', "stderr": ""}

    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(sandbox, "command", command)
    box = sandbox.Sandbox("image", deadline=110)
    box.create({"a.py": "pass"})
    box.inspect()
    box.worker("read_file", {"path": "a.py"})
    box.execute(["true"], timeout=60)
    assert calls == [("create", 10), ("start", 9), ("exec", 8),
                     ("inspect", 7), ("exec", 6), ("exec", 5)]
    now[0] = 111
    for operation in (lambda: box.create({}), box.inspect,
                      lambda: box.worker("read_file", {}), lambda: box.execute(["true"])):
        with pytest.raises(TimeoutError):
            operation()
    box.close()
    assert calls[-1] == ("rm", 30)


def test_uncertain_create_timeout_still_removes_owned_name(monkeypatch):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        return {"returncode": 124 if argv[1] == "create" else 0,
                "stdout": "", "stderr": "deadline"}

    monkeypatch.setattr(sandbox, "command", command)
    box = sandbox.Sandbox("image")
    with pytest.raises(RuntimeError):
        box.create({})
    box.close()
    assert calls[-1] == ["docker", "rm", "-f", box.name]


def test_cleanup_missing_owned_container_is_benign(monkeypatch):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv[1])
        return {"returncode": 1, "stdout": "",
                "stderr": "Error response from daemon: No such container: owned"}

    monkeypatch.setattr(sandbox, "command", command)
    box = sandbox.Sandbox("image")
    with pytest.raises(RuntimeError):
        box.create({})
    box.close()
    assert calls == ["create", "rm"]


@pytest.mark.parametrize("requested_deadline", [105, 1000])
@pytest.mark.parametrize("expire_on_proof", [False, True])
def test_verifier_uses_whole_deadline_and_rejects_late_pass(
    tmp_path, monkeypatch, expire_on_proof, requested_deadline
):
    now, instances = [100.0], []
    expected_deadline = min(220, requested_deadline)
    report = b'<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="a" name="test_a"/></testsuite>'

    class Box:
        def __init__(self, image, deadline=None):
            self.deadline = deadline
            self.closed = False
            instances.append(self)

        def create(self, files):
            pass

        def inspect(self):
            return {}

        def execute(self, argv, timeout=60):
            if argv[2] == grading_v2._READ:
                raw = report if argv[3].endswith("junit.xml") else b""
                return {"returncode": 0, "stdout": base64.b64encode(raw).decode(), "stderr": ""}
            return {"returncode": 0, "stdout": "", "stderr": ""}

        def export(self, paths):
            if expire_on_proof:
                now[0] = expected_deadline + 1
            return {"test_a.py": b"original"}

        def close(self):
            self.closed = True

    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(grading_v2, "Sandbox", Box)
    monkeypatch.setattr(grading_v2, "isolation", lambda row: {})
    task = NS(language="python", editable=("a.py",),
              verifier_files={"a.py": "", "test_a.py": "original"}, command=("pytest",))
    row = grading_v2.verify(task, "image", {"a.py": b"pass"}, tmp_path, ["a::test_a"], deadline=requested_deadline)
    assert instances[0].deadline == expected_deadline and instances[0].closed
    assert row["verdict"] == ("environment_error" if expire_on_proof else "pass")
    if expire_on_proof:
        assert row["classification"] == "verification_timeout"
        assert not row["eligible_feedback"]
    assert json.loads((tmp_path / "verification.json").read_text())["verdict"] == row["verdict"]


def test_expired_verifier_does_not_start_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 100)
    calls = []

    class Box:
        def __init__(self, image, deadline=None):
            pass

        def create(self, files):
            calls.append("create")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(grading_v2, "Sandbox", Box)
    task = NS(language="python", editable=("a.py",), verifier_files={}, command=("pytest",))
    row = grading_v2.verify(task, "image", {}, tmp_path, deadline=99)
    assert calls == ["close"]
    assert row["classification"] == "verification_timeout"


def test_execute_timeout_allows_only_cleanup_after_deadline(monkeypatch):
    now, calls = [100], []

    def command(argv, *, timeout=120, **kwargs):
        calls.append((argv[1], timeout))
        now[0] = 106
        return {"returncode": 124 if argv[1] == "exec" else 0,
                "stdout": "", "stderr": ""}

    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(sandbox, "command", command)
    box = sandbox.Sandbox("image", deadline=105)
    box.created = True
    assert box.execute(["true"])["returncode"] == 124
    assert calls == [("exec", 5), ("rm", 30)]
    assert not box.created

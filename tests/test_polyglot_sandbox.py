"""Benchmark sandbox tests. Real Docker cases require POLYGLOT_DOCKER_TESTS=1."""

import io
import os
import tarfile

import pytest

from benchmarks.polyglot.sandbox import Sandbox, safe_path
from benchmarks.polyglot import worker


@pytest.mark.parametrize("name", ["../x", "/etc/passwd", "a/../x", "a//b", ".", "a\\b"])
def test_reject_unsafe_paths(name):
    with pytest.raises(ValueError):
        safe_path(name)


def test_nested_archive_is_regular_and_owned():
    blob = Sandbox._archive({"nested/main.py": "print(42)"})
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        entry = archive.getmember("nested/main.py")
        assert entry.isfile() and entry.uid == 1000 and entry.gid == 1000
        assert archive.extractfile(entry).read() == b"print(42)"


def test_worker_export_rejects_symlink_and_parent_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "ok").write_text("ok")
    (tmp_path / "link").symlink_to("real", target_is_directory=True)
    (tmp_path / "filelink").symlink_to("real/ok")
    for name in ["filelink", "link/ok"]:
        with pytest.raises((OSError, ValueError)):
            worker.export_files([name])
    assert worker.export_files(["real/ok"]) == {"real/ok": "b2s="}


def test_worker_read_edit_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    (tmp_path / "x.py").write_text("return 1\n")
    versions = {}
    with pytest.raises(ValueError, match="Read"):
        worker.dispatch("edit_file", {"path": "x.py", "old_text": "1", "new_text": "2"}, versions)
    assert worker.dispatch("read_file", {"path": "x.py"}, versions)["content"] == "return 1\n"
    worker.dispatch("edit_file", {"path": "x.py", "old_text": "1", "new_text": "2"}, versions)
    assert (tmp_path / "x.py").read_text() == "return 2\n"


@pytest.mark.skipif(os.getenv("POLYGLOT_DOCKER_TESTS") != "1", reason="opt-in Docker smoke")
def test_docker_function_isolation_and_timeout():
    image = os.getenv("POLYGLOT_DOCKER_IMAGE", "weyra-polyglot:20261023")
    with Sandbox(image) as sandbox:
        sandbox.create({"nested/main.py": "def add(a,b): return a-b\n"})
        code = "from nested.main import add; assert add(2,3)==5"
        assert sandbox.execute(["python", "-B", "-c", code])["returncode"] != 0
        sandbox.worker("read_file", {"path": "nested/main.py"})
        sandbox.worker(
            "write_file", {"path": "nested/main.py", "content": "def add(a,b): return a+b\n"}
        )
        assert sandbox.execute(["python", "-B", "-c", code])["returncode"] == 0
        assert sandbox.export(["nested/main.py"])["nested/main.py"].endswith(b"a+b\n")
        inspection = sandbox.inspect()
        assert inspection["HostConfig"]["NetworkMode"] == "none"
        assert not inspection["HostConfig"].get("Binds")
        assert inspection["HostConfig"]["ReadonlyRootfs"]
        assert inspection["Config"]["User"] == "1000:1000"
        result = sandbox.execute(
            [
                "python",
                "-c",
                'import os,socket; assert os.getuid()==1000; assert not os.path.exists("/var/run/docker.sock"); s=socket.socket(); s.settimeout(.5); s.connect(("1.1.1.1",443))',
            ]
        )
        assert result["returncode"] != 0
        sandbox.execute(["ln", "-s", "/etc/passwd", "bad"])
        with pytest.raises(RuntimeError):
            sandbox.export(["bad"])
        result = sandbox.execute(
            [
                "python",
                "-c",
                'import subprocess,time; subprocess.Popen(["sleep","60"]); time.sleep(60)',
            ],
            timeout=1,
        )
        assert result["returncode"] == 124
        assert not sandbox.inspect()["State"]["Running"]

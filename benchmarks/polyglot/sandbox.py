"""No-network Docker workspace and strict allowlisted artifact transfer."""

import base64
import io
import json
import math
from pathlib import PurePosixPath
import subprocess
import tarfile
import tempfile
import time
import uuid


def safe_path(name):
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or PurePosixPath(name).is_absolute()
        or any(p in ("", ".", "..") for p in name.split("/"))
    ):
        raise ValueError("Unsafe relative path")
    return name


def command(argv, *, data=None, timeout=120):
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
            stdout=out,
            stderr=err,
        )
        try:
            p.communicate(data, timeout=timeout)
            code = p.returncode
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            code = 124
        out.seek(0)
        err.seek(0)
        return {
            "returncode": code,
            "stdout": out.read(1024 * 1024).decode(errors="replace"),
            "stderr": err.read(256 * 1024).decode(errors="replace"),
        }


class Sandbox:
    def __init__(self, image, name=None, deadline=None):
        self.image = image
        self.name = name or "weyra-polyglot-" + uuid.uuid4().hex[:16]
        self.versions = {}
        self.created = False
        self._creation_attempted = False
        if deadline is not None and (isinstance(deadline, bool) or not math.isfinite(deadline)):
            raise ValueError("Deadline must be a finite absolute timestamp")
        self.deadline = deadline

    def _timeout(self, timeout):
        if self.deadline is None:
            return timeout
        remaining = self.deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("Sandbox deadline expired")
        return min(timeout, remaining)

    @staticmethod
    def _archive(files):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w") as archive:
            for path, value in files.items():
                safe_path(path)
                raw = value.encode() if isinstance(value, str) else value
                entry = tarfile.TarInfo(path)
                entry.size = len(raw)
                entry.uid = entry.gid = 1000
                entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(raw))
        return out.getvalue()

    def create(self, files, *, writable=True):
        args = [
            "docker",
            "create",
            "--name",
            self.name,
            "--network",
            "none",
            "--memory",
            "2g",
            "--memory-swap",
            "2g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--user",
            "1000:1000",
            "--workdir",
            "/work",
            "--tmpfs",
            "/work:rw,nosuid,nodev,size=128m,uid=1000,gid=1000",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            self.image,
            "sleep",
            "infinity",
        ]
        timeout = self._timeout(120)
        # Docker may create the container even when its client times out.
        self._creation_attempted = True
        r = command(args, timeout=timeout)
        if r["returncode"]:
            raise RuntimeError(r["stderr"])
        self.created = True
        r = command(["docker", "start", self.name], timeout=self._timeout(120))
        if r["returncode"]:
            raise RuntimeError(r["stderr"])
        for path in files:
            safe_path(path)
        payload = {
            p: base64.b64encode(v.encode() if isinstance(v, str) else v).decode()
            for p, v in files.items()
        }
        result = self.worker("stage", {"files": payload})
        if result.get("status") == "error":
            raise RuntimeError(result["content"])
        return self

    def execute(self, argv, timeout=60):
        r = command(["docker", "exec", "-i", self.name, *argv], timeout=self._timeout(timeout))
        if r["returncode"] == 124:
            # Cleanup remains available after expiry; no new timed work starts.
            if self.deadline is None:
                command(["docker", "kill", self.name], timeout=15)
            else:
                self.close()
        return r

    def worker(self, operation, args):
        raw = json.dumps({"operation": operation, "args": args, "versions": self.versions}).encode()
        r = command(
            ["docker", "exec", "-i", self.name, "python", "/opt/worker.py"], data=raw, timeout=self._timeout(135)
        )
        try:
            result = json.loads(r["stdout"])
        except ValueError:
            raise RuntimeError("Container worker failed: " + r["stderr"][:1000]) from None
        self.versions = result.pop("versions", self.versions)
        if operation == "export" and r["returncode"]:
            raise RuntimeError(result.get("content", "Invalid artifact"))
        return result

    def export(self, paths):
        for p in paths:
            safe_path(p)
        rows = self.worker("export", {"paths": paths})["files"]
        return {p: base64.b64decode(v, validate=True) for p, v in rows.items()}

    def inspect(self):
        r = command(["docker", "inspect", self.name], timeout=self._timeout(120))
        if r["returncode"]:
            raise RuntimeError(r["stderr"])
        return json.loads(r["stdout"])[0]

    def close(self):
        if self.created or self._creation_attempted:
            r = command(["docker", "rm", "-f", self.name], timeout=30)
            if r["returncode"] and "no such container" not in r["stderr"].lower():
                raise RuntimeError("Container cleanup failed: " + r["stderr"])
            self.created = False
            self._creation_attempted = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

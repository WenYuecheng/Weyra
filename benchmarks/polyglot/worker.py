"""Isolated benchmark file operations; installed read-only outside the workspace."""

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import tempfile

ROOT = Path("/work")
LIMIT = 32 * 1024 * 1024


def checked(name):
    p = PurePosixPath(name)
    if (
        not name
        or p.is_absolute()
        or "\\" in name
        or any(x in ("", ".", "..") for x in name.split("/"))
    ):
        raise ValueError("Invalid relative file path")
    return p


def open_file(name, mode="rb"):
    p = checked(name)
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in p.parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        flags = os.O_RDONLY if mode == "rb" else os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        out = os.open(p.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=fd)
        import stat

        if not stat.S_ISREG(os.fstat(out).st_mode):
            os.close(out)
            raise ValueError("Only regular files are accepted")
        return os.fdopen(out, mode)
    finally:
        os.close(fd)


def read_bytes(name):
    with open_file(name) as f:
        data = f.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("File too large")
    return data


def export_files(names):
    out = {}
    total = 0
    for name in names:
        raw = read_bytes(name)
        total += len(raw)
        if total > LIMIT:
            raise ValueError("Artifacts exceed 32 MiB")
        out[name] = base64.b64encode(raw).decode()
    return out


def dispatch(op, args, versions):
    name = args.get("path", ".")
    if op == "stage":
        for path, value in args["files"].items():
            dispatch(
                "write_file",
                {"path": path, "content": base64.b64decode(value, validate=True).decode("utf-8")},
                versions,
            )
        return {"content": "Files staged"}
    if op == "export":
        return {"files": export_files(args["paths"])}
    if op == "read_file":
        raw = read_bytes(name)
        versions[name] = hashlib.sha256(raw).hexdigest()
        lines = raw.decode("utf-8").splitlines(keepends=True)
        start = args.get("offset", 1)
        if type(start) is not int or start < 1:
            raise ValueError("Offset starts at 1")
        content = "".join(lines[start - 1 : start - 1 + min(args.get("limit", 2000), 2000)])
        content = content.encode()[: 50 * 1024].decode("utf-8", errors="ignore")
        return {
            "content": content,
            "structured": {"total_lines": len(lines), "sha256": versions[name]},
        }
    if op in ("write_file", "edit_file"):
        checked(name)
        exists = (ROOT / name).exists() or (ROOT / name).is_symlink()
        old = read_bytes(name) if exists else b""
        if exists and versions.get(name) != hashlib.sha256(old).hexdigest():
            raise ValueError("Read the current file before editing; version conflict")
        if op == "edit_file":
            before = old.decode()
            if not args["old_text"] or before.count(args["old_text"]) != 1:
                raise ValueError("old_text must match exactly once")
            text = before.replace(args["old_text"], args["new_text"], 1)
        else:
            text = args["content"]
        if len(text.encode()) > LIMIT:
            raise ValueError("File too large")
        # Only create validated, nonsymlink parents.
        parent = ROOT
        for part in checked(name).parts[:-1]:
            parent = parent / part
            if parent.is_symlink():
                raise ValueError("Symlink parent")
            parent.mkdir(exist_ok=True)
        with open_file(name, "wb") as f:
            f.write(text.encode())
        versions[name] = hashlib.sha256(text.encode()).hexdigest()
        return {"content": f"Wrote {name} ({len(text.encode())} bytes)"}
    if op in ("list_files", "grep_search"):
        base = ROOT if name == "." else ROOT / checked(name)
        if base.is_symlink() or not base.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("Path escapes workspace")
        pattern = re.compile(args.get("pattern", "")) if op == "grep_search" else None
        rows = []
        for folder, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink())
            for item in sorted(files):
                path = Path(folder) / item
                if path.is_symlink() or not path.is_file():
                    continue
                rel = str(path.relative_to(ROOT))
                if pattern is None:
                    rows.append(rel)
                else:
                    try:
                        for num, line in enumerate(read_bytes(rel).decode().splitlines(), 1):
                            if pattern.search(line):
                                rows.append(f"{rel}:{num}:{line[:500]}")
                    except (UnicodeError, ValueError):
                        pass
                if len(rows) >= (min(args.get("limit", 100), 100) if pattern else 5000):
                    return {"content": "\n".join(rows)}
        return {"content": "\n".join(rows)}
    if op == "run_shell":
        if args.get("background"):
            raise ValueError("Background jobs are unavailable in this benchmark")
        timeout = min(max(float(args.get("timeout", 60)), 0.1), 120)
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            p = subprocess.Popen(
                ["/bin/sh", "-c", args["command"]],
                cwd=ROOT,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
                p.returncode = 124
            out.seek(0)
            err.seek(0)
            content = out.read(50 * 1024).decode(errors="replace") + err.read(14 * 1024).decode(
                errors="replace"
            )
        return {
            "content": content,
            "structured": {"exit_code": p.returncode},
            "status": "success" if p.returncode == 0 else "error",
        }
    raise ValueError("Tool unavailable")


if __name__ == "__main__":
    request = json.load(sys.stdin)
    versions = request.get("versions", {})
    try:
        result = dispatch(request["operation"], request["args"], versions)
        result["versions"] = versions
        print(json.dumps(result))
    except Exception as e:
        print(
            json.dumps(
                {"status": "error", "content": f"{type(e).__name__}: {e}", "versions": versions}
            )
        )
        sys.exit(1)

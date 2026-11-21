"""Official Polyglot single-attempt runner. No paid work on import or preflight."""

import hashlib

import json

from pathlib import Path


import xml.etree.ElementTree as ET


from weyra.contracts import digest


from .sandbox import Sandbox

BASE = Path(__file__).resolve().parents[2]


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        import os

        os.fsync(f.fileno())
    tmp.replace(path)


def validate_verdict(language, code, stats):
    if not stats:
        return "environment_error"
    if language == "javascript":
        total = stats.get("numTotalTests", 0)
        if not total or stats.get("numPendingTests", 0) or stats.get("numTodoTests", 0):
            return "environment_error"
        if (
            code == 0
            and stats.get("numPassedTests") == total
            and not stats.get("numFailedTests", 0)
        ):
            return "pass"
        return "fail"
    if not stats.get("tests") or stats.get("skipped", 0):
        return "environment_error"
    return (
        "pass"
        if code == 0 and not stats.get("failures", 0) and not stats.get("errors", 0)
        else "fail"
    )


def verify(task, image, files, folder, expected_cases=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    box = Sandbox(image)
    try:
        box.create({**task.verifier_files, **files})
        inspection = box.inspect()
        atomic(folder / "isolation.json", isolation(inspection))
        result = box.execute(list(task.command), timeout=120)
        (folder / "stdout.txt").write_text(result["stdout"])
        (folder / "stderr.txt").write_text(result["stderr"])
        stats = {}
        if task.language == "javascript":
            r = box.execute(["cat", "/tmp/jest-result.json"])
            if r["returncode"] == 0:
                try:
                    stats = json.loads(r["stdout"])
                    assertions = [
                        a
                        for suite in stats.get("testResults", [])
                        for a in suite.get("assertionResults", [])
                    ]
                    stats["case_ids"] = sorted(a["fullName"] for a in assertions)
                    stats["failed_case_ids"] = sorted(
                        a["fullName"] for a in assertions if a.get("status") == "failed"
                    )
                except ValueError:
                    pass
        else:
            r = box.execute(["cat", "/tmp/junit.xml"])
            if r["returncode"] == 0:
                try:
                    tree = ET.fromstring(r["stdout"])
                    suites = [tree] if tree.tag == "testsuite" else list(tree.iter("testsuite"))
                    stats = {
                        k: sum(int(s.attrib.get(k, 0)) for s in suites)
                        for k in ("tests", "failures", "errors", "skipped")
                    }
                except (ET.ParseError, ValueError):
                    pass
        if task.language == "python" and stats:
            cases = [
                (case.attrib.get("classname", "") + "::" + case.attrib.get("name", ""), case)
                for case in tree.iter("testcase")
            ]
            stats["case_ids"] = sorted(name for name, _ in cases)
            stats["failed_case_ids"] = sorted(
                name
                for name, case in cases
                if any(case.find(tag) is not None for tag in ("failure", "error"))
            )
        verdict = validate_verdict(task.language, result["returncode"], stats)
        immutable = {
            p: text.encode() for p, text in task.verifier_files.items() if p not in task.editable
        }
        try:
            if box.export(list(immutable)) != immutable:
                verdict = "environment_error"
                stats["test_files_modified"] = True
        except Exception:
            verdict = "environment_error"
            stats["test_files_unreadable"] = True
        if expected_cases is not None and stats.get("case_ids") != expected_cases:
            verdict = "environment_error"
            stats["case_identity_mismatch"] = True
        atomic(
            folder / "verification.json",
            {"returncode": result["returncode"], "stats": stats, "verdict": verdict},
        )
        return verdict, stats
    finally:
        box.close()
        atomic(folder / "cleanup.json", {"removed": True})


def isolation(i):
    h = i["HostConfig"]
    cfg = i["Config"]
    data = {
        "name": i["Name"],
        "image": i["Image"],
        "network": h["NetworkMode"],
        "binds": h.get("Binds"),
        "mounts": i.get("Mounts", []),
        "user": cfg["User"],
        "privileged": h["Privileged"],
        "cap_drop": h.get("CapDrop"),
        "security_opt": h.get("SecurityOpt"),
        "readonly_root": h["ReadonlyRootfs"],
        "memory": h["Memory"],
        "pids": h["PidsLimit"],
        "env_names": [x.split("=", 1)[0] for x in cfg["Env"]],
    }
    assert (
        data["network"] == "none"
        and not data["binds"]
        and not data["privileged"]
        and data["user"] == "1000:1000"
    )
    assert data["readonly_root"] and h["Memory"] == 2 * 1024**3 and h["PidsLimit"] == 256
    assert not any("KEY" in x or "TOKEN" in x or "SECRET" in x for x in data["env_names"])
    return data


def source_hash():
    return digest(
        {
            str(p.relative_to(BASE)): hashlib.sha256(p.read_bytes()).hexdigest()
            for top in ("weyra", "benchmarks/polyglot_full")
            for p in sorted((BASE / top).rglob("*.py"))
        }
    )

"""Validate and freeze explicitly reused baseline evidence."""

import hashlib
import json
from pathlib import Path
import shutil

CONDITIONS = (
    "dataset", "model", "endpoint", "images", "checks", "task_tokens", "task_calls",
    "rounds", "baseline_skill", "system",
)
TREATMENT_ONLY = {"repair_context", "repair_review"}


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(folder):
    if folder.is_symlink():
        raise ValueError("Baseline evidence root must be a regular directory")
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError("Baseline evidence contains a symlink")
        if path.is_file():
            files[path.relative_to(folder).as_posix()] = _hash(path)
        elif not path.is_dir():
            raise ValueError("Baseline evidence must contain regular files")
    return files


def _folder(root, task_id):
    folder = root / "formal" / task_id / "A"
    if not folder.resolve().is_relative_to(root.resolve()):
        raise ValueError("Baseline evidence escaped its root")
    current = folder
    while current != root:
        if current.is_symlink():
            raise ValueError("Baseline evidence root contains a symlink")
        current = current.parent
    return folder


def _documents(source):
    paths = source / "full-protocol.json", source / "full-report.json"
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("Baseline protocol and report must be regular files")
    return paths


def _validate_artifacts(folder, result_path, result, cases):
    attempt = result_path.parent
    if not all((attempt / name).is_file() for name in ("history.json", "trajectory.jsonl")):
        raise ValueError("Baseline trajectory evidence is missing")
    rounds = result.get("rounds", [])
    if result["status"] == "pass" and (not rounds or rounds[-1]["verdict"] != "pass"):
        raise ValueError("Baseline passing result lacks verification")
    for row in rounds:
        if type(row.get("round")) is not int or row["round"] < 1:
            raise ValueError("Baseline verification round is invalid")
        stage = attempt / str(row["round"])
        record = stage / "verification.json"
        if not record.is_file():
            raise ValueError("Baseline verification evidence is missing")
        verification = json.loads(record.read_text())
        if verification["verdict"] != row["verdict"]:
            raise ValueError("Baseline verification/result mismatch")
        if row["verdict"] == "pass" and verification["stats"].get("case_ids") != cases:
            raise ValueError("Baseline verification case mismatch")
        if not row.get("file_hashes"):
            raise ValueError("Baseline implementation evidence is missing")
        for name, digest in row["file_hashes"].items():
            path = stage / "implementation" / name
            if not path.resolve().is_relative_to(folder.resolve()) or not path.is_file() or _hash(path) != digest:
                raise ValueError("Baseline implementation evidence is missing or changed")


def snapshot(source, expected, tasks):
    """Validate complete shared A conditions and hash all original A evidence."""
    source = Path(source).resolve()
    protocol_path, report_path = _documents(source)
    previous = json.loads(protocol_path.read_text())
    report = json.loads(report_path.read_text())
    if len(tasks) != 225 or report.get("campaign_status") != "completed" or not report.get("coverage_complete"):
        raise ValueError("Baseline reuse requires a completed 225-task campaign")
    if any(previous.get(key) != expected.get(key) for key in CONDITIONS):
        raise ValueError("Baseline conditions differ")
    profiles = [
        {key: value for key, value in item["profile"].items() if key not in TREATMENT_ONLY}
        for item in (previous, expected)
    ]
    if profiles[0] != profiles[1] or set(previous["checks"]) != set(tasks):
        raise ValueError("Baseline conditions differ")
    rows = report.get("rows", [])
    row_map = {row["id"]: row for row in rows}
    if len(rows) != 225 or len(row_map) != 225 or set(row_map) != set(tasks):
        raise ValueError("Baseline report requires 225 unique task rows")
    if any((row.get("R") or {}).get("status") not in {"pass", "fail", "resource_exhausted"} for row in rows):
        raise ValueError("Baseline reuse requires a completed 225-task campaign")
    outcomes, tokens = {}, 0
    for task_id in tasks:
        folder = _folder(source, task_id)
        files = _files(folder)
        results = sorted(folder.glob("attempt-*/result.json"), reverse=True)
        if not results:
            raise ValueError("Baseline reuse requires a completed 225-task campaign")
        result = json.loads(results[0].read_text())
        if row_map[task_id].get("A") != result:
            raise ValueError("Baseline report/result mismatch")
        if result.get("status") not in {"pass", "fail", "resource_exhausted"}:
            raise ValueError("Baseline reuse requires a completed 225-task campaign")
        _validate_artifacts(folder, results[0], result, previous["checks"][task_id]["cases"])
        outcomes[task_id] = {"files": files}
        tokens += result.get("accounted_tokens", 0)
    return {
        "source": str(source), "protocol_sha256": _hash(protocol_path),
        "report_sha256": _hash(report_path), "reused_arms": ["A"],
        "accounted_tokens_from_original_campaign": tokens, "outcomes": outcomes,
    }


def restore(source, output, frozen):
    """Copy immutable A outcomes only; a new ledger charges only new model requests."""
    source, output = Path(source).resolve(), Path(output)
    _documents(source)
    if (
        str(source) != frozen["source"]
        or _hash(source / "full-protocol.json") != frozen["protocol_sha256"]
        or _hash(source / "full-report.json") != frozen["report_sha256"]
    ):
        raise ValueError("Baseline evidence changed")
    for task_id, expected in frozen["outcomes"].items():
        origin = _folder(source, task_id)
        if _files(origin) != expected["files"]:
            raise ValueError("Baseline evidence changed")
    for task_id, expected in frozen["outcomes"].items():
        origin, target = _folder(source, task_id), _folder(output, task_id)
        if target.exists():
            observed = _files(target)
            if any(expected["files"].get(name) != digest for name, digest in observed.items()):
                raise ValueError("Baseline destination evidence changed")
        shutil.copytree(origin, target, dirs_exist_ok=True)
        if _files(target) != expected["files"]:
            raise ValueError("Baseline copied evidence changed")

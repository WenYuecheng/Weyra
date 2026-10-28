"""Bounded user replay evidence; no model calls, history scans, or growing test bank.

AutoSkill supplies the recent-user-evidence boundary; SkillEvo supplies bounded
development/validation replay. The 2+2 window is a Weyra engineering default.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath

MAX_FILE_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 128 * 1024
MAX_FILES = 8
MAX_CONTEXT_CHARS = 8000
# Search/list inspect a directory universe that first-file preimages do not capture.
# Until that universe can be restored faithfully, retain experience without promotion.
REPLAY_TOOLS = frozenset({"skill", "read_file", "write_file", "edit_file"})


def _json(value):
    """Serialize an evidence identity deterministically."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    """Hash data without depending on dictionary insertion order."""
    return hashlib.sha256(_json(value).encode()).hexdigest()


class ReplayCapture:
    """Capture first-access file preimages under the existing file-tool lock."""

    def __init__(self, turn_id, messages, *, system_instructions=""):
        self.turn_id = turn_id
        self.files = {}
        self.written = []
        self.reason = ""
        self.size = 0
        self.system_instructions = system_instructions
        self.context = [{"role": m.role, "content": m.content}
                        for m in messages if m.role in {"user", "assistant"}
                        and isinstance(m.content, str) and not getattr(m, "tool_calls", ())]
        if len(self.context) > 8 or sum(len(m["content"]) for m in self.context) > MAX_CONTEXT_CHARS:
            self.reason = "context_exceeds_replay_limit"
            self.context = []
        if len(system_instructions) > 16000:
            self.reason = self.reason or "instructions_exceed_replay_limit"
            self.system_instructions = ""

    def tool(self, name):
        """Observe unsupported capabilities without changing the foreground turn."""
        if name not in REPLAY_TOOLS:
            self.reason = self.reason or "unsupported_tool:" + name

    def file(self, relative, raw, *, write=False):
        """Retain the first bytes or absence of a file, never its later output."""
        path = PurePosixPath(relative)
        if ".weyra" in path.parts:
            return
        if path.is_absolute() or ".." in path.parts or path.name.startswith(".env"):
            self.reason = self.reason or "unsupported_input_path"
            return
        if relative not in self.files:
            if (len(self.files) >= MAX_FILES or (raw is not None and
                    (len(raw) > MAX_FILE_BYTES or self.size + len(raw) > MAX_SNAPSHOT_BYTES))):
                self.reason = self.reason or "files_exceed_replay_limit"
                return
            try:
                content = raw.decode("utf-8") if raw is not None else None
            except UnicodeError:
                self.reason = self.reason or "non_text_input"
                return
            self.files[relative] = content
            self.size += len(raw) if raw is not None else 0
        if write and relative not in self.written:
            self.written.append(relative)

    def snapshot(self):
        """Return an independent, bounded value suitable for session persistence."""
        return json.loads(_json({"turn_id": self.turn_id, "complete": not self.reason,
                                "reason": self.reason, "files": self.files,
                                "written_paths": self.written, "context": self.context,
                                "system_instructions": self.system_instructions}))


@dataclass(frozen=True)
class ReplayWindow:
    """Immutable window: callers receive copies of case payloads, never live state."""
    version: str
    payload: str
    reason: str = ""

    @property
    def cases(self):
        """Return fresh cases so concurrent maintenance cannot mutate the window."""
        return json.loads(self.payload)

    @property
    def development(self):
        """Return development evidence, excluded from acceptance gains."""
        return [case for case in self.cases if case["split"] == "development"]

    @property
    def validation(self):
        """Return independent host-selected validation tasks."""
        return [case for case in self.cases if case["split"] == "validation"]


def freeze_window(evidence, manifest_cases):
    """Freeze <=2 development and <=2 independent validation cases before proposing.

    Original questions and their interaction IDs are never validation evidence.
    Host manifests may declare split=development (or mutate_dev); otherwise
    their cases are independent validation. No generated tests or growing pool.
    """
    snapshot = evidence.get("replay")
    reason = ""
    development, validation = [], []
    user = str(evidence.get("user", ""))
    if not isinstance(snapshot, dict):
        reason = "missing_replay_snapshot"
    elif not snapshot.get("complete"):
        reason = snapshot.get("reason") or "incomplete_replay_snapshot"
    elif not user.strip() or len(user) > MAX_CONTEXT_CHARS:
        reason = "invalid_replay_prompt"
    else:
        files = {p: text for p, text in snapshot.get("files", {}).items() if text is not None}
        written = list(snapshot.get("written_paths", []))
        context = snapshot.get("context", [])
        prompt = user
        if context:
            prompt = ("Prior conversation for reference:\n" + _json(context) +
                      "\n\nCurrent user task:\n" + user)
        development.append({
            "id": "turn-" + str(snapshot.get("turn_id", "unknown")),
            "source_window_id": snapshot.get("turn_id"), "prompt": prompt,
            "files": files, "split": "development",
            "rubric": str(evidence.get("feedback", "")),
            "system_instructions": snapshot.get("system_instructions", ""),
            "output_mode": "files" if written else "text",
            "expected_artifacts": {},
            "hard_constraints": {
                "protected_input_paths": sorted(set(files) - set(written)),
                "writable_input_paths": sorted(set(files) & set(written)),
                "required_output_paths": written, "allowed_output_paths": written,
            },
        })
    original_id = snapshot.get("turn_id") if isinstance(snapshot, dict) else None
    original_files = {p: t for p, t in (snapshot if isinstance(snapshot, dict) else {}).get("files", {}).items()
                      if t is not None}
    seen_ids, seen_tasks = set(), set()
    for source in manifest_cases:
        case = json.loads(_json(source))
        identity = _hash({"prompt": case.get("prompt", "").strip(), "files": case.get("files", {})})
        if (case.get("id") in seen_ids or identity in seen_tasks
                or (original_id and case.get("source_window_id") == original_id)
                or (case.get("prompt", "").strip() == user.strip() and case.get("files", {}) == original_files)):
            continue
        seen_ids.add(case.get("id"))
        seen_tasks.add(identity)
        split = case.get("split", "validation")
        if split not in {"development", "mutate_dev", "validation", "promotion_test"}:
            raise ValueError("Unknown replay sample split")
        role = "development" if split in {"development", "mutate_dev"} else "validation"
        destination = development if role == "development" else validation
        if len(destination) < 2:
            case["split"] = role
            destination.append(case)
        if len(development) == 2 and len(validation) == 2:
            break
    cases = development + validation
    if not validation:
        reason = reason or "insufficient_independent_validation"
    return ReplayWindow(_hash(cases), _json(cases), reason)

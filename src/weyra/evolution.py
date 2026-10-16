"""Bounded, auditable skill maintenance inspired by WikiSkill and SkillOpt."""

from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime, timezone
import threading
import uuid
from pathlib import Path
from .knowledge import atomic, sha, reject_symlinks


class EvolutionStore:
    """Apply bounded skill edits with durable preparation and monotonic versions."""

    def __init__(self, skills, state_dir=None):
        self.skills = skills
        self.root = Path(state_dir) if state_dir else skills.root / ".weyra" / "evolution"
        reject_symlinks(self.root)
        self.root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._async_locks = {}

    def _key(self, name):
        try:
            path = self.skills.load(name).path
        except (KeyError, ValueError):
            path = self.skills.root / "skills" / name / "SKILL.md"
        return sha(str(path.resolve()) + "\0" + name)

    def _dir(self, name):
        p = self.root / self._key(name)
        reject_symlinks(p)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _record(self, name, event):
        with (self._dir(name) / "provenance.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()

    def history(self, name):
        p = self._dir(name) / "provenance.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def _reject(self, name, event_id, reason, record=True, knowledge=False):
        result = {
            "status": "rejected",
            "event_id": event_id,
            "reason": reason,
            "failure_kind": "failure_to_apply",
            "task_quality": None,
        }
        try:
            if record:
                self._record(name, result)
            if knowledge:
                try:
                    version = self.skills.load(name).version
                except (KeyError, ValueError):
                    version = "unknown"
                self._save_note(
                    name,
                    {"kind": "rejected_update", "text": "failure_to_apply: " + reason[:270]},
                    event_id,
                    version,
                )
        except OSError as error:
            result["auxiliary_errors"] = [str(error)]
        return result

    def _note(self, note, event_id, version):
        if isinstance(note, str):
            note = {"kind": "observation", "text": note}
        if not isinstance(note, dict) or note.get("kind") not in {
            "observation",
            "boundary",
            "rejected_update",
        }:
            raise ValueError("Invalid knowledge kind")
        text = note.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ValueError("Knowledge text must contain 1 to 300 characters")
        return {
            "event_id": event_id,
            "kind": note["kind"],
            "text": text,
            "time": datetime.now(timezone.utc).isoformat(),
            "skill_version": version,
        }

    def knowledge(self, name):
        path = self._dir(name) / "knowledge.json"
        try:
            data = json.loads(path.read_text()) if path.exists() else []
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        records = []
        seen = set()
        for item in data:
            try:
                if isinstance(item, str):
                    record = self._note(item, "legacy:" + sha(item), "unknown")
                    record["time"] = "unknown"
                else:
                    record = self._note(
                        item, item.get("event_id", "unknown"), item.get("skill_version", "unknown")
                    )
                    record["time"] = item.get("time", "unknown")
                key = (record["event_id"], record["kind"], record["text"])
                if key not in seen:
                    records.append(record)
                    seen.add(key)
            except (ValueError, AttributeError, TypeError):
                continue
        return records[-10:]

    def _save_note(self, name, note, event_id, version):
        record = self._note(note, event_id, version)
        records = self.knowledge(name)
        key = (record["event_id"], record["kind"], record["text"])
        records = [
            item for item in records if (item["event_id"], item["kind"], item["text"]) != key
        ]
        records.append(record)
        atomic(self._dir(name) / "knowledge.json", json.dumps(records[-10:], ensure_ascii=False))

    def knowledge_prompt(self, name):
        return "\n".join(
            f"[{item['kind']}; version {item['skill_version']}] {item['text']}"
            for item in self.knowledge(name)
        )[:2000]

    def apply(
        self,
        name,
        edits,
        event_id=None,
        base_hash=None,
        description=None,
        when_to_use=None,
        reason="",
        permission_mode="default",
        authorized=False,
        knowledge_note=None,
    ):
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            try:
                snap = self.skills.load(name)
                if base_hash != snap.content_hash:
                    raise ValueError("Base hash conflict")
                if not 1 <= len(edits) <= 2:
                    raise ValueError("One or two edits required")
                spans = []
                appends = []
                for edit in edits:
                    kind = edit.get("type", edit.get("op"))
                    if kind == "replace":
                        old = edit.get("old", "")
                        new = edit.get("new", "")
                        if (
                            not old
                            or len(old) > 1000
                            or len(new) > 1000
                            or snap.body.count(old) != 1
                        ):
                            raise ValueError("Replace requires a unique bounded old range")
                        start = snap.body.index(old)
                        spans.append((start, start + len(old), new))
                    elif kind == "append":
                        text = edit.get("text", edit.get("new", ""))
                        if not text or len(text) > 1000 or text in snap.body or text in appends:
                            raise ValueError("Duplicate or invalid append")
                        appends.append(text)
                    else:
                        raise ValueError("Unsupported edit")
                spans.sort()
                if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
                    raise ValueError("Overlapping edits")
                body = snap.body
                for start, end, new in reversed(spans):
                    body = body[:start] + new + body[end:]
                if appends:
                    body += "\n" + "\n".join(appends)
                return self._publish(
                    snap, body, event_id, description, when_to_use, reason, knowledge_note
                )
            except (ValueError, KeyError, TypeError, OSError) as e:
                return self._reject(name, event_id, str(e), knowledge=True)

    def _publish(
        self,
        snap,
        body,
        event_id,
        description=None,
        when_to_use=None,
        reason="",
        knowledge_note=None,
    ):
        if not body.strip() or any(line.strip() == "---" for line in body.splitlines()):
            raise ValueError("Invalid body/frontmatter delimiter")
        description = snap.description if description is None else description
        when_to_use = snap.when_to_use if when_to_use is None else when_to_use
        if (
            body == snap.body
            and description == snap.description
            and when_to_use == snap.when_to_use
        ):
            raise ValueError("No effective change")
        if len(description) > 500 or len(when_to_use) > 500:
            raise ValueError("Metadata exceeds 500 characters")
        if knowledge_note is not None:
            self._note(knowledge_note, event_id, snap.version)
        parts = snap.version.split(".")
        if len(parts) != 3 or not all(x.isdigit() for x in parts):
            raise ValueError("Invalid semantic version")
        version = ".".join(parts[:2] + [str(int(parts[2]) + 1)])
        metadata = {
            **snap.metadata,
            "name": snap.name,
            "version": version,
            "description": description,
            "when_to_use": when_to_use,
        }
        if snap.context:
            metadata["context"] = snap.context
        raw = (
            "---\n"
            + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k, v in metadata.items())
            + "\n---\n"
            + body
        )
        folder = self._dir(snap.name)
        old = snap.path.read_text(encoding="utf-8")
        if sha(old) != snap.content_hash:
            raise ValueError("Concurrent modification")
        atomic(folder / (snap.version + ".md"), old)
        event = {
            "status": "prepared",
            "name": snap.name,
            "event_id": event_id,
            "version": version,
            "base_hash": snap.content_hash,
            "content_hash": sha(raw),
            "reason": reason,
        }
        self._record(snap.name, event)
        if sha(snap.path.read_text(encoding="utf-8")) != snap.content_hash:
            raise ValueError("Concurrent modification")
        atomic(snap.path, raw)
        result = {**event, "status": "updated", "auxiliary_errors": []}
        try:
            atomic(folder / (version + ".md"), raw)
            if knowledge_note:
                self._save_note(snap.name, knowledge_note, event_id, version)
            self._record(snap.name, result)
        except OSError as e:
            result["auxiliary_errors"].append(str(e))
        return result

    def rollback(self, name, version, event_id=None, permission_mode="default", authorized=False):
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            try:
                if not version or any(c not in "0123456789." for c in version):
                    raise ValueError("Invalid version")
                from .knowledge import parse_markdown

                metadata, body = parse_markdown((self._dir(name) / (version + ".md")).read_text())
                return self._publish(
                    self.skills.load(name),
                    body,
                    event_id,
                    metadata.get("description", ""),
                    metadata.get("when_to_use", ""),
                    "rollback " + version,
                )
            except (ValueError, KeyError, FileNotFoundError) as e:
                return self._reject(name, event_id, str(e), knowledge=True)

    def discard(self, name, event_id=None, reason=""):
        result = {"status": "discarded", "event_id": event_id or uuid.uuid4().hex, "reason": reason}
        self._record(name, result)
        return result

    def create(
        self,
        name,
        body,
        description="",
        when_to_use="",
        permission_mode="default",
        authorized=False,
        event_id=None,
    ):
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            if (
                not name
                or name in (".", "..")
                or "/" in name
                or "\\" in name
                or not body.strip()
                or any(x.strip() == "---" for x in body.splitlines())
                or len(description) > 500
                or len(when_to_use) > 500
            ):
                return self._reject(name, event_id, "Invalid new skill")
            path = self.skills.root / "skills" / name / "SKILL.md"
            try:
                reject_symlinks(path)
            except ValueError as error:
                return self._reject(name, event_id, str(error))
            if path.exists() or name in self.skills.capture_catalog():
                return self._reject(name, event_id, "Skill exists")
            raw = (
                "---\n"
                + "\n".join(
                    k + ": " + json.dumps(v, ensure_ascii=False)
                    for k, v in dict(
                        name=name, version="1.0.0", description=description, when_to_use=when_to_use
                    ).items()
                )
                + "\n---\n"
                + body
            )
            event = {
                "status": "prepared",
                "event_id": event_id,
                "name": name,
                "version": "1.0.0",
                "content_hash": sha(raw),
            }
            atomic(self._dir(name) / "1.0.0.md", raw)
            self._record(name, event)
            atomic(path, raw)
            result = {**event, "status": "updated", "auxiliary_errors": []}
            try:
                self._record(name, result)
            except OSError as e:
                result["auxiliary_errors"].append(str(e))
            return result

    async def maintain(
        self,
        window,
        model_call,
        permission_mode="default",
        authorized=False,
        event_id=None,
        evidence=None,
        target_snapshot=None,
    ):
        """Persist source evidence, then apply a proposal against program-pinned state."""
        event_id = event_id or uuid.uuid4().hex
        if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
            return {
                "status": "rejected",
                "event_id": event_id,
                "reason": "Permission required",
                "failure_kind": "failure_to_apply",
                "task_quality": None,
            }
        # Capture all available bases before awaiting the model. A proposal may select a
        # catalog entry, but cannot supply the comparison hash used for publication.
        catalog = self.skills.capture_catalog() if target_snapshot is None else None
        frozen_window = json.loads(json.dumps(window, ensure_ascii=False))
        frozen_evidence = json.loads(
            json.dumps(evidence if evidence is not None else window, ensure_ascii=False)
        )
        source_record = {
            "event_id": event_id,
            "evidence": frozen_evidence,
            "target": None
            if target_snapshot is None
            else {
                "name": target_snapshot.name,
                "path": str(target_snapshot.path),
                "version": target_snapshot.version,
                "content_hash": target_snapshot.content_hash,
            },
        }
        evidence_path = self.root / "evidence.jsonl"
        try:
            reject_symlinks(evidence_path)
            with self._lock, evidence_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(source_record, ensure_ascii=False) + "\n")
                output.flush()

                os.fsync(output.fileno())
        except (OSError, ValueError) as error:
            return {
                "status": "rejected",
                "event_id": event_id,
                "reason": str(error),
                "failure_kind": "failure_to_apply",
                "task_quality": None,
            }
        proposal = await model_call(frozen_window)
        if not proposal:
            proposal = {"action": "discard"}
        if not isinstance(proposal, dict):
            return self._reject("_maintenance", event_id, "Invalid model proposal")
        proposal = dict(proposal)
        action = proposal.pop("action", "apply")
        name = proposal.pop("name", None)
        for field in ("event_id", "authorized", "permission_mode", "base_hash", "content_hash"):
            proposal.pop(field, None)
        if action == "discard":
            return self.discard(
                name or "_maintenance", event_id=event_id, reason=proposal.get("reason", "")
            )
        if not isinstance(name, str) or not name:
            return self._reject("_maintenance", event_id, "A skill name is required")
        if action == "apply":
            if target_snapshot is not None and name != target_snapshot.name:
                return self._reject(name, event_id, "Proposal target differs from pinned skill")
            target = target_snapshot if target_snapshot is not None else catalog.get(name)
            if target is None:
                return self._reject(name, event_id, "No program-pinned target")
            proposal["base_hash"] = target.content_hash
            name = target.name
        elif action == "create":
            if name in self.skills.capture_catalog():
                return self._reject(name, event_id, "Create requires a new skill")
            proposal.pop("reason", None)
            proposal.pop("knowledge_note", None)
        else:
            return self._reject(name, event_id, "Unknown maintenance action")
        proposal.update(
            name=name, event_id=event_id, permission_mode=permission_mode, authorized=authorized
        )
        async with self._async_locks.setdefault(name, asyncio.Lock()):
            try:
                if action == "create":
                    return self.create(**proposal)
                return self.apply(**proposal)
            except (TypeError, ValueError) as error:
                return self._reject(name, event_id, str(error))

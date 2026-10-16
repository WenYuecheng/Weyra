"""Project-local Markdown memory and immutable skill catalog snapshots."""

from __future__ import annotations
import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".weyra-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def reject_symlinks(path):
    for component in [Path(path), *Path(path).parents]:
        if component.is_symlink():
            raise ValueError("Symlink paths are not allowed")


def tokens(text):
    text = text.casefold()
    return (
        re.findall(r"[^\W_]+", text)
        + re.findall(r"[\u3400-\u9fff]", text)
        + [
            text[i : i + 2]
            for i in range(len(text) - 1)
            if all("\u3400" <= c <= "\u9fff" for c in text[i : i + 2])
        ]
    )


def rank(query, items, limit):
    q = set(tokens(query))
    docs = [Counter(tokens(text)) for _, text in items]
    n = len(docs)
    avg = sum(sum(d.values()) for d in docs) / max(n, 1) or 1
    scores = []
    for (item, _), d in zip(items, docs):
        score = 0
        for term in q:
            freq = d[term]
            df = sum(term in x for x in docs)
            score += (
                math.log(1 + (n - df + 0.5) / (df + 0.5))
                * freq
                * 2.2
                / (freq + 1.2 * (0.25 + 0.75 * sum(d.values()) / avg))
                if freq
                else 0
            )
        if score:
            scores.append((score, item))
    return [item for _, item in sorted(scores, key=lambda x: (-x[0], str(x[1])))[:limit]]


def parse_markdown(text):
    metadata = {}
    body = text
    if text.startswith("---\n"):
        parts = text.split("\n---", 1)
        if len(parts) != 2:
            raise ValueError("Unclosed frontmatter")
        for line in parts[0][4:].splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                key, value = line.split(":", 1)
                value = value.strip()
                if value.startswith(chr(34)):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = value.strip(chr(34))
                else:
                    value = value.strip(chr(39))
                metadata[key.strip()] = value
        body = parts[1].lstrip("\r\n")
    return metadata, body


@dataclass(frozen=True)
class SkillSnapshot:
    name: str
    path: Path
    version: str
    description: str
    when_to_use: str
    body: str
    content_hash: str
    resources: Mapping[str, str]
    context: str = ""
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


class MemoryStore:
    """Maintain a deterministic Markdown index within one project state directory."""

    def __init__(self, project_path, state_dir=None):
        self.root = (
            Path(state_dir) if state_dir else Path(project_path).resolve() / ".weyra" / "memory"
        )
        reject_symlinks(self.root)
        self.root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._refresh()

    def _path(self, name):
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError("Invalid memory name")
        path = self.root / (name + ".md")
        reject_symlinks(path)
        return path

    def _refresh(self):
        self.records = {}
        reject_symlinks(self.root / "manifest.json")
        try:
            old = (
                json.loads((self.root / "manifest.json").read_text())
                if (self.root / "manifest.json").exists()
                else {}
            )
        except (ValueError, OSError):
            old = {}
        if not isinstance(old, dict):
            old = {}
        for p in sorted(self.root.glob("*.md")):
            reject_symlinks(p)
            content = p.read_text(encoding="utf-8")
            h = sha(content)
            prior = old.get(p.stem, {})
            if not isinstance(prior, dict):
                prior = {}
            previous_version = prior.get("version", 0)
            if type(previous_version) is not int or previous_version < 0:
                previous_version = 0
            self.records[p.stem] = {
                "id": p.stem,
                "content": content,
                "content_hash": h,
                "version": previous_version + (h != prior.get("content_hash")),
            }
        atomic(
            self.root / "manifest.json",
            json.dumps(self.records, ensure_ascii=False, sort_keys=True),
        )

    def create(self, name, content):
        if self._path(name).exists():
            raise ValueError("Memory already exists")
        return self.update(name, content)

    def update(self, name, content):
        atomic(self._path(name), content)
        self._refresh()
        return self.records[name].copy()

    def delete(self, name):
        self._path(name).unlink()
        self._refresh()

    def get(self, name):
        return self.records[name].copy()

    def list(self):
        return [v.copy() for v in self.records.values()]

    def search(self, query, limit=5):
        return [
            self.get(k)
            for k in rank(
                query, [(k, k + " " + v["content"]) for k, v in self.records.items()], limit
            )
        ]

    async def recall(self, query, selector=None, timeout=8):
        selected = self.search(query)
        if selector:
            task = asyncio.create_task(selector(query, self.list()))
            done, _ = await asyncio.wait({task}, timeout=min(max(timeout, 0), 8))
            if not done:
                task.cancel()
                task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
                return ""
            ids = task.result()
            ids = ids if isinstance(ids, (list, tuple)) else []
            selected = [
                self.get(k)
                for k in dict.fromkeys(k for k in ids if isinstance(k, str) and k in self.records)
            ]
        result = []
        total = 0
        for record in selected[:5]:
            # JSON encoding prevents identifiers from breaking the wrapper.
            opening = "<memory id=" + json.dumps(record["id"], ensure_ascii=False) + ">"
            closing = "</memory>"
            budget = 4096 - len((opening + closing).encode())
            if budget < 0:
                continue
            text = opening + record["content"].encode()[:budget].decode("utf-8", "ignore") + closing
            if total + len(text.encode()) + 1 > 60 * 1024:
                break
            result.append(text)
            total += len(text.encode()) + 1
        return "\n".join(result)


class SkillStore:
    """Capture skill bodies and resource hashes without changing workspace files."""

    def __init__(self, root, skill_dirs=None):
        self.root = Path(root).resolve()
        self.skill_dirs = [self.root / "skills"] + [Path(x).resolve() for x in (skill_dirs or [])]

    def _snapshot(self, p):
        reject_symlinks(p)
        raw = p.read_text(encoding="utf-8")
        m, body = parse_markdown(raw)
        resources = {
            str(f.relative_to(p.parent)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(p.parent.rglob("*"))
            if f.is_file() and f != p and not f.is_symlink()
        }
        return SkillSnapshot(
            m.get("name", p.parent.name),
            p.resolve(),
            m.get("version", "1.0.0"),
            m.get("description", ""),
            m.get("when_to_use", m.get("when-to-use", "")),
            body,
            sha(raw),
            MappingProxyType(resources),
            m.get("context", ""),
            MappingProxyType(m),
        )

    def capture_catalog(self):
        """Return immutable snapshots, rejecting ambiguous duplicate skill names."""
        result = {}
        for directory in self.skill_dirs:
            for p in sorted(directory.glob("*/SKILL.md")):
                snapshot = self._snapshot(p)
                if snapshot.name in result:
                    raise ValueError("Duplicate skill name")
                result[snapshot.name] = snapshot
        return MappingProxyType(result)

    def load(self, name, catalog=None):
        return (catalog if catalog is not None else self.capture_catalog())[name]

    def search(self, query, limit=5, catalog=None):
        catalog = catalog if catalog is not None else self.capture_catalog()
        return [
            catalog[k]
            for k in rank(
                query,
                [
                    (k, s.name + " " + s.description + " " + s.when_to_use + " " + s.body)
                    for k, s in catalog.items()
                ],
                limit,
            )
        ]

    def read_resource(self, snapshot, relative_path):
        """Read a pinned resource only if its location and bytes still match."""
        reject_symlinks(snapshot.path.parent / relative_path)
        p = (snapshot.path.parent / relative_path).resolve()
        if (
            not p.is_relative_to(snapshot.path.parent.resolve())
            or relative_path not in snapshot.resources
        ):
            raise ValueError("Resource outside snapshot")
        raw = p.read_bytes()
        if hashlib.sha256(raw).hexdigest() != snapshot.resources[relative_path]:
            raise ValueError("Resource drift detected")
        return raw.decode("utf-8")

"""Sourced project Markdown memory and bounded recall."""

import asyncio
import json
from pathlib import Path
from .frontmatter import atomic, rank, reject_symlinks, sha


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
        self._background = set()
        self._closed = False

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
        self._ensure_fresh()
        return self.records[name].copy()

    def list(self):
        self._ensure_fresh()
        return [v.copy() for v in self.records.values()]

    def _ensure_fresh(self):
        current = {}
        for path in sorted(self.root.glob("*.md")):
            reject_symlinks(path)
            current[path.stem] = sha(path.read_text(encoding="utf-8"))
        if current != {key: value["content_hash"] for key, value in self.records.items()}:
            self._refresh()

    def search(self, query, limit=5):
        self._ensure_fresh()
        return [
            self.get(k)
            for k in rank(
                query, [(k, k + " " + v["content"]) for k, v in self.records.items()], limit
            )
        ]

    def _selector_done(self, task):
        self._background.discard(task)
        if not task.cancelled():
            # Timed-out work has no waiting caller to retrieve its exception.
            task.exception()

    @staticmethod
    async def _drain(tasks):
        """Let paid request settlement finish even if the owning waiter is cancelled again."""
        pending = asyncio.gather(*tasks, return_exceptions=True)
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return pending.result()

    def cancel_pending(self):
        """Immediately request cancellation; close() also waits for durable settlement."""
        tasks = tuple(task for task in self._background if not task.done())
        for task in tasks:
            task.cancel()
        return tasks

    async def close(self):
        """Drain every selector before the owning session writer or provider closes.

        Normal recall deadlines never wait for an uncooperative backend. Such
        tasks remain owned here until their adapter's bounded request finishes,
        or shutdown cancels and drains them. Their late result is never injected.
        """
        self._closed = True
        await self._drain(self.cancel_pending())

    async def recall(self, query, selector=None, timeout=8):
        return "\n".join(
            item["wrapped"] for item in await self.recall_entries(query, selector, timeout)
        )

    async def recall_entries(self, query, selector=None, timeout=8):
        """Pin selection to the versions offered to the selector, with complete wrappers."""
        if self._closed:
            raise RuntimeError("MemoryStore is closed")
        selected = self.search(query)
        snapshot = {record["id"]: record for record in self.list()}
        if selector:
            task = asyncio.create_task(selector(query, list(snapshot.values())))
            self._background.add(task)
            task.add_done_callback(self._selector_done)
            try:
                done, _ = await asyncio.wait({task}, timeout=min(max(timeout, 0), 8))
            except asyncio.CancelledError:
                task.cancel()
                await self._drain((task,))
                raise
            if not done:
                task.cancel()
                return []
            ids = task.result()
            if self._closed:
                return []
            ids = ids if isinstance(ids, (list, tuple)) else []
            selected = [
                snapshot[k].copy()
                for k in dict.fromkeys(k for k in ids if isinstance(k, str) and k in snapshot)
            ]
        result = []
        total = 0
        for record in selected[:5]:
            # JSON encoding prevents identifiers from breaking the wrapper.
            opening = (
                "<memory id="
                + json.dumps(record["id"], ensure_ascii=False)
                + " version="
                + json.dumps(record["content_hash"])
                + ">"
            )
            closing = "</memory>"
            budget = 4096 - len((opening + closing).encode())
            if budget < 0:
                continue
            text = opening + record["content"].encode()[:budget].decode("utf-8", "ignore") + closing
            if total + len(text.encode()) + 1 > 60 * 1024:
                break
            result.append({**record, "wrapped": text})
            total += len(text.encode()) + 1
        return result

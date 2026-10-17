"""Version-pinned project and user skills."""
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from .frontmatter import parse_markdown, rank, reject_symlinks, sha

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


class SkillStore:
    """Capture skill bodies and resource hashes without changing workspace files."""

    def __init__(self, root, skill_dirs=None):
        self.root = Path(root).resolve()
        self.project_skills = self.root / '.weyra' / 'skills'
        self.skill_dirs = [self.project_skills]
        self.skill_dirs += [Path(x).resolve() for x in skill_dirs] if skill_dirs is not None else [Path.home() / '.weyra' / 'skills']
        # Read old Weyra locations without moving or overwriting user files.
        self.skill_dirs.append(self.root / 'skills')

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
            local_names = set()
            for p in sorted(directory.glob("*/SKILL.md")):
                snapshot = self._snapshot(p)
                if snapshot.name in local_names:
                    raise ValueError("Duplicate skill name")
                local_names.add(snapshot.name)
                result.setdefault(snapshot.name, snapshot)
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

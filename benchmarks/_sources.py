"""Content hashes for current runtime, benchmark implementations and fixture inputs."""
import hashlib
from pathlib import Path


def source_manifest(project):
    project = Path(project).resolve()
    paths = set()
    for folder in ("weyra", "benchmarks", "data"):
        for path in (project / folder).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json", ".tcss", ".txt"} and "__pycache__" not in path.parts:
                paths.add(path)
    return {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}

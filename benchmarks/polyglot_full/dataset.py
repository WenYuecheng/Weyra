"""Official Exercism metadata projection, keeping gold out of agent workspaces."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from .sandbox import safe_path


@dataclass(frozen=True)
class Task:
    id: str
    language: str
    files: dict
    verifier_files: dict
    solutions: dict
    editable: tuple
    command: tuple
    instruction: str

    def metadata(self):
        return {
            "id": self.id,
            "language": self.language,
            "editable": list(self.editable),
            "command": list(self.command),
            "agent_files_sha256": {
                p: hashlib.sha256(s.encode()).hexdigest() for p, s in self.files.items()
            },
            "verifier_sha256": {
                p: hashlib.sha256(s.encode()).hexdigest() for p, s in self.verifier_files.items()
            },
            "reference_sha256": {
                p: hashlib.sha256(s.encode()).hexdigest() for p, s in self.solutions.items()
            },
        }


def rank(task):
    return hashlib.sha256(("weyra-polyglot-40-v1:" + task.id).encode()).hexdigest()


def discover(root):
    tasks = []
    for language in ("python", "javascript"):
        for config in sorted(
            (Path(root) / language / "exercises/practice").glob("*/.meta/config.json")
        ):
            folder = config.parent.parent
            meta = json.loads(config.read_text())["files"]
            editable = tuple(safe_path(s) for s in meta["solution"])
            tests = tuple(safe_path(s) for s in meta["test"])
            examples = tuple(safe_path(s) for s in meta.get("example", []))
            if len(editable) != len(examples):
                continue
            instruction = "\n\n".join(
                p.read_text()
                for name in ("introduction.md", "instructions.md", "instructions.append.md")
                if (p := folder / ".docs" / name).is_file()
            )
            files = {s: (folder / s).read_text() for s in editable}
            files["INSTRUCTIONS.md"] = instruction
            verifier = {s: (folder / s).read_text() for s in (*editable, *tests)}
            # Exact upstream npm-test.sh enablement: xtest -> test only.
            if language == "javascript":
                for s in tests:
                    import re

                    verifier[s] = re.sub(r"\bxtest\(", "test(", verifier[s])
                for s in ("package.json", "babel.config.js"):
                    if (folder / s).exists():
                        verifier[s] = (folder / s).read_text()
                        files[s] = verifier[s]
                cmd = (
                    "jest",
                    "--runInBand",
                    "--no-cache",
                    "--ci",
                    "--json",
                    "--outputFile=/tmp/jest-result.json",
                )
            else:
                cmd = (
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--junitxml=/tmp/junit.xml",
                    *tests,
                )
            solutions = {
                target: (folder / source).read_text() for target, source in zip(editable, examples)
            }
            tasks.append(
                Task(
                    f"{language}/{folder.name}",
                    language,
                    files,
                    verifier,
                    solutions,
                    editable,
                    cmd,
                    instruction,
                )
            )
    return sorted(tasks, key=rank)

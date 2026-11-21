"""Fixed upstream tasks with a separate private verifier view."""

from dataclasses import replace
from pathlib import Path
import json
import re
import tomllib

from .dataset import Task, discover
from .sandbox import safe_path

LANGUAGES = ("python", "javascript", "go", "rust", "cpp", "java")
UPSTREAM = "7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f"
RUST_REFERENCE_DEPS = {
    "alphametics": {"itertools": "0.13", "permutohedron": "0.2"},
    "decimal": {"num-bigint": "0.4", "num-traits": "0.2"},
    "grep": {"thiserror": "1"},
    "pig-latin": {"regex": "1"},
    "poker": {"counter": "0.5"},
    "robot-name": {"rand": "0.8"},
}


def rust_manifest(text, slug):
    existing = tomllib.loads(text).get("dependencies", {})
    additions = [
        f'{name} = "{version}"'
        for name, version in RUST_REFERENCE_DEPS.get(slug, {}).items()
        if name not in existing
    ]
    if not additions:
        return text
    entries = "\n".join(additions)
    if "[dependencies]" in text:
        return text.replace("[dependencies]", f"[dependencies]\n{entries}", 1)
    return f"{text.rstrip()}\n\n[dependencies]\n{entries}\n"


def discover_all(root, *, full_coverage=False):
    root = Path(root)
    for task in sorted(discover(root), key=lambda task: (LANGUAGES.index(task.language), task.id)):
        if task.language == "javascript":
            verifier = {
                p: re.sub(r"\b(?:xit|(?:test|it)\.skip)\s*\(", "test(", value)
                if p.endswith((".spec.js", ".test.js")) and p not in task.editable
                else value
                for p, value in task.verifier_files.items()
            }
            data = root / "javascript/exercises/practice" / task.id.split("/")[1] / "data"
            for path in sorted(data.glob("*.txt")):
                verifier[f"data/{path.name}"] = path.read_text()
            task = replace(task, verifier_files=verifier)
        yield task
    for language in LANGUAGES[2:]:
        for config in sorted((root / language).glob("exercises/practice/*/.meta/config.json")):
            folder = config.parent.parent
            meta = json.loads(config.read_text())["files"]
            editable = tuple(
                safe_path(path)
                for path in meta["solution"]
                if path.endswith((".go", ".rs", ".h", ".cpp", ".java"))
            )
            reference = [safe_path(path) for path in meta["example"]]
            if language in {"go", "rust"}:
                solutions = {
                    path: (folder / ref).read_text() for path, ref in zip(editable, reference)
                }
            else:
                by_name = {Path(ref).name: (folder / ref).read_text() for ref in reference}
                if language == "cpp":
                    by_name = {Path(ref).suffix: (folder / ref).read_text() for ref in reference}
                    solutions = {
                        path: by_name[Path(path).suffix]
                        for path in editable
                        if Path(path).suffix in by_name
                    }
                else:
                    solutions = {
                        path: by_name[Path(path).name]
                        for path in editable
                        if Path(path).name in by_name
                    }
                    solutions.update(
                        {f"src/main/java/{name}": value for name, value in by_name.items()}
                    )
            instruction = "\n\n".join(
                path.read_text()
                for name in ("introduction.md", "instructions.md", "instructions.append.md")
                if (path := folder / ".docs" / name).is_file()
            )
            files = {p: (folder / p).read_text() for p in editable}
            files["INSTRUCTIONS.md"] = instruction
            if language in {"go", "rust"}:
                build = "go.mod" if language == "go" else "Cargo.toml"
                files[build] = (folder / build).read_text()
                if language == "go":
                    files.update(
                        {
                            path.name: path.read_text()
                            for path in folder.glob("*.go")
                            if not path.name.endswith("_test.go") and path.name not in editable
                        }
                    )
                else:
                    files[build] = rust_manifest(files[build], folder.name)
            fixtures = dict(files)
            for path in sorted(folder.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(folder).as_posix()
                if rel.startswith(".") or rel in files:
                    continue
                if language == "go":
                    keep = rel.endswith("_test.go")
                elif language == "cpp":
                    keep = rel.startswith("test/") or rel in meta["test"]
                elif language == "java":
                    keep = rel.startswith(("src/test/", "src/main/"))
                else:
                    keep = rel.startswith(("tests/", "src/", "macros/", "macros-tests/", "test/"))
                if not keep or path.suffix not in {
                    ".go",
                    ".cpp",
                    ".h",
                    ".hpp",
                    ".rs",
                    ".toml",
                    ".java",
                    ".txt",
                    ".json",
                }:
                    continue
                value = path.read_text()
                if language == "rust":
                    value = re.sub(r"(?m)^[ \t]*#\[ignore(?:[^\]]*)\][ \t]*\n", "", value)
                if language == "java":
                    value = re.sub(r"(?m)^[ \t]*@Disabled(?:\([^\n]*\))?[ \t]*\n", "", value)
                fixtures[rel] = value
            task_id = f"{language}/{folder.name}"
            if full_coverage and task_id == "go/counter":
                editable = ("counter_test.go",)
                files.update({path: fixtures[path] for path in editable})
                solutions = {editable[0]: (folder / reference[0]).read_text()}
                instruction += (
                    "\n\nWrite your Go tests in counter_test.go against makeCounter(). "
                    "The suite must accept the correct counter and reject the defective counters. "
                    "The grader selects the implementation via COUNTER_IMPL."
                )
                files["INSTRUCTIONS.md"] = instruction
            elif full_coverage and task_id == "rust/robot-name":
                solutions = {
                    path: text.replace(
                        "use rand::{thread_rng, Rng};", "use rand::{seq::SliceRandom, thread_rng};"
                    )
                    .replace("thread_rng().choose(LETTERS)", "LETTERS.choose(&mut thread_rng())")
                    .replace("thread_rng().choose(NUMBERS)", "NUMBERS.choose(&mut thread_rng())")
                    for path, text in solutions.items()
                }
            yield Task(task_id, language, files, fixtures, solutions, editable, (), instruction)

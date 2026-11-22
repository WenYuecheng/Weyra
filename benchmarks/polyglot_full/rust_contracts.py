"""Project original Rust doc examples into private, stable test fixtures."""

import json
import re
import tomllib

DIRECTORY = "_weyra_rust_contracts"
MANIFEST = f"{DIRECTORY}/manifest.json"
TEST_TARGETS = "_weyra_rust_test_targets.json"
DOC_LINE = re.compile(r"^[ \t]*(///(?!/)|//!)(?: )?(.*)$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
UNSUPPORTED_DOC = re.compile(r"/\*(?:\*(?!\*)|!)|#\s*!?\[\s*doc\s*=")
SUPPORTED_FENCE_INFO = {"", "rust", "compile_fail", "no_run", "should_panic"}

POSITIVE_CONTROLS = {
    "rust/react": """\
extern crate react;

fn main() {
    let mut reactor = react::Reactor::new();
    let input: react::InputCellId = reactor.create_input(111);
    let _: react::ComputeCellId =
        reactor.create_compute(&[react::CellId::Input(input)], |_| 222).unwrap();
}
""",
    "rust/doubly-linked-list": """\
extern crate doubly_linked_list;

fn main() {
    let _ = doubly_linked_list::LinkedList::<i32>::new();
}
""",
}


def fenced_examples(path, source):
    """Yield complete fences from contiguous outer or inner line docs."""
    if UNSUPPORTED_DOC.search(source):
        raise ValueError(f"Unsupported Rust doc comment or attribute in {path}")
    fence = None
    doc_kind = None
    for line in source.splitlines():
        doc = DOC_LINE.fullmatch(line)
        if fence is not None and (doc is None or doc[1] != doc_kind):
            raise ValueError(f"Unclosed Rust doc fence in {path}")
        if doc is None:
            continue
        doc_kind, content = doc.groups()
        marker = FENCE.fullmatch(content)
        if fence is None and marker:
            delimiter, info = marker.groups()
            if info.strip() not in SUPPORTED_FENCE_INFO:
                raise ValueError(f"Unsupported Rust doc fence in {path}: {info}")
            fence = [content]
        elif fence is not None:
            fence.append(content)
            if marker and re.fullmatch(rf"{delimiter[0]}{{{len(delimiter)},}}\s*", content.strip()):
                if not any(line.strip() for line in fence[1:-1]):
                    raise ValueError(f"Empty Rust doc fence in {path}")
                yield fence
                fence = None
    if fence:
        raise ValueError(f"Unclosed Rust doc fence in {path}")


def protected_fixtures(task_id, originals):
    cases = {}
    harness = []
    for path, source in sorted(originals.items()):
        if path.endswith(".rs"):
            for ordinal, example in enumerate(fenced_examples(path, source), 1):
                item = f"contract_{len(cases) + 1}"
                cases[item] = f"doctest:{path}:{ordinal}"
                harness.extend(f"/// {line}" for line in example)
                harness.append(f"pub struct {item};\n")
    if not cases:
        return {}
    if task_id not in POSITIVE_CONTROLS:
        raise ValueError(f"Missing Rust doctest positive control for {task_id}")
    cargo = tomllib.loads(originals["Cargo.toml"])
    package = cargo["package"]
    crate = cargo.get("lib", {}).get("name", package["name"].replace("-", "_"))
    manifest = {
        "version": 1,
        "crate": crate,
        "edition": package.get("edition", "2015"),
        "cases": cases,
    }
    return {
        MANIFEST: json.dumps(manifest, indent=2) + "\n",
        f"{DIRECTORY}/harness.rs": "\n".join(harness),
        f"{DIRECTORY}/positive.rs": POSITIVE_CONTROLS[task_id],
    }

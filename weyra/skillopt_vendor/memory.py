"""SkillOpt-Sleep — skill/memory document manipulation.

Ported verbatim from `microsoft/SkillOpt` (`skillopt_sleep/memory.py`, MIT License)
at commit `dc0b5e654cafa1073a2819086151c1e0d13012f0`.

Applies bounded EditRecords to a skill (SKILL.md body) or memory document.
All edits live inside a protected, clearly-marked region so the sleep cycle
never clobbers the user's hand-written content.
"""
from __future__ import annotations

import re
from typing import List, Tuple

from .types import EditRecord

LEARNED_START = "<!-- SKILLOPT-SLEEP:LEARNED START -->"
LEARNED_END = "<!-- SKILLOPT-SLEEP:LEARNED END -->"
_BANNER = (
    "_This block is maintained by SkillOpt-Sleep. Edits here are proposed "
    "offline, validated against your past tasks, and adopted only after you "
    "approve them. Hand-edits outside this block are never touched._"
)


def extract_learned(doc: str) -> str:
    s = doc.find(LEARNED_START)
    e = doc.find(LEARNED_END)
    if s == -1 or e == -1:
        return ""
    return doc[s + len(LEARNED_START):e].strip()


def _strip_learned(doc: str) -> str:
    while True:
        s = doc.find(LEARNED_START)
        if s == -1:
            break
        e = doc.find(LEARNED_END, s)
        if e == -1:
            doc = doc[:s] + doc[s + len(LEARNED_START):]
            break
        doc = doc[:s] + doc[e + len(LEARNED_END):]
    while "\n\n\n" in doc:
        doc = doc.replace("\n\n\n", "\n\n")
    return doc.rstrip()


def _learned_item(text: str) -> str:
    """Normalize one learned item to the single bullet line it is stored as."""
    item = " ".join(str(text or "").split())
    if item.startswith("- "):
        item = item[2:].lstrip()
    return item


def set_learned(doc: str, learned_lines: List[str]) -> str:
    """Replace the protected learned region with the given bullet lines."""
    base = _strip_learned(doc)
    items = (_learned_item(ln) for ln in learned_lines)
    body = "\n".join(f"- {item}" for item in items if item)
    block = (
        f"\n\n{LEARNED_START}\n"
        f"## Learned preferences & procedures\n\n{_BANNER}\n\n{body}\n"
        f"{LEARNED_END}\n"
    )
    return (base + block).lstrip("\n")


def current_learned_lines(doc: str) -> List[str]:
    """Return the learned items, one per ``- `` bullet."""
    inner = extract_learned(doc)
    lines: List[str] = []
    for ln in inner.splitlines():
        ln = ln.strip()
        if ln.startswith("- "):
            lines.append(_learned_item(ln))
        elif ln and lines:
            lines[-1] = _learned_item(f"{lines[-1]} {ln}")
    return lines


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def apply_edits(doc: str, edits: List[EditRecord]) -> Tuple[str, List[EditRecord]]:
    """Apply add/delete/replace edits to the protected learned region."""
    new_doc, applied, _unmatched = apply_edits_detailed(doc, edits)
    return new_doc, applied


def apply_edits_detailed(
    doc: str, edits: List[EditRecord]
) -> Tuple[str, List[EditRecord], List[EditRecord]]:
    """Apply edits and also report the ones that matched nothing."""
    lines = current_learned_lines(doc)
    norm_set = {_norm(line) for line in lines}
    applied: List[EditRecord] = []
    unmatched: List[EditRecord] = []

    for e in edits:
        op = (e.op or "add").lower()
        if op == "add":
            item = _learned_item(e.content)
            if not item or _norm(item) in norm_set:
                unmatched.append(e)
                continue
            lines.append(item)
            norm_set.add(_norm(item))
            applied.append(e)
        elif op == "delete":
            anchor = _norm(e.anchor or e.content)
            if not anchor:
                unmatched.append(e)
                continue
            keep = [line for line in lines if anchor not in _norm(line)]
            if len(keep) != len(lines):
                lines = keep
                norm_set = {_norm(line) for line in lines}
                applied.append(e)
            else:
                unmatched.append(e)
        elif op == "replace":
            anchor = _norm(e.anchor)
            replacement = _learned_item(e.content)
            kept = {_norm(line) for line in lines if not (anchor and anchor in _norm(line))}
            placed = _norm(replacement) in kept
            new_lines = []
            changed = False
            for line in lines:
                if anchor and anchor in _norm(line):
                    if placed:
                        changed = True
                        continue
                    new_lines.append(replacement)
                    placed = True
                    changed = changed or replacement != line
                else:
                    new_lines.append(line)
            if changed:
                lines = new_lines
                norm_set = {_norm(line) for line in lines}
                applied.append(e)
            else:
                unmatched.append(e)
        else:
            unmatched.append(e)

    return set_learned(doc, lines), applied, unmatched


def ensure_skill_scaffold(doc: str, *, name: str, description: str) -> str:
    """Ensure a SKILL.md has YAML frontmatter so local agents load it."""
    if doc.lstrip().startswith("---"):
        return doc
    fm = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {name}\n\n"
        "Preferences and procedures learned from your past local agent sessions.\n"
    )
    return fm + doc

"""Pure parser and exact, forward line matcher for Weyra's supported patch subset.

The framing follows the Codex-style Begin/End Patch notation. Matching is exact
(after CRLF normalization); this module intentionally makes no fuzzy-match or
full upstream compatibility guarantee. Filesystem and permission work lives in
patch_tool, so parsing and simulation cannot write files.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Hunk:
    old: tuple[str, ...]
    new: tuple[str, ...]
    anchor: str | None = None
    eof: bool = False


@dataclass(frozen=True)
class PatchOperation:
    kind: str
    path: str
    content: str = ""
    hunks: tuple[Hunk, ...] = ()
    move_to: str | None = None


def _path(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("Patch paths must be nonempty and contain no NUL")
    return value


def parse_patch(text: str) -> tuple[PatchOperation, ...]:
    """Parse a complete patch, rejecting unknown directives or trailing content."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("Expected Begin Patch and End Patch framing")
    operations = []
    i = 1
    while i < len(lines) - 1:
        header = lines[i]
        kind = next(
            (
                kind
                for kind in ("Add", "Delete", "Update")
                if header.startswith(f"*** {kind} File: ")
            ),
            None,
        )
        if kind is None:
            raise ValueError("Expected Add, Delete, or Update File directive")
        path = _path(header[len(f"*** {kind} File: ") :])
        i += 1
        if kind == "Delete":
            operations.append(PatchOperation("delete", path))
            continue
        if kind == "Add":
            content = []
            while i < len(lines) - 1 and lines[i].startswith("+"):
                content.append(lines[i][1:])
                i += 1
            operations.append(
                PatchOperation("add", path, "\n".join(content) + ("\n" if content else ""))
            )
            continue
        move_to = None
        if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
            move_to = _path(lines[i][len("*** Move to: ") :])
            i += 1
        hunks = []
        while i < len(lines) - 1 and (lines[i] == "@@" or lines[i].startswith("@@ ")):
            anchor = lines[i][3:] if lines[i].startswith("@@ ") else None
            i += 1
            old, new = [], []
            count = 0
            while i < len(lines) - 1 and lines[i][:1] in {" ", "+", "-"}:
                prefix, body = lines[i][0], lines[i][1:]
                if prefix != "+":
                    old.append(body)
                if prefix != "-":
                    new.append(body)
                count += 1
                i += 1
            if not count:
                raise ValueError("An update hunk must contain lines")
            eof = i < len(lines) - 1 and lines[i] == "*** End of File"
            if eof:
                i += 1
            hunks.append(Hunk(tuple(old), tuple(new), anchor, eof))
        if not hunks and move_to is None:
            raise ValueError("An update needs hunks or a move destination")
        operations.append(PatchOperation("update", path, hunks=tuple(hunks), move_to=move_to))
    if not operations:
        raise ValueError("A patch must contain at least one operation")
    return tuple(operations)


def apply_hunks(text: str, hunks: tuple[Hunk, ...]) -> str:
    """Apply exact hunks in forward order, retaining the source newline convention."""
    if not hunks:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n")
    final_newline = normalized.endswith("\n") or not normalized
    lines = normalized.split("\n") if normalized else []
    if normalized.endswith("\n"):
        lines.pop()
    cursor = 0
    output = []
    for hunk in hunks:
        start = cursor
        if hunk.anchor is not None:
            try:
                start = lines.index(hunk.anchor, start) + 1
            except ValueError:
                raise ValueError("Patch anchor was not found") from None
        if not hunk.old:
            position = len(lines)
        else:
            positions = (
                (len(lines) - len(hunk.old),)
                if hunk.eof
                else range(start, len(lines) - len(hunk.old) + 1)
            )
            position = next(
                (
                    pos
                    for pos in positions
                    if pos >= start and tuple(lines[pos : pos + len(hunk.old)]) == hunk.old
                ),
                None,
            )
            if position is None:
                raise ValueError("Patch context was not found")
        output.extend(lines[cursor:position])
        output.extend(hunk.new)
        cursor = position + len(hunk.old)
    output.extend(lines[cursor:])
    return newline.join(output) + (newline if output and final_newline else "")

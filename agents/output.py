"""Pi-compatible initial previews and DSH pressure pruning of saved output."""

from dataclasses import dataclass

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 51200
GREP_MAX_LINE_LENGTH = 500
PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"


@dataclass(frozen=True)
class TruncationResult:
    content: str
    truncated: bool = False
    output_lines: int = 0
    total_lines: int = 0
    truncated_by: str | None = None
    first_line_exceeds_limit: bool = False
    last_line_partial: bool = False


def _lines(content):
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def truncate_head(content, max_lines=DEFAULT_MAX_LINES, max_bytes=DEFAULT_MAX_BYTES):
    lines = _lines(content)
    if len(lines) <= max_lines and len(content.encode("utf-8")) <= max_bytes:
        return TruncationResult(content, output_lines=len(lines), total_lines=len(lines))
    if lines and len(lines[0].encode("utf-8")) > max_bytes:
        return TruncationResult("", True, 0, len(lines), "bytes", True)
    selected, size, reason = [], 0, "lines"
    for line in lines[:max_lines]:
        cost = len(line.encode("utf-8")) + bool(selected)
        if size + cost > max_bytes:
            reason = "bytes"
            break
        selected.append(line)
        size += cost
    return TruncationResult("\n".join(selected), True, len(selected), len(lines), reason)


def truncate_tail(content, max_lines=DEFAULT_MAX_LINES, max_bytes=DEFAULT_MAX_BYTES):
    lines = _lines(content)
    if len(lines) <= max_lines and len(content.encode("utf-8")) <= max_bytes:
        return TruncationResult(content, output_lines=len(lines), total_lines=len(lines))
    selected, size, reason, partial = [], 0, "lines", False
    for line in reversed(lines[-max_lines:]):
        cost = len(line.encode("utf-8")) + bool(selected)
        if size + cost > max_bytes:
            reason = "bytes"
            if not selected:
                selected.append(line.encode("utf-8")[-max_bytes:].decode("utf-8", "ignore"))
                partial = True
            break
        selected.append(line)
        size += cost
    return TruncationResult(
        "\n".join(reversed(selected)),
        True,
        len(selected),
        len(lines),
        reason,
        last_line_partial=partial,
    )


def prune_tool_result(content):
    """Python strings are Unicode code points, matching DSH Array.from(text)."""
    return content if len(content) <= 8192 else content[:4096] + PRUNE_MARKER + content[-1024:]


def preview_output(result):
    """Called only after the complete handler output has been saved as an artifact."""
    metadata = result.structured if isinstance(result.structured, dict) else {}
    if result.name == "read_file" and metadata.get("_preview") == "read":
        start = metadata["offset"]
        full_lines = result.content.split("\n")
        selected = (
            "\n".join(full_lines[: metadata["limit"]]) if metadata.get("limit") else result.content
        )
        truncation = truncate_head(selected)
        if truncation.first_line_exceeds_limit:
            return (
                f"[Line {start} exceeds 50.0KB limit. Use run_shell to read a byte range. "
                f"Full output: {result.output_ref}]"
            )
        if truncation.truncated:
            end = start + truncation.output_lines - 1
            return (
                truncation.content + f"\n\n[Showing lines {start}-{end} of "
                f"{metadata['total_lines']}. Use offset={end + 1} to continue.]"
            )
        if metadata.get("limit") is not None and metadata["limit"] < len(full_lines):
            remaining = len(full_lines) - metadata["limit"]
            return (
                truncation.content + f"\n\n[{remaining} more lines in file. "
                f"Use offset={start + metadata['limit']} to continue.]"
            )
        return truncation.content
    if result.name == "grep_search" and metadata.get("_preview") == "grep":
        matches = metadata["matches"]
        limit = metadata.get("limit", 100)
        lines = [
            f"{m['path']}:{m['line']}: "
            + (m["text"][:500] + "... [truncated]" if len(m["text"]) > 500 else m["text"])
            for m in matches[:limit]
        ]
        truncation = truncate_head("\n".join(lines), max_lines=max(len(lines), 1))
        notices = []
        if len(matches) >= limit:
            notices.append(
                f"{limit} matches limit reached. Use limit={limit * 2} for more, or refine pattern"
            )
        if truncation.truncated:
            notices.append("50.0KB limit reached")
        if any(len(m["text"]) > 500 for m in matches[:limit]):
            notices.append("Some lines truncated to 500 chars. Use read_file to see full lines")
        return (truncation.content or "No matches found") + (
            "\n\n[" + ". ".join(notices) + "]" if notices else ""
        )
    tail = result.name in {"run_shell", "wait_job"}
    truncation = truncate_tail(result.content) if tail else truncate_head(result.content)
    content = truncation.content
    if truncation.truncated:
        content += (
            f"\n\n[Output truncated ({truncation.truncated_by}). Full output: {result.output_ref}]"
        )
    if tail and metadata.get("returncode") not in {0, None}:
        content += f"\n\nCommand exited with code {metadata['returncode']}"
    return content

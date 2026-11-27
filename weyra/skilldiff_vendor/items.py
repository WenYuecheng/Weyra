"""Partition a skill into the item types used in the paper.

Ported verbatim from `LitaoGuo/How-Self-Evolution-Reshapes-Skills` (`experiments/items.py`,
MIT License) at commit `db2cdbde29e752ebc9cc81817f17400b64056c40`.
"""

import re

KEYLINE = re.compile(r"^[A-Za-z_][\w-]*\s*:")
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^#{1,6}\s")
LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
TABLE = re.compile(r"^\s*\|")


def split_document(text):
    if not text.startswith("---"):
        return "", text
    lines = text.splitlines(keepends=True)
    end = 1
    while end < len(lines):
        content = lines[end].strip()
        if content == "---":
            end += 1
            break
        if content and not KEYLINE.match(content) and not lines[end].startswith((" ", "\t", "-")):
            break
        end += 1
    front = "".join(lines[:end])
    body = "".join(lines[end:])
    if not body.strip():
        return "", text
    return front, body


def split_items(text):
    front, body = split_document(text)
    lines = body.splitlines(keepends=True)
    content_lines = [line.rstrip("\r\n") for line in lines]
    offsets = [len(front)]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    items = []
    if front:
        items.append({"id": "F0", "type": "front matter", "start": 0, "end": len(front), "text": front})
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        end = index + 1
        if FENCE.match(content_lines[index]):
            while end < len(lines) and not FENCE.match(content_lines[end]):
                end += 1
            end = min(end + 1, len(lines))
            kind = "code block"
        elif HEADING.match(content_lines[index]):
            kind = "heading"
        elif TABLE.match(content_lines[index]):
            while end < len(lines) and TABLE.match(content_lines[end]):
                end += 1
            kind = "table"
        else:
            kind = "list entry" if LIST.match(content_lines[index]) else "paragraph"
            while end < len(lines) and lines[end].strip():
                if any(pattern.match(content_lines[end]) for pattern in (FENCE, HEADING, LIST, TABLE)):
                    break
                end += 1
        start_offset, end_offset = offsets[index], offsets[end]
        items.append({
            "id": f"B{sum(item['type'] != 'front matter' for item in items) + 1}",
            "type": kind, "start": start_offset, "end": end_offset,
            "text": text[start_offset:end_offset],
        })
        index = end
    return items

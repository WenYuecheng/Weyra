"""Markdown metadata, atomic file publication and lexical retrieval helpers."""
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".weyra-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def reject_symlinks(path):
    for component in [Path(path), *Path(path).parents]:
        if component.is_symlink():
            raise ValueError("Symlink paths are not allowed")


def tokens(text):
    text = text.casefold()
    return (
        re.findall(r"[^\W_]+", text)
        + re.findall(r"[\u3400-\u9fff]", text)
        + [
            text[i : i + 2]
            for i in range(len(text) - 1)
            if all("\u3400" <= c <= "\u9fff" for c in text[i : i + 2])
        ]
    )


def rank(query, items, limit):
    q = set(tokens(query))
    docs = [Counter(tokens(text)) for _, text in items]
    n = len(docs)
    avg = sum(sum(d.values()) for d in docs) / max(n, 1) or 1
    scores = []
    for (item, _), d in zip(items, docs):
        score = 0
        for term in q:
            freq = d[term]
            df = sum(term in x for x in docs)
            score += (
                math.log(1 + (n - df + 0.5) / (df + 0.5))
                * freq
                * 2.2
                / (freq + 1.2 * (0.25 + 0.75 * sum(d.values()) / avg))
                if freq
                else 0
            )
        if score:
            scores.append((score, item))
    return [item for _, item in sorted(scores, key=lambda x: (-x[0], str(x[1])))[:limit]]


def parse_markdown(text):
    metadata = {}
    body = text
    if text.startswith("---\n"):
        parts = text.split("\n---", 1)
        if len(parts) != 2:
            raise ValueError("Unclosed frontmatter")
        for line in parts[0][4:].splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                key, value = line.split(":", 1)
                value = value.strip()
                if value.startswith(chr(34)):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = value.strip(chr(34))
                else:
                    value = value.strip(chr(39))
                metadata[key.strip()] = value
        body = parts[1].lstrip("\r\n")
    return metadata, body

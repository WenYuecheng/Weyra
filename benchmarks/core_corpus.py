"""Versioned host-side fixtures. Never copy this manifest into an agent workspace."""
from __future__ import annotations
import copy
import json
from pathlib import Path

_DATA = Path(__file__).resolve().parent / 'data'

def load_core_manifest(path: str | Path | None = None) -> dict:
    return json.loads((Path(path) if path is not None else _DATA / 'core-eval-v1.json').read_text(encoding='utf-8'))

def load_public_cases() -> list[dict]:
    return load_core_manifest()['public_cases']

def public_task(case: dict) -> dict:
    """Explicit allowlist; grading, source answers, and rationale stay on the host."""
    return copy.deepcopy({k: case[k] for k in ('id','type','split','input','family','group_id') if k in case})

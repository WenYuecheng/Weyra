"""Standardized I/O types for SkillOpt (`microsoft/SkillOpt`, MIT License).

Ported verbatim from `skillopt/types.py` and `skillopt_sleep/types.py` at commit
`dc0b5e654cafa1073a2819086151c1e0d13012f0` (dropping unused dataset `BatchSpec` import).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import fields as dc_fields
from typing import Any, List, Literal

from .gate import GateAction, GateResult  # noqa: F401

# ── Atomic types ─────────────────────────────────────────────────────────

EditOp = Literal["append", "insert_after", "replace", "delete"]


@dataclass
class Edit:
    """A single edit operation on a skill document.

    Used across Reflect -> Aggregate -> Select -> Update -> MetaReflect.
    """

    op: EditOp
    content: str = ""
    target: str = ""
    support_count: int | None = None
    source_type: Literal["failure", "success"] | None = None
    merge_level: int | None = None
    update_origin: str = ""
    update_target: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> Edit:
        return cls(
            op=d.get("op", "append"),
            content=d.get("content", ""),
            target=d.get("target", ""),
            support_count=d.get("support_count"),
            source_type=d.get("source_type"),
            merge_level=d.get("merge_level"),
            update_origin=d.get("update_origin", ""),
            update_target=d.get("update_target", ""),
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"op": self.op, "content": self.content}
        if self.target:
            d["target"] = self.target
        if self.support_count is not None:
            d["support_count"] = self.support_count
        if self.source_type is not None:
            d["source_type"] = self.source_type
        if self.merge_level is not None:
            d["merge_level"] = self.merge_level
        if self.update_origin:
            d["update_origin"] = self.update_origin
        if self.update_target:
            d["update_target"] = self.update_target
        return d


@dataclass
class Patch:
    """A set of edits with reasoning.

    Output of Aggregate, Select; input to Update.
    """

    edits: list[Edit] = field(default_factory=list)
    reasoning: str = ""
    ranking_details: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Patch:
        edits_raw = d.get("edits", [])
        return cls(
            edits=[Edit.from_dict(e) if isinstance(e, dict) else e for e in edits_raw],
            reasoning=d.get("reasoning", ""),
            ranking_details=d.get("ranking_details"),
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "reasoning": self.reasoning,
            "edits": [e.to_dict() if isinstance(e, Edit) else e for e in self.edits],
        }
        if self.ranking_details is not None:
            d["ranking_details"] = self.ranking_details
        return d


@dataclass
class RolloutResult:
    """Result of a single episode/task rollout.

    Universal fields are required; env-specific fields live in ``extras``.
    """

    id: str
    hard: float
    soft: float
    n_turns: int = 0
    fail_reason: str = ""
    task_type: str = ""
    task_description: str = ""
    predicted_answer: str = ""
    question: str = ""
    reference_text: str = ""
    target_system_prompt: str = ""
    target_user_prompt: str = ""
    spreadsheet_preview: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    _KNOWN_FIELDS: frozenset[str] | None = field(
        default=None, init=False, repr=False, compare=False,
    )

    @classmethod
    def _get_known_fields(cls) -> frozenset[str]:
        if cls._KNOWN_FIELDS is None:
            cls._KNOWN_FIELDS = frozenset(
                f.name for f in dc_fields(cls)
                if f.name != "_KNOWN_FIELDS"
            )
        return cls._KNOWN_FIELDS

    @classmethod
    def from_dict(cls, d: dict) -> RolloutResult:
        known = cls._get_known_fields()
        extras = {k: v for k, v in d.items() if k not in known}
        return cls(
            id=str(d.get("id", "")),
            hard=float(d.get("hard", 0)),
            soft=float(d.get("soft", 0.0)),
            n_turns=int(d.get("n_turns", 0)),
            fail_reason=str(d.get("fail_reason", "")),
            task_type=str(d.get("task_type", "")),
            task_description=str(d.get("task_description", "")),
            predicted_answer=str(d.get("predicted_answer", "")),
            question=str(d.get("question", "")),
            reference_text=str(d.get("reference_text", "")),
            target_system_prompt=str(d.get("target_system_prompt", "")),
            target_user_prompt=str(d.get("target_user_prompt", "")),
            spreadsheet_preview=str(d.get("spreadsheet_preview", "")),
            extras=extras,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "hard": self.hard,
            "soft": self.soft,
            "n_turns": self.n_turns,
            "fail_reason": self.fail_reason,
            "task_type": self.task_type,
            "task_description": self.task_description,
            "predicted_answer": self.predicted_answer,
            "question": self.question,
            "reference_text": self.reference_text,
            "target_system_prompt": self.target_system_prompt,
            "target_user_prompt": self.target_user_prompt,
            "spreadsheet_preview": self.spreadsheet_preview,
        }
        d.update(self.extras)
        return d


@dataclass
class EditRecord:
    """One bounded edit proposed/applied to skill or memory (`skillopt_sleep/types.py`)."""

    target: str                       # "skill" | "memory"
    op: str                           # add | delete | replace
    content: str = ""
    anchor: str = ""                  # for replace/delete: text being changed
    rationale: str = ""
    source_task_ids: List[str] = field(default_factory=list)
    source_session_ids: List[str] = field(default_factory=list)

"""Provider-neutral values shared by the execution kernel."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


def new_id() -> str:
    return uuid.uuid4().hex


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    role: str
    content: str = ""
    id: str = field(default_factory=new_id)
    source: str = "user"
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    provider_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Message:
        data = copy.deepcopy(data)
        data["tool_calls"] = tuple(ToolCall(**call) for call in data.get("tool_calls", ()))
        return cls(**data)


@dataclass(frozen=True)
class ModelInfo:
    provider_id: str
    model_id: str
    context_window: int
    max_output_tokens: int
    supports_tools: bool = True
    supports_reasoning: bool = False
    supports_cache: bool = False
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    def __post_init__(self):
        if not self.provider_id or not self.model_id:
            raise ValueError("provider_id and model_id are required")
        if not 0 < self.max_output_tokens < self.context_window:
            raise ValueError("Output reserve must be positive and below the context window")


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass
class ModelResponse:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    provider_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StreamEvent:
    kind: str
    data: Any = None


@dataclass(frozen=True, init=False)
class PreparedRequest:
    model: ModelInfo
    system: str
    purpose: str = "main"
    context_version: int = 0
    id: str = field(default_factory=new_id)
    _payload: str = ""

    def __init__(
        self,
        model,
        system,
        messages,
        tools=(),
        purpose="main",
        context_version=0,
        parameters=None,
        id=None,
    ):
        for key, value in {
            "model": model,
            "system": system,
            "purpose": purpose,
            "context_version": context_version,
            "id": id or new_id(),
        }.items():
            object.__setattr__(self, key, value)
        object.__setattr__(
            self,
            "_payload",
            canonical(
                {
                    "messages": [m.to_dict() for m in messages],
                    "tools": tools,
                    "parameters": parameters or {},
                }
            ),
        )

    @property
    def messages(self):
        return tuple(Message.from_dict(m) for m in json.loads(self._payload)["messages"])

    @property
    def tools(self):
        return tuple(json.loads(self._payload)["tools"])

    @property
    def parameters(self):
        return json.loads(self._payload)["parameters"]

    def to_dict(self) -> dict:
        return {
            "model": asdict(self.model),
            "system": self.system,
            "purpose": self.purpose,
            "context_version": self.context_version,
            "id": self.id,
            **json.loads(self._payload),
        }

    @property
    def fingerprint(self) -> str:
        data = self.to_dict()
        data.pop("id")
        return digest(data)

    @property
    def baseline_fingerprint(self) -> str:
        return digest({"model": asdict(self.model), "system": self.system, "tools": self.tools})


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    status: str = "success"
    started: bool = False
    effect: str = "none"
    structured: Any = None
    output_ref: str | None = None

    @property
    def is_error(self) -> bool:
        return self.status != "success"


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    handler: Callable
    execution_mode: str = "exclusive"
    permission: str = "write"

    def declaration(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.schema}


@dataclass
class ChildResult:
    task_id: str
    state: str
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    usage: int = 0

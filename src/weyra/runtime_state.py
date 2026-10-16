"""Cancellation, reservations and resource ownership shared by a session."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import new_id


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(
        self,
        token_limit: int | None = None,
        call_limit: int | None = None,
        maintenance_limit: int | None = None,
    ):
        for value in (token_limit, call_limit, maintenance_limit):
            if value is not None and value < 0:
                raise ValueError("Budget limits cannot be negative")
        self.token_limit = token_limit
        self.call_limit = call_limit
        self.maintenance_limit = maintenance_limit
        self.total_tokens = 0
        self.calls = 0
        self.unknown_calls = 0
        self.records: list[dict] = []
        self._pending: dict[str, tuple[int, str]] = {}
        self._maintenance = 0
        self._lock = asyncio.Lock()
        self._journal = None

    def attach(self, session):
        if self._journal is not None:
            return
        state = next(
            (
                record["data"]
                for record in reversed(session.records)
                if record["kind"] == "budget_checkpoint"
            ),
            None,
        )
        if state:
            pending = state.get("pending", [])
            self.total_tokens = state["total_tokens"] + sum(item[0] for item in pending)
            self.calls = state["calls"]
            self.unknown_calls = state["unknown_calls"] + len(pending)
            self._maintenance = state.get("maintenance_tokens", 0) + sum(
                amount for amount, purpose in pending if purpose in {"evolution", "maintenance"}
            )
        self._journal = session
        self._checkpoint()

    def _checkpoint(self):
        if self._journal is not None:
            self._journal.append(
                "budget_checkpoint",
                {
                    "total_tokens": self.total_tokens,
                    "calls": self.calls,
                    "unknown_calls": self.unknown_calls,
                    "maintenance_tokens": self._maintenance,
                    "pending": list(self._pending.values()),
                },
                True,
            )

    async def reserve(self, amount: int, purpose: str = "main") -> str:
        if amount < 0:
            raise ValueError("Negative reservation")
        async with self._lock:
            pending = sum(value[0] for value in self._pending.values())
            if (
                self.token_limit is not None
                and self.total_tokens + pending + amount > self.token_limit
            ):
                raise BudgetExceeded("Session token budget exhausted")
            if self.call_limit is not None and self.calls >= self.call_limit:
                raise BudgetExceeded("Session model-call budget exhausted")
            if purpose in {"evolution", "maintenance"} and self.maintenance_limit is not None:
                held = sum(
                    n for n, p in self._pending.values() if p in {"evolution", "maintenance"}
                )
                if self._maintenance + held + amount > self.maintenance_limit:
                    raise BudgetExceeded("Maintenance allowance exhausted")
            reservation = new_id()
            self._pending[reservation] = (amount, purpose)
            self.calls += 1
            self._checkpoint()
            return reservation

    async def settle(self, reservation: str, actual: int | None) -> None:
        if actual is not None and actual < 0:
            raise ValueError("Negative usage")
        async with self._lock:
            amount, purpose = self._pending.pop(reservation)
            charged = amount if actual is None else actual
            self.total_tokens += charged
            if purpose in {"evolution", "maintenance"}:
                self._maintenance += charged
            self.unknown_calls += actual is None
            self.records.append(
                {
                    "reservation": reservation,
                    "purpose": purpose,
                    "tokens": actual,
                    "accounted_tokens": charged,
                }
            )
            self._checkpoint()

    @property
    def pending_tokens(self) -> int:
        return sum(amount for amount, _ in self._pending.values())


@dataclass
class RunContext:
    project: Path
    session: Any
    budget: Budget
    permission_mode: str = "default"
    id: str = field(default_factory=new_id)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    parent_cancel: asyncio.Event | None = None
    plan_path: Path | None = None
    services: dict = field(default_factory=dict)
    read_versions: dict = field(default_factory=dict)
    depth: int = 0
    on_event: Any = None
    _resources: list = field(default_factory=list)

    def check_cancelled(self):
        if self.cancel_event.is_set() or (self.parent_cancel and self.parent_cancel.is_set()):
            raise asyncio.CancelledError

    async def emit(self, kind: str, data: Any = None):
        if self.on_event:
            result = self.on_event(kind, data)
            if inspect.isawaitable(result):
                await result

    def register_resource(self, cleanup):
        self._resources.append(cleanup)

    async def close(self):
        errors = []
        for cleanup in reversed(self._resources):
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                errors.append(type(error).__name__)
        self._resources.clear()
        if errors:
            await self.emit("cleanup_errors", errors)


@dataclass
class Goal:
    objective: str
    revision: int = 1
    todos: list[dict] = field(default_factory=list)
    state: str = "active"
    continuations: int = 0
    stagnant_rounds: int = 0
    progress: str = ""

    def revise(self, objective: str):
        self.objective = objective
        self.revision += 1
        self.continuations = 0
        self.stagnant_rounds = 0
        self.state = "active"

    def should_continue(self, progress: str) -> bool:
        if self.state != "active" or not any(not item.get("done") for item in self.todos):
            return False
        self.stagnant_rounds = self.stagnant_rounds + 1 if progress == self.progress else 0
        self.progress = progress
        if self.continuations >= 3 or self.stagnant_rounds >= 2:
            self.state = "stopped"
            return False
        self.continuations += 1
        return True

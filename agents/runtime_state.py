"""Cancellation, reservations and resource ownership shared by a session."""

from __future__ import annotations

import asyncio
import inspect
import copy
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import new_id


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Session root ledger; reservations are durable before any physical request."""

    def __init__(
        self, token_limit=None, call_limit=None, maintenance_limit=None, *, cost_limit_cny=None
    ):
        for value in (token_limit, call_limit, maintenance_limit):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError("Budget limits cannot be negative")
        self.token_limit, self.call_limit, self.maintenance_limit = (
            token_limit,
            call_limit,
            maintenance_limit,
        )
        self.cost_limit_cny = self._money(cost_limit_cny) if cost_limit_cny is not None else None
        self.total_tokens = self.calls = self.unknown_calls = self._maintenance = 0
        self.total_cost_cny = Decimal(0)
        self.records, self._pending, self._scopes = [], {}, {}
        self._lock, self._journal = asyncio.Lock(), None

    @staticmethod
    def _money(value):
        try:
            result = Decimal(str(value))
        except (ValueError, InvalidOperation):
            raise ValueError("Cost must be a finite nonnegative CNY amount") from None
        if not result.is_finite() or result < 0:
            raise ValueError("Cost must be a finite nonnegative CNY amount")
        return result

    def set_scope_limit(self, scope, token_limit):
        if token_limit is not None and token_limit < 0:
            raise ValueError("Task limit cannot be negative")
        self._scopes.setdefault(scope, {"limit": token_limit, "tokens": 0})["limit"] = token_limit

    def attach(self, session):
        if self._journal is not None:
            return
        state = next(
            (r["data"] for r in reversed(session.records) if r["kind"] == "budget_checkpoint"), None
        )
        self.records = [r["data"] for r in session.records if r["kind"] == "budget_settlement"]
        if state:
            pending = state.get("pending", {})
            if isinstance(pending, list):
                pending = {
                    str(i): {"tokens": v[0], "purpose": v[1], "cost": "0", "scope": None}
                    for i, v in enumerate(pending)
                }
            self.total_tokens = state["total_tokens"] + sum(v["tokens"] for v in pending.values())
            self.calls = state["calls"]
            self.unknown_calls = state["unknown_calls"] + len(pending)
            self._maintenance = state.get("maintenance_tokens", 0) + sum(
                v["tokens"]
                for v in pending.values()
                if v["purpose"] in {"compression", "memory", "evolution", "maintenance"}
            )
            self.total_cost_cny = Decimal(state.get("total_cost_cny", "0")) + sum(
                (Decimal(v.get("cost", "0")) for v in pending.values()), Decimal(0)
            )
            self._scopes = state.get("scopes", {})
            for value in pending.values():
                scope = value.get("scope")
                if scope in self._scopes:
                    self._scopes[scope]["tokens"] += value["tokens"]
        self._journal = session
        self._checkpoint()

    def _state(self):
        return {
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "unknown_calls": self.unknown_calls,
            "maintenance_tokens": self._maintenance,
            "total_cost_cny": str(self.total_cost_cny),
            "pending": self._pending,
            "scopes": self._scopes,
        }

    def _checkpoint(self):
        if self._journal is not None:
            self._journal.append("budget_checkpoint", self._state(), True)

    def _restore(self, state):
        self.total_tokens, self.calls, self.unknown_calls = (
            state["total_tokens"],
            state["calls"],
            state["unknown_calls"],
        )
        self._maintenance = state["maintenance_tokens"]
        self.total_cost_cny = Decimal(state["total_cost_cny"])
        self._pending, self._scopes = state["pending"], state["scopes"]

    async def reserve(self, amount, purpose="main", *, estimated_cost_cny=None, scope=None):
        if isinstance(amount, bool) or amount < 0:
            raise ValueError("Negative reservation")
        if self.cost_limit_cny is not None and estimated_cost_cny is None:
            raise BudgetExceeded("Verified CNY pricing is required before a paid request")
        cost = self._money(estimated_cost_cny or 0)
        async with self._lock:
            pending = sum(v["tokens"] for v in self._pending.values())
            held_cost = sum((Decimal(v["cost"]) for v in self._pending.values()), Decimal(0))
            if (
                self.token_limit is not None
                and self.total_tokens + pending + amount > self.token_limit
            ):
                raise BudgetExceeded("Session token budget exhausted")
            if self.call_limit is not None and self.calls >= self.call_limit:
                raise BudgetExceeded("Session model-call budget exhausted")
            if (
                self.cost_limit_cny is not None
                and self.total_cost_cny + held_cost + cost > self.cost_limit_cny
            ):
                raise BudgetExceeded("Session CNY budget exhausted")
            maintenance = purpose in {"compression", "memory", "evolution", "maintenance"}
            if maintenance and self.maintenance_limit is not None:
                held = sum(
                    v["tokens"]
                    for v in self._pending.values()
                    if v["purpose"] in {"compression", "memory", "evolution", "maintenance"}
                )
                if self._maintenance + held + amount > self.maintenance_limit:
                    raise BudgetExceeded("Maintenance allowance exhausted")
            local = self._scopes.get(scope)
            if local and local["limit"] is not None:
                held = sum(v["tokens"] for v in self._pending.values() if v.get("scope") == scope)
                if local["tokens"] + held + amount > local["limit"]:
                    raise BudgetExceeded("Task token budget exhausted")
            before = copy.deepcopy(self._state())
            reservation = new_id()
            self._pending[reservation] = {
                "tokens": amount,
                "purpose": purpose,
                "cost": str(cost),
                "scope": scope,
            }
            self.calls += 1
            try:
                self._checkpoint()
            except BaseException:
                self._restore(before)
                raise
            return reservation

    async def settle(self, reservation, actual, *, actual_cost_cny=None, cost_known=None):
        if actual is not None and actual < 0:
            raise ValueError("Negative usage")
        async with self._lock:
            if reservation not in self._pending:
                if any(r["reservation"] == reservation for r in self.records):
                    return
                raise ValueError("Unknown reservation")
            before = copy.deepcopy(self._state())
            value = self._pending.pop(reservation)
            charged = value["tokens"] if actual is None else actual
            cost = (
                Decimal(value["cost"]) if actual_cost_cny is None else self._money(actual_cost_cny)
            )
            self.total_tokens += charged
            self.total_cost_cny += cost
            if value["purpose"] in {"compression", "memory", "evolution", "maintenance"}:
                self._maintenance += charged
            self.unknown_calls += actual is None
            scope = value.get("scope")
            if scope in self._scopes:
                self._scopes[scope]["tokens"] += charged
            record = {
                "reservation": reservation,
                "purpose": value["purpose"],
                "scope": scope,
                "tokens": actual,
                "accounted_tokens": charged,
                "cost_cny": str(cost),
                "cost_known": actual_cost_cny is not None if cost_known is None else cost_known,
            }
            try:
                if self._journal is not None:
                    with self._journal.transaction():
                        self._journal.append("budget_settlement", record, True)
                        self._checkpoint()
                else:
                    self._checkpoint()
            except BaseException:
                self._restore(before)
                raise
            self.records.append(record)

    @property
    def pending_tokens(self):
        return sum(value["tokens"] for value in self._pending.values())


@dataclass
class RunContext:
    project: Path
    session: Any
    budget: Budget
    permission_mode: str = "default"
    id: str = field(default_factory=new_id)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    parent_cancel: asyncio.Event | None = None
    budget_scope: str | None = None
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

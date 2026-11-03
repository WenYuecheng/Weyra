"""Single durable CNY ledger with phase and per-task ceilings."""

from decimal import Decimal
import math
import asyncio
import statistics
from weyra.core.state import Budget, BudgetExceeded


class CampaignBudget(Budget):
    def __init__(self):
        super().__init__(cost_limit_cny="100")
        self.active = None
        self._campaign_lock = asyncio.Lock()
        self.recovered = {}
        self.recovered_calls = {}

    def attach(self, session):
        if self._journal is not None:
            return
        state = next(
            (r["data"] for r in reversed(session.records) if r["kind"] == "budget_checkpoint"), {}
        )
        self.recovered = {k: Decimal(v) for k, v in state.get("campaign_recovered", {}).items()}
        self.recovered_calls = dict(state.get("campaign_recovered_calls", {}))
        for v in state.get("pending", {}).values():
            scope = v["scope"]
            self.recovered[scope] = self.recovered.get(scope, Decimal(0)) + Decimal(v["cost"])
            self.recovered_calls[scope] = self.recovered_calls.get(scope, 0) + 1
        super().attach(session)

    def _state(self):
        return {
            **super()._state(),
            "campaign_recovered": {k: str(v) for k, v in self.recovered.items()},
            "campaign_recovered_calls": self.recovered_calls,
        }

    def begin(self, task_id, phase="formal"):
        if phase not in ("check", "formal"):
            raise ValueError("Invalid phase")
        self.active = phase + "/" + task_id

    def task_cost(self, scope, *, pending=True):
        cost = self.recovered.get(scope, Decimal(0)) + sum(
            (Decimal(r["cost_cny"]) for r in self.records if r["scope"] == scope), Decimal(0)
        )
        if pending:
            cost += sum(
                (Decimal(r["cost"]) for r in self._pending.values() if r["scope"] == scope),
                Decimal(0),
            )
        return cost

    def phase_cost(self, phase):
        scopes = (
            set(self.recovered)
            | {r["scope"] for r in self.records}
            | {r["scope"] for r in self._pending.values()}
        )
        return sum(
            (self.task_cost(s) for s in scopes if s and s.startswith(phase + "/")), Decimal(0)
        )

    async def reserve(self, amount, purpose="main", *, estimated_cost_cny=None, scope=None):
        async with self._campaign_lock:
            return await self._reserve(
                amount, purpose, estimated_cost_cny=estimated_cost_cny, scope=scope
            )

    async def _reserve(self, amount, purpose="main", *, estimated_cost_cny=None, scope=None):
        if self.active is None:
            raise ValueError("No active task")
        cost = self._money(estimated_cost_cny) if estimated_cost_cny is not None else None
        if cost is None:
            raise BudgetExceeded("Verified ceiling pricing required")
        phase = self.active.split("/")[0]
        task_limit = Decimal("2.4") if phase == "formal" else Decimal("4")
        phase_limit = Decimal("96") if phase == "formal" else Decimal("4")
        calls = (
            sum(r["scope"] == self.active for r in self.records)
            + sum(r["scope"] == self.active for r in self._pending.values())
            + self.recovered_calls.get(self.active, 0)
        )
        if (
            calls >= 80
            or self.task_cost(self.active) + cost > task_limit
            or self.phase_cost(phase) + cost > phase_limit
        ):
            raise BudgetExceeded("Campaign phase/task/call limit exhausted")
        return await super().reserve(amount, purpose, estimated_cost_cny=cost, scope=self.active)


def summarize(rows, costs):
    passed = sum(r.get("verifier") == "pass" for r in rows)
    p = passed / 40
    z = 1.959963984540054
    den = 1 + z * z / 40
    center = (p + z * z / 80) / den
    half = z * math.sqrt(p * (1 - p) / 40 + z * z / (4 * 40 * 40)) / den
    steps = [r.get("model_steps", 0) for r in rows if r.get("started")]
    formal = Decimal(costs["formal"])
    return {
        "denominator": 40,
        "passed": passed,
        "pass_at_1": p,
        "wilson95": [max(0, center - half), min(1, center + half)],
        "languages": {
            lang: {
                "passed": sum(
                    r.get("verifier") == "pass" and r["id"].startswith(lang + "/") for r in rows
                ),
                "denominator": 20,
            }
            for lang in ("python", "javascript")
        },
        "cost_cny": costs,
        "cost_basis": "conservative_ceiling_not_provider_invoice",
        "cost_per_planned_task_cny": str(formal / 40),
        "cost_per_started_task_cny": str(formal / len(steps)) if steps else None,
        "cost_per_passed_task_cny": str(formal / passed) if passed else None,
        "model_steps_mean": statistics.mean(steps) if steps else None,
        "model_steps_median": statistics.median(steps) if steps else None,
        "started": len(steps),
        "verification_runnable": sum(r.get("verifier") in ("pass", "fail") for r in rows),
        "limitations": [
            "40-task two-language subset; not official leaderboard score",
            "public tests isolated at runtime, training contamination unknown",
            "Wilson interval descriptive; exercises may be correlated",
        ],
        "tasks": rows,
    }

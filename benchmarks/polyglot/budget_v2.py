"""Durable v2 campaign budget with isolated first-pass and repair allowances.

Attempt identity lives in the root ledger's reservation scope, including pending
requests. CampaignBudget's recovery therefore conservatively retains unknown
charges and physical calls without a second journal or post-reservation update.
"""

from contextvars import ContextVar
from decimal import Decimal

from weyra.core.state import Budget, BudgetExceeded

from .accounting import CampaignBudget


class FeedbackBudget(CampaignBudget):
    """Share CNY 40 across four workers: formal 36, check 4.

    Formal tasks have CNY 1 for ``first`` and .5 for ``repair``, CNY 1.5
    combined, and at most 80 physical calls across both attempts. Check calls
    share the separate CNY 4 phase allowance. Scheduling concurrency belongs to
    the runner; each worker calls :meth:`begin` before reserving requests.
    """

    def __init__(self):
        self._active_context = ContextVar("polyglot_v2_task_attempt", default=None)
        super().__init__()
        self.cost_limit_cny = Decimal("40")

    @property
    def active(self):
        return self._active_context.get()

    @active.setter
    def active(self, value):
        self._active_context.set(value)

    def begin(self, task_id, phase="formal", attempt="first"):
        if phase not in ("formal", "check"):
            raise ValueError("Invalid phase")
        if attempt not in ("first", "repair"):
            raise ValueError("Invalid attempt")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Invalid task ID")
        self.active = f"{phase}/{task_id}/{attempt}"

    def stage_total(self, task_id, attempt, phase="formal"):
        """Return settled plus conservatively reserved/recovered stage cost."""
        return self.task_cost(f"{phase}/{task_id}/{attempt}")

    def task_total(self, task_id, phase="formal"):
        """Return both attempts' total, including unknown outcomes."""
        return sum(
            (self.stage_total(task_id, attempt, phase) for attempt in ("first", "repair")),
            Decimal(0),
        )

    async def _reserve(self, amount, purpose="main", *, estimated_cost_cny=None, scope=None):
        # CampaignBudget.reserve holds one shared lock through every ceiling
        # check and the durable root reservation. Ignore a model's random scope.
        active = self.active
        if active is None:
            raise ValueError("No active task")
        if estimated_cost_cny is None:
            raise BudgetExceeded("Verified ceiling pricing required")
        cost = self._money(estimated_cost_cny)
        phase, task_attempt = active.split("/", 1)
        task_id, attempt = task_attempt.rsplit("/", 1)
        scopes = {f"{phase}/{task_id}/{stage}" for stage in ("first", "repair")}
        calls = (
            sum(record["scope"] in scopes for record in self.records)
            + sum(record["scope"] in scopes for record in self._pending.values())
            + sum(self.recovered_calls.get(stage, 0) for stage in scopes)
        )
        phase_limit = Decimal("36") if phase == "formal" else Decimal("4")
        stage_limit = Decimal("1") if attempt == "first" else Decimal(".5")
        if (
            calls >= 80
            or self.phase_cost(phase) + cost > phase_limit
            or (
                phase == "formal"
                and (
                    self.task_total(task_id, phase) + cost > Decimal("1.5")
                    or self.stage_total(task_id, attempt, phase) + cost > stage_limit
                )
            )
        ):
            raise BudgetExceeded("Campaign phase/task/stage/call limit exhausted")
        # Bypass v1's CNY 2.4/task and 96/formal checks, retaining the original
        # monetary journal, cancellation rollback and idempotent settlement.
        return await Budget.reserve(
            self, amount, purpose, estimated_cost_cny=cost, scope=active
        )

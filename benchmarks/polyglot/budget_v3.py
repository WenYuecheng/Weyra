"""Durable v3 budget: one allowance per task across every feedback round."""

import copy
from contextvars import ContextVar
from decimal import Decimal
import time

from weyra.core.state import Budget, BudgetExceeded

from .accounting import CampaignBudget


class IterativeBudget(CampaignBudget):
    """Share CNY 33.25 across workers, retaining the combined CNY 40 ceiling.

    Previous campaign spend is opening metadata, never a model request or a
    physical call. Each task shares CNY .95 and 120 physical requests across all
    rounds and purposes. ``spend_total`` includes unsettled reservations, while
    ``previous_cost_cny`` remains separate for transparent combined reporting.
    """

    def __init__(self, previous_cost_cny="5.8554306629531"):
        previous = Budget._money(previous_cost_cny)
        if previous > Decimal("40"):
            raise ValueError("Previous campaign cost exceeds combined limit")
        self._active_context = ContextVar("polyglot_v3_task", default=None)
        super().__init__()
        self.previous_cost_cny = previous
        self.cost_limit_cny = min(Decimal("33.25"), Decimal("40") - previous)
        self._task_deadlines = {}
        self._reconciled = {}
        self._infrastructure = {}
        self._pool_revision = None

    @property
    def active(self):
        return self._active_context.get()

    @active.setter
    def active(self, value):
        self._active_context.set(value)

    def begin(self, task_id, round_id=0):
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Invalid task ID")
        if isinstance(round_id, bool) or not isinstance(round_id, int) or round_id < 0:
            raise ValueError("Invalid round ID")
        # Round identity belongs to the case trace, never to its budget scope.
        self.active = f"formal/{task_id}"

    def set_deadline(self, task_id, epoch_seconds):
        """Restore or shorten a task deadline from its durable progress record."""
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Invalid task ID")
        deadline = self._money(epoch_seconds)
        if deadline <= 0:
            raise ValueError("Deadline must be a finite positive epoch")
        self._task_deadlines[task_id] = min(
            deadline, self._task_deadlines.get(task_id, deadline)
        )

    def attach(self, session):
        if self._journal is not None:
            return
        metadata = [r["data"] for r in session.records if r["kind"] == "campaign_metadata"]
        if metadata:
            if len(metadata) != 1 or metadata[0].get("version") != 3:
                raise ValueError("Invalid v3 opening metadata")
            if self._money(metadata[0].get("previous_cost_cny")) != self.previous_cost_cny:
                raise ValueError("Campaign opening cost does not match ledger")
        elif any(r["kind"] in {"budget_checkpoint", "budget_settlement"} for r in session.records):
            raise ValueError("A v3 campaign requires a new ledger with opening metadata")
        # Validate persisted money before CampaignBudget performs conservative
        # recovery. An invalid ledger must never disable a ceiling via NaN.
        for record in session.records:
            if record["kind"] == "budget_settlement":
                self._money(record["data"]["cost_cny"])
            elif record["kind"] == "budget_checkpoint":
                state = record["data"]
                self._money(state.get("total_cost_cny", "0"))
                for value in state.get("campaign_recovered", {}).values():
                    self._money(value)
                for value in state.get("pending", {}).values():
                    self._money(value["cost"])
        before = copy.deepcopy(self._state())
        old_records = self.records
        old_reconciled = self._reconciled
        old_infrastructure = self._infrastructure
        old_limit, old_pool_revision = self.cost_limit_cny, self._pool_revision
        try:
            with session.transaction():
                if not metadata:
                    session.append("campaign_metadata", {
                        "version": 3,
                        "previous_cost_cny": str(self.previous_cost_cny),
                        "v3_cost_limit_cny": str(self.cost_limit_cny),
                        "combined_cost_limit_cny": "40",
                        "task_cost_limit_cny": ".95",
                        "task_call_limit": 120,
                        "cost_basis": "conservative_ceiling_not_provider_invoice",
                    }, True)
                super().attach(session)
                # Base recovery reads original append-only settlement events.
                # Project effective task costs without modifying those events
                # or crediting the already-adjusted checkpoint total twice.
                self.records = copy.deepcopy(self.records)
                self._reconciled = {}
                self._infrastructure = {}
                self._pool_revision = None
                self.cost_limit_cny = min(Decimal("33.25"), Decimal("40") - self.previous_cost_cny)
                for event in session.records:
                    if event["kind"] == "budget_reconciliation":
                        self._project_reconciliation(event["data"])
                    elif event["kind"] == "budget_infrastructure":
                        self._project_infrastructure(event["data"])
                    elif event["kind"] == "campaign_pool_revision":
                        self._project_pool_revision(event["data"])
        except BaseException:
            self._restore(before)
            self.recovered = {
                k: Decimal(v) for k, v in before["campaign_recovered"].items()
            }
            self.recovered_calls = before["campaign_recovered_calls"]
            self.records = old_records
            self._reconciled = old_reconciled
            self._infrastructure = old_infrastructure
            self.cost_limit_cny, self._pool_revision = old_limit, old_pool_revision
            self._journal = None
            raise

    def _project_reconciliation(self, event):
        reservation, source_hash = event["reservation"], event["source_hash"]
        if not isinstance(source_hash, str) or not source_hash:
            raise ValueError("Reconciliation requires usage evidence source hash")
        old = self._money(event["old_cost_cny"])
        new = self._money(event["new_cost_cny"])
        if new > old or Decimal(event["delta_cny"]) != new - old:
            raise ValueError("Invalid reconciliation delta")
        key = (reservation, source_hash)
        if key in self._reconciled:
            if self._reconciled[key] != new:
                raise ValueError("Reconciliation evidence already used for another cost")
            return
        record = next((r for r in self.records if r["reservation"] == reservation), None)
        if record is None or record["tokens"] is None:
            raise ValueError("Reconciliation requires a settlement with known usage")
        if self._money(record["cost_cny"]) != old:
            raise ValueError("Reconciliation does not match prior effective cost")
        record["cost_cny"] = str(new)
        self._reconciled[key] = new

    async def reconcile(self, reservation, new_cost_cny, source_hash):
        """Credit known usage using evidence without rewriting its settlement.

        The caller supplies a hash of the approved pricing and usage evidence.
        Unknown outcomes retain their conservative ceilings and physical calls.
        """
        new = self._money(new_cost_cny)
        if not isinstance(source_hash, str) or not source_hash:
            raise ValueError("Reconciliation requires usage evidence source hash")
        async with self._campaign_lock:
            async with self._lock:
                key = (reservation, source_hash)
                if key in self._reconciled:
                    if self._reconciled[key] != new:
                        raise ValueError("Reconciliation evidence already used for another cost")
                    return
                record = next(
                    (r for r in self.records if r["reservation"] == reservation), None
                )
                if record is None or record["tokens"] is None:
                    raise ValueError("Reconciliation requires a settlement with known usage")
                old = self._money(record["cost_cny"])
                if new > old:
                    raise ValueError("Reconciliation cannot increase cost")
                if self._journal is None:
                    raise ValueError("Reconciliation requires an attached durable ledger")
                event = {
                    "reservation": reservation,
                    "scope": record["scope"],
                    "old_cost_cny": str(old),
                    "new_cost_cny": str(new),
                    "delta_cny": str(new - old),
                    "source_hash": source_hash,
                }
                before_total, before_records = self.total_cost_cny, self.records
                self.total_cost_cny += new - old
                self.records = [
                    {**row, "cost_cny": str(new)} if row is record else row
                    for row in self.records
                ]
                try:
                    with self._journal.transaction():
                        self._journal.append("budget_reconciliation", event, True)
                        self._checkpoint()
                except BaseException:
                    self.total_cost_cny, self.records = before_total, before_records
                    raise
                self._reconciled[key] = new

    def task_total(self, task_id):
        return self.task_cost(f"formal/{task_id}")

    def _project_pool_revision(self, event):
        if not isinstance(event.get("source_hash"), str) or not event["source_hash"]:
            raise ValueError("Pool revision requires approval evidence source hash")
        if self._pool_revision is not None:
            if self._pool_revision != event:
                raise ValueError("Campaign pool already revised with different evidence")
            return
        old, new = self._money(event["old_limit_cny"]), self._money(event["new_limit_cny"])
        if old != self.cost_limit_cny or new != Decimal("40") - self.previous_cost_cny or new < old:
            raise ValueError("Pool revision exceeds approved remaining campaign allowance")
        self.cost_limit_cny = new
        self._pool_revision = dict(event)

    async def enable_infrastructure_pool(self, source_hash):
        """Activate the approved remaining CNY 40 pool without changing charges."""
        async with self._campaign_lock:
            async with self._lock:
                if self._journal is None:
                    raise ValueError("Pool revision requires an attached durable ledger")
                event = {
                    "old_limit_cny": str(min(Decimal("33.25"), Decimal("40") - self.previous_cost_cny)),
                    "new_limit_cny": str(Decimal("40") - self.previous_cost_cny),
                    "source_hash": source_hash,
                }
                before_limit, before_revision = self.cost_limit_cny, self._pool_revision
                try:
                    self._project_pool_revision(event)
                    if before_revision is not None:
                        return
                    with self._journal.transaction():
                        self._journal.append("campaign_pool_revision", event, True)
                        self._checkpoint()
                except BaseException:
                    self.cost_limit_cny, self._pool_revision = before_limit, before_revision
                    raise

    def _project_infrastructure(self, event):
        reservation = event["reservation"]
        if event.get("reason") not in {"APIConnectionError", "APITimeoutError"}:
            raise ValueError("Unsupported infrastructure failure reason")
        if not isinstance(event.get("source_hash"), str) or not event["source_hash"]:
            raise ValueError("Infrastructure classification requires evidence source hash")
        record = next((r for r in self.records if r["reservation"] == reservation), None)
        if record is None or record["tokens"] is not None or record["cost_known"] is not False:
            raise ValueError("Infrastructure classification requires a settled unknown usage")
        if event.get("scope") != record["scope"] or self._money(event["cost_cny"]) != self._money(record["cost_cny"]):
            raise ValueError("Infrastructure classification does not match settlement")
        previous = self._infrastructure.get(reservation)
        if previous is not None and previous != event:
            raise ValueError("Reservation already has different infrastructure evidence")
        self._infrastructure[reservation] = dict(event)

    async def classify_infrastructure(self, reservation, reason, source_hash):
        """Exclude proven connection failures only from task execution money.

        Conservative charges remain in the root ledger, gross task totals and
        physical-call counters. The caller supplies the failure evidence hash.
        """
        async with self._campaign_lock:
            async with self._lock:
                record = next(
                    (r for r in self.records if r["reservation"] == reservation), None
                )
                if record is None:
                    raise ValueError("Infrastructure classification requires a settlement")
                event = {
                    "reservation": reservation,
                    "scope": record["scope"],
                    "cost_cny": record["cost_cny"],
                    "reason": reason,
                    "source_hash": source_hash,
                }
                before = dict(self._infrastructure)
                try:
                    self._project_infrastructure(event)
                    if reservation in before:
                        return
                    if self._journal is None:
                        raise ValueError("Infrastructure classification requires a durable ledger")
                    with self._journal.transaction():
                        self._journal.append("budget_infrastructure", event, True)
                        self._checkpoint()
                except BaseException:
                    self._infrastructure = before
                    raise

    def task_infrastructure_total(self, task_id):
        scope = f"formal/{task_id}"
        return sum(
            (self._money(row["cost_cny"]) for row in self.records
             if row["scope"] == scope and row["reservation"] in self._infrastructure),
            Decimal(0),
        )

    def task_execution_total(self, task_id):
        return self.task_total(task_id) - self.task_infrastructure_total(task_id)

    def task_calls(self, task_id):
        scope = f"formal/{task_id}"
        return (
            sum(record["scope"] == scope for record in self.records)
            + sum(record["scope"] == scope for record in self._pending.values())
            + self.recovered_calls.get(scope, 0)
        )

    def spend_total(self):
        return self.total_cost_cny + sum(
            (self._money(record["cost"]) for record in self._pending.values()), Decimal(0)
        )

    def task_remaining(self, task_id):
        return max(Decimal(0), Decimal(".95") - self.task_execution_total(task_id))

    async def _reserve(self, amount, purpose="main", *, estimated_cost_cny=None, scope=None):
        # CampaignBudget.reserve holds its shared lock across the checks and
        # root reservation. ContextVar survives wait_for-created child tasks.
        active = self.active
        if active is None:
            raise ValueError("No active task")
        if estimated_cost_cny is None:
            raise BudgetExceeded("Verified ceiling pricing required")
        cost = self._money(estimated_cost_cny)
        task_id = active.removeprefix("formal/")
        deadline = self._task_deadlines.get(task_id)
        if deadline is not None and time.time() >= deadline:
            raise BudgetExceeded("Task deadline exhausted")
        if self.task_calls(task_id) >= 120 or self.task_execution_total(task_id) + cost > Decimal(".95"):
            raise BudgetExceeded("Campaign task/call limit exhausted")
        # Keep the root monetary journal and rollback semantics, bypassing v1's
        # phase limits and its different task and physical-call ceilings.
        return await Budget.reserve(
            self, amount, purpose, estimated_cost_cny=cost, scope=active
        )

    async def settle(self, reservation, actual, *, actual_cost_cny=None, cost_known=None):
        # Budget.settle pops a reservation before validating its monetary cost.
        # Prevalidation keeps rejected settlements available for retry/recovery.
        if actual_cost_cny is not None:
            actual_cost_cny = self._money(actual_cost_cny)
            held = self._pending.get(reservation)
            if held is not None and actual_cost_cny > self._money(held["cost"]):
                raise ValueError("Actual cost exceeds verified reservation ceiling")
        return await super().settle(
            reservation, actual, actual_cost_cny=actual_cost_cny, cost_known=cost_known
        )

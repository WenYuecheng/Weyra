"""Offline coverage for the v2 campaign's durable, two-stage budget."""

import asyncio
from decimal import Decimal

import pytest

from benchmarks.polyglot.budget_v2 import FeedbackBudget
from weyra.core.state import BudgetExceeded
from weyra.session import SessionStore


def test_four_concurrent_tasks_keep_attempt_attribution():
    async def run():
        budget = FeedbackBudget()

        async def work(index):
            task = f"python/{index}"
            budget.begin(task)
            await asyncio.sleep(0)
            first = await budget.reserve(1, estimated_cost_cny="1", scope="random-model-context")
            budget.begin(task, attempt="repair")
            await asyncio.sleep(0)
            repair = await budget.reserve(1, estimated_cost_cny=".5")
            await budget.settle(first, 1, actual_cost_cny=".8")
            await budget.settle(repair, 1, actual_cost_cny=".4")
            assert budget.active == f"formal/{task}/repair"

        await asyncio.gather(*(work(index) for index in range(4)))
        assert budget.active is None
        assert budget.total_cost_cny == Decimal("4.8")
        for index in range(4):
            task = f"python/{index}"
            assert budget.stage_total(task, "first") == Decimal(".8")
            assert budget.stage_total(task, "repair") == Decimal(".4")
            assert budget.task_total(task) == Decimal("1.2")

    asyncio.run(run())


def test_stage_caps_cannot_borrow_and_task_total_is_combined():
    async def run():
        budget = FeedbackBudget()
        budget.begin("python/x", attempt="repair")
        await budget.reserve(1, estimated_cost_cny=".5")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny=".001")
        budget.begin("python/x")
        await budget.reserve(1, estimated_cost_cny="1")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny=".001")
        assert budget.task_total("python/x") == Decimal("1.5")
        assert budget.task_total("python/xy") == 0
        assert budget.task_total("python/x/first") == 0

    asyncio.run(run())


def test_root_and_phase_caps_are_nonborrowing():
    async def run():
        budget = FeedbackBudget()
        assert budget.cost_limit_cny == Decimal("40")
        for index in range(36):
            budget.begin(f"python/{index}")
            await budget.reserve(1, estimated_cost_cny="1")
        budget.begin("python/overflow")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny=".01")
        budget.begin("health", phase="check")
        await budget.reserve(1, estimated_cost_cny="4")
        budget.begin("other-health", phase="check")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny=".01")
        assert budget.phase_cost("formal") == 36
        assert budget.phase_cost("check") == 4
        assert sum(Decimal(row["cost"]) for row in budget._pending.values()) == 40

        fresh = FeedbackBudget()
        fresh.begin("health", phase="check")
        with pytest.raises(BudgetExceeded):
            await fresh.reserve(1, estimated_cost_cny="4.01")
        fresh.begin("x")
        fresh.total_cost_cny = Decimal("39.5")
        with pytest.raises(BudgetExceeded):
            await fresh.reserve(1, estimated_cost_cny=".51")

    asyncio.run(run())


def test_eighty_physical_calls_are_combined_across_attempts():
    async def run():
        budget = FeedbackBudget()
        for attempt in ("first", "repair"):
            budget.begin("javascript/x", attempt=attempt)
            for _ in range(40):
                reservation = await budget.reserve(1, estimated_cost_cny="0")
                await budget.settle(reservation, None)
                await budget.settle(reservation, None)
        for attempt in ("first", "repair"):
            budget.begin("javascript/x", attempt=attempt)
            with pytest.raises(BudgetExceeded):
                await budget.reserve(1, estimated_cost_cny="0")
        assert budget.calls == len(budget.records) == budget.unknown_calls == 80
        budget.begin("javascript/other")
        await budget.reserve(1, estimated_cost_cny="0")

    asyncio.run(run())


def test_pending_calls_count_toward_combined_eighty_call_limit():
    async def run():
        budget = FeedbackBudget()
        for attempt in ("first", "repair"):
            budget.begin("x", attempt=attempt)
            for _ in range(40):
                await budget.reserve(1, estimated_cost_cny="0")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny="0")

    asyncio.run(run())


def test_restart_preserves_settled_and_unknown_pending_stage_costs(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v2-budget")
        budget = FeedbackBudget()
        budget.attach(store)
        budget.begin("python/x")
        first = await budget.reserve(1, estimated_cost_cny=".6")
        await budget.settle(first, 1, actual_cost_cny=".4")
        await budget.reserve(1, estimated_cost_cny=".6")
        budget.begin("python/x", attempt="repair")
        await budget.reserve(1, estimated_cost_cny=".5")
        store.close()

        for _ in range(2):
            store = SessionStore(project=tmp_path, session_id="v2-budget")
            budget = FeedbackBudget()
            budget.attach(store)
            assert budget.total_cost_cny == budget.task_total("python/x") == Decimal("1.5")
            assert budget.stage_total("python/x", "first") == 1
            assert budget.stage_total("python/x", "repair") == Decimal(".5")
            assert budget.calls == 3
            assert budget.unknown_calls == 2
            assert not budget._pending
            for attempt in ("first", "repair"):
                budget.begin("python/x", attempt=attempt)
                with pytest.raises(BudgetExceeded):
                    await budget.reserve(1, estimated_cost_cny=".01")
            store.close()

    asyncio.run(run())


def test_restart_preserves_combined_call_limit(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v2-budget")
        budget = FeedbackBudget()
        budget.attach(store)
        for attempt in ("first", "repair"):
            budget.begin("x", attempt=attempt)
            for _ in range(40):
                await budget.reserve(1, estimated_cost_cny="0")
        store.close()
        store = SessionStore(project=tmp_path, session_id="v2-budget")
        budget = FeedbackBudget()
        budget.attach(store)
        budget.begin("x")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny="0")
        store.close()

    asyncio.run(run())


def test_concurrent_stage_reservations_are_atomic():
    async def run():
        budget = FeedbackBudget()
        budget.begin("python/race")
        await budget._lock.acquire()
        first = asyncio.create_task(budget.reserve(1, estimated_cost_cny=".6"))
        second = asyncio.create_task(budget.reserve(1, estimated_cost_cny=".6"))
        await asyncio.sleep(0)
        budget._lock.release()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
        assert budget.stage_total("python/race", "first") == Decimal(".6")

    asyncio.run(run())


@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
def test_failed_checkpoint_leaks_neither_money_calls_nor_attribution(monkeypatch, error):
    async def run():
        budget = FeedbackBudget()
        budget.begin("python/x", attempt="repair")
        checkpoint = budget._checkpoint

        def fail():
            raise error("checkpoint failed")

        monkeypatch.setattr(budget, "_checkpoint", fail)
        with pytest.raises(error):
            await budget.reserve(1, estimated_cost_cny=".5")
        assert not budget._pending
        assert budget.calls == budget.task_total("python/x") == 0
        monkeypatch.setattr(budget, "_checkpoint", checkpoint)
        reservation = await budget.reserve(1, estimated_cost_cny=".5")
        monkeypatch.setattr(budget, "_checkpoint", fail)
        with pytest.raises(error):
            await budget.settle(reservation, 1, actual_cost_cny=".2")
        assert budget.stage_total("python/x", "repair") == Decimal(".5")
        assert budget.total_cost_cny == 0
        assert not budget.records
        monkeypatch.setattr(budget, "_checkpoint", checkpoint)
        await budget.settle(reservation, 1, actual_cost_cny=".2")
        await budget.settle(reservation, 1, actual_cost_cny=".2")
        assert budget.stage_total("python/x", "repair") == Decimal(".2")
        assert budget.total_cost_cny == Decimal(".2")

    asyncio.run(run())


def test_root_attachment_ignores_per_case_attach(tmp_path):
    root = SessionStore(project=tmp_path / "root")
    case = SessionStore(project=tmp_path / "case")
    try:
        budget = FeedbackBudget()
        budget.attach(root)
        count = len(root.records)
        budget.attach(case)
        assert budget._journal is root
        assert len(root.records) == count
        assert not any(record["kind"] == "budget_checkpoint" for record in case.records)
    finally:
        case.close()
        root.close()


def test_failed_durable_settlement_preserves_pending_stage_on_restart(tmp_path, monkeypatch):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v2-budget")
        budget = FeedbackBudget()
        budget.attach(store)
        budget.begin("x", attempt="repair")
        reservation = await budget.reserve(1, estimated_cost_cny=".5")
        checkpoint = budget._checkpoint

        def fail():
            raise OSError("checkpoint failed")

        monkeypatch.setattr(budget, "_checkpoint", fail)
        with pytest.raises(OSError):
            await budget.settle(reservation, 1, actual_cost_cny=".2")
        assert not any(record["kind"] == "budget_settlement" for record in store.records)
        monkeypatch.setattr(budget, "_checkpoint", checkpoint)
        store.close()

        store = SessionStore(project=tmp_path, session_id="v2-budget")
        budget = FeedbackBudget()
        budget.attach(store)
        assert budget.stage_total("x", "repair") == Decimal(".5")
        assert budget.total_cost_cny == Decimal(".5")
        assert budget.unknown_calls == 1
        budget.begin("x", attempt="repair")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny=".01")
        store.close()

    asyncio.run(run())


def test_begin_validates_context_and_reservations_require_pricing():
    async def run():
        budget = FeedbackBudget()
        with pytest.raises(ValueError, match="active"):
            await budget.reserve(1, estimated_cost_cny="1")
        for kwargs in ({"phase": "invalid"}, {"attempt": "third"}):
            with pytest.raises(ValueError):
                budget.begin("x", **kwargs)
        budget.begin("x")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1)
        with pytest.raises(ValueError):
            await budget.reserve(1, estimated_cost_cny="NaN")

    asyncio.run(run())

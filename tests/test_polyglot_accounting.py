from decimal import Decimal
import asyncio
import pytest
from benchmarks.polyglot.accounting import CampaignBudget, summarize
from weyra.core.state import BudgetExceeded
from weyra.session import SessionStore


def test_task_and_phase_caps_persist_unknown_requests(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path)
        b = CampaignBudget()
        b.attach(store)
        b.begin("python/x", "formal")
        r = await b.reserve(1, estimated_cost_cny="2")
        with pytest.raises(BudgetExceeded):
            await b.reserve(1, estimated_cost_cny=".5")
        await b.settle(r, 1, actual_cost_cny="1", cost_known=False)
        r = await b.reserve(1, estimated_cost_cny="1.4")
        state = b._state()
        session_id = store.session_id
        store.close()
        store = SessionStore(project=tmp_path, session_id=session_id)
        b = CampaignBudget()
        b.attach(store)
        b.begin("python/x", "formal")
        with pytest.raises(BudgetExceeded):
            await b.reserve(1, estimated_cost_cny=".01")
        assert b.total_cost_cny == Decimal("2.4")
        assert b.task_cost("formal/python/x") == Decimal("2.4")
        assert state["pending"]
        store.close()

    asyncio.run(run())


def test_fixed_denominator_and_no_pass_cost():
    s = summarize([], {"formal": "0", "check": "0"})
    assert s["passed"] == 0 and s["denominator"] == 40
    assert s["cost_per_passed_task_cny"] is None


def test_concurrent_reservations_cannot_overrun_task_cap():
    async def run():
        b = CampaignBudget()
        b.begin("python/race")
        await b._lock.acquire()
        first = asyncio.create_task(b.reserve(1, estimated_cost_cny="1.4"))
        second = asyncio.create_task(b.reserve(1, estimated_cost_cny="1.4"))
        await asyncio.sleep(0)
        b._lock.release()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert sum(isinstance(r, BudgetExceeded) for r in results) == 1
        assert b.task_cost("formal/python/race") == Decimal("1.4")

    asyncio.run(run())

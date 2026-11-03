import asyncio
from decimal import Decimal

import pytest

from benchmarks.final_delivery_eval import StageBudget, estimate_remaining
from weyra.runtime_state import Budget, BudgetExceeded


def test_stage_budget_checks_shared_reservations_before_spending():
    async def check():
        root = Budget(cost_limit_cny='100')
        stage = StageBudget(root, start_cost='0', limit='20')
        first = await stage.reserve(100, estimated_cost_cny='19')
        with pytest.raises(BudgetExceeded):
            await stage.reserve(100, estimated_cost_cny='2')
        assert root.calls == 1
        await stage.settle(first, None)
        assert root.total_cost_cny == Decimal('19')
    asyncio.run(check())


def test_estimate_preserves_all_220_planned_executions():
    estimate = estimate_remaining()
    assert estimate['new_final_executions'] == 220
    assert estimate['fixed_final_slots'] == 294
    assert estimate['new_skill_cap_cny'] == '20'
    assert estimate['estimated_remaining_cny'] > 20

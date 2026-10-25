import asyncio
from decimal import Decimal
import pytest
from weyra.runtime_state import Budget, BudgetExceeded
from weyra.session import SessionStore


def test_concurrent_cost_reservations_do_not_reuse_remaining_budget(tmp_path):
    async def run():
        budget = Budget(cost_limit_cny="20")
        budget.attach(SessionStore(project=tmp_path))
        first = await budget.reserve(10, estimated_cost_cny="12", scope="a")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(10, estimated_cost_cny="12", scope="b")
        await budget.settle(first, 5, actual_cost_cny="2")
        assert budget.total_cost_cny == Decimal("2")
        second = await budget.reserve(10, estimated_cost_cny="12")
        await budget.settle(second, None)
        assert budget.total_cost_cny == Decimal("14")
        budget._journal.close()

    asyncio.run(run())


def test_scope_tokens_and_unknown_price_are_fail_closed(tmp_path):
    async def run():
        budget = Budget(token_limit=100, cost_limit_cny="20")
        budget.set_scope_limit("child", 12)
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1)
        r = await budget.reserve(10, estimated_cost_cny="1", scope="child")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(3, estimated_cost_cny="1", scope="child")
        await budget.settle(r, 8, actual_cost_cny="0.5")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(5, estimated_cost_cny="1", scope="child")

    asyncio.run(run())


def test_cost_checkpoint_resume_retains_unknown_precharge(tmp_path):
    async def run():
        session = SessionStore(project=tmp_path)
        sid = session.session_id
        b = Budget(cost_limit_cny="20")
        b.attach(session)
        await b.reserve(30, estimated_cost_cny="3")
        session.close()
        resumed = SessionStore(project=tmp_path, session_id=sid)
        b2 = Budget(cost_limit_cny="20")
        b2.attach(resumed)
        assert b2.total_cost_cny == Decimal("3")
        assert b2.total_tokens == 30
        resumed.close()

    asyncio.run(run())


def test_branch_does_not_erase_session_spend_on_resume(tmp_path):
    async def run():
        session = SessionStore(project=tmp_path)
        sid = session.session_id
        budget = Budget(cost_limit_cny="20")
        budget.attach(session)
        before = session.leaf
        r = await budget.reserve(1, estimated_cost_cny="15")
        await budget.settle(r, 1, actual_cost_cny="15")
        session.branch(before)
        session.close()
        resumed = SessionStore(project=tmp_path, session_id=sid)
        second = Budget(cost_limit_cny="20")
        second.attach(resumed)
        assert second.total_cost_cny == Decimal("15")
        with pytest.raises(BudgetExceeded):
            await second.reserve(1, estimated_cost_cny="15")
        resumed.close()

    asyncio.run(run())


def test_heuristic_input_is_not_a_monetary_upper_bound(tmp_path):
    from weyra.contracts import ModelInfo, ModelResponse, Message, PreparedRequest, Usage
    from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
    from weyra.runtime_state import RunContext

    async def run():
        provider = ScriptedProvider([ModelResponse("x", usage=Usage(2000, 1))])
        registry = ProviderRegistry()
        registry.register("x", provider)
        model = ModelInfo("x", "x", 10000, 1)
        runtime = ModelRuntime(registry, {"main": model})
        runtime.pricing = {
            "x/x": {"currency": "CNY", "input_per_million": "10000", "output_per_million": "10000"}
        }
        session = SessionStore(project=tmp_path)
        budget = Budget(cost_limit_cny="20")
        budget.attach(session)
        with pytest.raises(BudgetExceeded):
            await runtime.complete(
                PreparedRequest(model, "", (Message("user", "x" * 2000),)),
                RunContext(tmp_path, session, budget),
            )
        assert not provider.requests
        session.close()

    asyncio.run(run())


def test_missing_cache_classification_is_not_exact_cost():
    from weyra.pricing import Price
    from weyra.contracts import Usage

    price = Price.from_dict(
        {"input_per_million": "1", "output_per_million": "2", "cache_write_per_million": "3"}
    )
    assert price.actual(Usage(1000, 1)) is None
    assert price.upper_bound(Usage(1000, 1)) == Decimal(".003002")


def test_reported_cache_read_without_tariff_is_unknown():
    from weyra.pricing import Price
    from weyra.contracts import Usage

    price = Price.from_dict({"input_per_million": "1", "output_per_million": "2"})
    assert price.actual(Usage(1000, 1, 100)) is None
    assert price.upper_bound(Usage(1000, 1, 100)) is None


def test_dynamic_price_ceiling_is_not_claimed_as_exact_charge():
    from weyra.pricing import Price
    from weyra.contracts import Usage

    price = Price.from_dict(
        {"input_per_million": "2", "output_per_million": "4", "cost_basis": "ceiling"}
    )
    assert price.actual(Usage(1000, 10)) is None
    assert price.upper_bound(Usage(1000, 10)) == Decimal(".00204")


def test_cache_write_included_in_input_is_an_explicit_billing_policy():
    from weyra.pricing import Price
    from weyra.contracts import Usage

    price = Price.from_dict(
        {
            "input_per_million": "1",
            "output_per_million": "2",
            "cache_read_per_million": ".1",
            "cache_write_billing": "included",
        }
    )
    assert price.actual(Usage(1000, 10, 200)) == Decimal(".00084")
    assert Price.from_dict(price.to_dict()) == price
    with pytest.raises(ValueError):
        Price.from_dict({**price.to_dict(), "cache_write_per_million": "2"})

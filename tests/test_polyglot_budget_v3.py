"""Offline checks for the durable shared-round v3 budget."""

import asyncio
from decimal import Decimal

import pytest

from benchmarks.polyglot.budget_v3 import IterativeBudget
from weyra.core.state import BudgetExceeded
from weyra.session import SessionStore


def test_four_workers_and_wait_for_preserve_stable_task_scopes():
    async def run():
        budget = IterativeBudget()

        async def work(index):
            task = f"python/{index}"
            for round_id in range(3):
                budget.begin(task, round_id)
                await asyncio.sleep(0)
                reservation = await asyncio.wait_for(
                    budget.reserve(1, estimated_cost_cny=".3", scope="random"), 1
                )
                await budget.settle(reservation, 1, actual_cost_cny=".2")
                assert budget.active == f"formal/{task}"
            assert budget.task_total(task) == Decimal(".6")
            assert budget.task_calls(task) == 3
            assert budget.task_remaining(task) == Decimal(".35")

        await asyncio.gather(*(work(index) for index in range(4)))
        assert budget.active is None
        assert budget.spend_total() == Decimal("2.4")
        assert {r["scope"] for r in budget.records} == {
            f"formal/python/{index}" for index in range(4)
        }

    asyncio.run(run())


def test_all_rounds_share_task_allowance_with_no_round_subcap():
    async def run():
        budget = IterativeBudget()
        budget.begin("python/x", 0)
        await budget.reserve(1, estimated_cost_cny=".8")
        budget.begin("python/x", 9)
        await budget.reserve(1, estimated_cost_cny=".15")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny=".0001")
        assert budget.task_total("python/x") == Decimal(".95")
        assert budget.task_total("python/xy") == 0
        assert budget.task_total("python/x/0") == 0
        assert budget.task_remaining("python/x") == 0

    asyncio.run(run())


@pytest.mark.parametrize("previous,limit", [("0", "33.25"), ("5.8554306629531", "33.25"), ("7", "33")])
def test_root_enforces_v3_safety_margin_and_combined_forty(previous, limit):
    async def run():
        budget = IterativeBudget(previous_cost_cny=previous)
        assert budget.cost_limit_cny == Decimal(limit)
        remaining = Decimal(limit)
        index = 0
        while remaining:
            budget.begin(str(index))
            cost = min(remaining, Decimal(".95"))
            await budget.reserve(1, estimated_cost_cny=cost)
            remaining -= cost
            index += 1
        budget.begin("overflow")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny=".00001")
        assert budget.spend_total() == Decimal(limit)
        assert budget.spend_total() + budget.previous_cost_cny <= 40

    asyncio.run(run())


def test_120_physical_calls_include_auxiliary_retries_and_pending():
    async def run():
        budget = IterativeBudget()
        for index in range(120):
            budget.begin("x", index // 40)
            reservation = await budget.reserve(
                1, ("main", "compression", "memory", "parse_retry", "transient_retry")[index % 5],
                estimated_cost_cny="0",
            )
            if index % 2:
                await budget.settle(reservation, None)
                await budget.settle(reservation, None)
        assert budget.task_calls("x") == 120
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny="0")
        budget.begin("other")
        await budget.reserve(1, estimated_cost_cny="0")
        assert budget.task_calls("other") == 1

    asyncio.run(run())


def test_restart_preserves_known_pending_costs_calls_and_opening_metadata(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        budget = IterativeBudget()
        budget.attach(store)
        budget.begin("x")
        reservation = await budget.reserve(1, estimated_cost_cny=".4")
        await budget.settle(reservation, 1, actual_cost_cny=".2")
        budget.begin("x", 1)
        for _ in range(119):
            await budget.reserve(1, estimated_cost_cny=".001")
        store.close()
        for _ in range(2):
            store = SessionStore(project=tmp_path, session_id="v3")
            budget = IterativeBudget()
            budget.attach(store)
            assert budget.spend_total() == budget.task_total("x") == Decimal(".319")
            assert budget.task_calls("x") == budget.calls == 120
            assert budget.unknown_calls == 119
            assert len(budget.records) == 1
            metadata = [r for r in store.records if r["kind"] == "campaign_metadata"]
            assert len(metadata) == 1
            assert metadata[0]["data"]["previous_cost_cny"] == "5.8554306629531"
            budget.begin("x", 2)
            with pytest.raises(BudgetExceeded):
                await budget.reserve(1, estimated_cost_cny="0")
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
def test_checkpoint_failure_rolls_back_reservation_and_settlement(monkeypatch, error):
    async def run():
        budget = IterativeBudget()
        budget.begin("x")
        checkpoint = budget._checkpoint

        def fail():
            raise error("checkpoint failed")

        monkeypatch.setattr(budget, "_checkpoint", fail)
        with pytest.raises(error):
            await budget.reserve(1, estimated_cost_cny=".5")
        assert budget.calls == budget.task_calls("x") == budget.spend_total() == 0
        monkeypatch.setattr(budget, "_checkpoint", checkpoint)
        reservation = await budget.reserve(1, estimated_cost_cny=".5")
        monkeypatch.setattr(budget, "_checkpoint", fail)
        with pytest.raises(error):
            await budget.settle(reservation, 1, actual_cost_cny=".2")
        assert budget.spend_total() == Decimal(".5")
        assert not budget.records
        monkeypatch.setattr(budget, "_checkpoint", checkpoint)
        await budget.settle(reservation, 1, actual_cost_cny=".2")
        assert budget.spend_total() == Decimal(".2")

    asyncio.run(run())


def test_root_attaches_once_and_opening_mismatch_rejected(tmp_path):
    root_store = SessionStore(project=tmp_path / "root", session_id="v3")
    case_store = SessionStore(project=tmp_path / "case")
    try:
        budget = IterativeBudget()
        budget.attach(root_store)
        budget.attach(case_store)
        assert budget._journal is root_store
        assert not any(r["kind"] == "budget_checkpoint" for r in case_store.records)
    finally:
        case_store.close()
        root_store.close()
    root_store = SessionStore(project=tmp_path / "root", session_id="v3")
    try:
        with pytest.raises(ValueError, match="opening"):
            IterativeBudget(previous_cost_cny="0").attach(root_store)
    finally:
        root_store.close()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "bad", True, None])
def test_invalid_opening_cost_rejected(value):
    with pytest.raises(ValueError):
        IterativeBudget(previous_cost_cny=value)


def test_invalid_context_and_prices_cannot_consume_or_erase_budget():
    async def run():
        budget = IterativeBudget()
        with pytest.raises(ValueError, match="active"):
            await budget.reserve(1, estimated_cost_cny=".1")
        for task in ("", None, 1):
            with pytest.raises(ValueError):
                budget.begin(task)
        for round_id in (-1, True, "1"):
            with pytest.raises(ValueError):
                budget.begin("x", round_id)
        budget.begin("x")
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1)
        for value in ("NaN", "Infinity", "-1"):
            with pytest.raises(ValueError):
                await budget.reserve(1, estimated_cost_cny=value)
        reservation = await budget.reserve(1, estimated_cost_cny=".5")
        for value in ("NaN", "Infinity", "-1"):
            with pytest.raises(ValueError):
                await budget.settle(reservation, 1, actual_cost_cny=value)
            assert budget.spend_total() == Decimal(".5")
        await budget.settle(reservation, 1, actual_cost_cny=".2")
        assert budget.spend_total() == Decimal(".2")

    asyncio.run(run())


def test_concurrent_reservations_cannot_oversubscribe_task():
    async def run():
        budget = IterativeBudget()
        budget.begin("x")
        results = await asyncio.gather(
            *(budget.reserve(1, estimated_cost_cny=".5") for _ in range(4)),
            return_exceptions=True,
        )
        assert sum(isinstance(result, BudgetExceeded) for result in results) == 3
        assert budget.task_calls("x") == 1
        assert budget.task_total("x") == Decimal(".5")

    asyncio.run(run())


def test_failed_durable_settlement_keeps_conservative_charge_after_restart(tmp_path, monkeypatch):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        budget = IterativeBudget()
        budget.attach(store)
        budget.begin("x")
        reservation = await budget.reserve(1, estimated_cost_cny=".95")

        def fail():
            raise OSError("checkpoint failed")

        monkeypatch.setattr(budget, "_checkpoint", fail)
        with pytest.raises(OSError):
            await budget.settle(reservation, 1, actual_cost_cny=".2")
        assert not any(r["kind"] == "budget_settlement" for r in store.records)
        store.close()
        store = SessionStore(project=tmp_path, session_id="v3")
        try:
            recovered = IterativeBudget()
            recovered.attach(store)
            assert recovered.task_total("x") == recovered.spend_total() == Decimal(".95")
            assert recovered.task_calls("x") == recovered.unknown_calls == 1
            recovered.begin("x", 12)
            with pytest.raises(BudgetExceeded):
                await recovered.reserve(1, estimated_cost_cny=".001")
        finally:
            store.close()

    asyncio.run(run())


def test_settlement_cannot_exceed_verified_reservation_ceiling():
    async def run():
        budget = IterativeBudget()
        budget.begin("x")
        reservation = await budget.reserve(1, estimated_cost_cny=".95")
        with pytest.raises(ValueError, match="ceiling"):
            await budget.settle(reservation, 1, actual_cost_cny=".951")
        assert budget.task_total("x") == budget.spend_total() == Decimal(".95")
        await budget.settle(reservation, 1, actual_cost_cny=".8")
        await budget.settle(reservation, 1, actual_cost_cny=".8")
        assert budget.task_calls("x") == 1
        assert budget.spend_total() == Decimal(".8")

    asyncio.run(run())


def test_deadline_blocks_every_request_after_expiry(monkeypatch):
    async def run():
        budget = IterativeBudget()
        budget.begin("x")
        budget.set_deadline("x", 1000)
        monkeypatch.setattr("benchmarks.polyglot.budget_v3.time.time", lambda: 999)
        await budget.reserve(1, estimated_cost_cny=".1")
        monkeypatch.setattr("benchmarks.polyglot.budget_v3.time.time", lambda: 1000)
        for purpose in ("main", "compression", "memory", "parse_retry", "transient_retry"):
            with pytest.raises(BudgetExceeded, match="deadline"):
                await budget.reserve(1, purpose, estimated_cost_cny="0")
        assert budget.task_calls("x") == 1
        budget.begin("other")
        await budget.reserve(1, estimated_cost_cny="0")

    asyncio.run(run())


def test_deadline_cannot_extend_and_can_shorten(monkeypatch):
    async def run():
        budget = IterativeBudget()
        budget.begin("x", 0)
        budget.set_deadline("x", 1000)
        budget.set_deadline("x", 2000)
        budget.begin("x", 1)
        monkeypatch.setattr("benchmarks.polyglot.budget_v3.time.time", lambda: 1000)
        with pytest.raises(BudgetExceeded, match="deadline"):
            await budget.reserve(1, estimated_cost_cny="0")
        budget.set_deadline("x", 900)
        monkeypatch.setattr("benchmarks.polyglot.budget_v3.time.time", lambda: 950)
        with pytest.raises(BudgetExceeded, match="deadline"):
            await budget.reserve(1, estimated_cost_cny="0")
        assert budget.task_calls("x") == 0

    asyncio.run(run())


@pytest.mark.parametrize("deadline", [0, -1, float("nan"), float("inf"), True, None, "bad"])
def test_deadline_requires_finite_positive_epoch(deadline):
    with pytest.raises(ValueError):
        IterativeBudget().set_deadline("x", deadline)


def test_reconcile_is_append_only_restores_allowance_and_preserves_calls(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        try:
            budget = IterativeBudget()
            budget.attach(store)
            budget.begin("x")
            reservation = await budget.reserve(1, estimated_cost_cny=".95")
            await budget.settle(reservation, 1, actual_cost_cny=".95", cost_known=False)
            original = [r for r in store.records if r["kind"] == "budget_settlement"][0]
            await budget.reconcile(reservation, ".25", "evidence-a")
            assert budget.spend_total() == budget.task_total("x") == Decimal(".25")
            assert budget.task_remaining("x") == Decimal(".70")
            assert budget.task_calls("x") == budget.calls == 1
            assert original["data"]["cost_cny"] == ".95" or original["data"]["cost_cny"] == "0.95"
            event = [r["data"] for r in store.records if r["kind"] == "budget_reconciliation"][0]
            assert Decimal(event["old_cost_cny"]) == Decimal(".95")
            assert Decimal(event["new_cost_cny"]) == Decimal(".25")
            assert Decimal(event["delta_cny"]) == Decimal("-.70")
            await budget.reserve(1, estimated_cost_cny=".70")
            assert budget.task_total("x") == Decimal(".95")
        finally:
            store.close()

    asyncio.run(run())


def test_reconciliation_restart_and_historical_repeats_never_double_credit(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        budget = IterativeBudget()
        budget.attach(store)
        budget.begin("x")
        reservation = await budget.reserve(1, estimated_cost_cny=".8")
        await budget.settle(reservation, 1, actual_cost_cny=".8")
        await budget.reserve(1, estimated_cost_cny=".1")
        await budget.reconcile(reservation, ".4", "evidence-a")
        await budget.reconcile(reservation, ".2", "evidence-b")
        store.close()
        for _ in range(2):
            store = SessionStore(project=tmp_path, session_id="v3")
            try:
                budget = IterativeBudget()
                budget.attach(store)
                assert budget.spend_total() == budget.task_total("x") == Decimal(".3")
                assert budget.task_calls("x") == budget.calls == 2
                assert budget.unknown_calls == 1
                await budget.reconcile(reservation, ".4", "evidence-a")
                await budget.reconcile(reservation, ".2", "evidence-b")
                assert budget.spend_total() == Decimal(".3")
                assert len([r for r in store.records if r["kind"] == "budget_reconciliation"]) == 2
                original = [r for r in store.records if r["kind"] == "budget_settlement"][0]
                assert Decimal(original["data"]["cost_cny"]) == Decimal(".8")
            finally:
                store.close()

    asyncio.run(run())


@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
def test_failed_reconciliation_rolls_back_event_projection_and_checkpoint(tmp_path, monkeypatch, error):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        budget = IterativeBudget()
        budget.attach(store)
        budget.begin("x")
        reservation = await budget.reserve(1, estimated_cost_cny=".8")
        await budget.settle(reservation, 1, actual_cost_cny=".8")
        checkpoint = budget._checkpoint

        def fail():
            raise error("checkpoint failed")

        monkeypatch.setattr(budget, "_checkpoint", fail)
        with pytest.raises(error):
            await budget.reconcile(reservation, ".2", "evidence")
        assert budget.spend_total() == budget.task_total("x") == Decimal(".8")
        assert not any(r["kind"] == "budget_reconciliation" for r in store.records)
        monkeypatch.setattr(budget, "_checkpoint", checkpoint)
        store.close()
        store = SessionStore(project=tmp_path, session_id="v3")
        try:
            budget = IterativeBudget()
            budget.attach(store)
            assert budget.spend_total() == budget.task_total("x") == Decimal(".8")
            await budget.reconcile(reservation, ".2", "evidence")
            assert budget.spend_total() == Decimal(".2")
        finally:
            store.close()

    asyncio.run(run())


def test_reconciliation_rejects_unknown_usage_and_invalid_or_increasing_costs(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        try:
            budget = IterativeBudget()
            budget.attach(store)
            budget.begin("x")
            known = await budget.reserve(1, estimated_cost_cny=".4")
            await budget.settle(known, 1, actual_cost_cny=".4")
            unknown = await budget.reserve(1, estimated_cost_cny=".2")
            await budget.settle(unknown, None)
            pending = await budget.reserve(1, estimated_cost_cny=".1")
            for reservation in (unknown, pending, "missing"):
                with pytest.raises(ValueError):
                    await budget.reconcile(reservation, "0", "evidence")
            for cost in ("NaN", "Infinity", "-.1", ".401"):
                with pytest.raises(ValueError):
                    await budget.reconcile(known, cost, "evidence")
            for source in (None, "", 1):
                with pytest.raises(ValueError):
                    await budget.reconcile(known, ".2", source)
            await budget.reconcile(known, ".2", "evidence")
            with pytest.raises(ValueError):
                await budget.reconcile(known, ".1", "evidence")
            assert budget.spend_total() == Decimal(".5")
            assert budget.task_calls("x") == 3
            assert budget.unknown_calls == 1
        finally:
            store.close()

    asyncio.run(run())


def test_infrastructure_cost_excludes_only_task_execution_allowance(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        try:
            budget = IterativeBudget()
            budget.attach(store)
            budget.begin("x")
            reservation = await budget.reserve(1, estimated_cost_cny=".8")
            await budget.settle(reservation, None)
            await budget.classify_infrastructure(reservation, "APIConnectionError", "proof")
            assert budget.task_total("x") == budget.spend_total() == Decimal(".8")
            assert budget.task_infrastructure_total("x") == Decimal(".8")
            assert budget.task_execution_total("x") == 0
            assert budget.task_remaining("x") == Decimal(".95")
            assert budget.task_calls("x") == 1
            await budget.reserve(1, estimated_cost_cny=".95")
            assert budget.task_total("x") == Decimal("1.75")
            assert budget.task_execution_total("x") == Decimal(".95")
            with pytest.raises(BudgetExceeded):
                await budget.reserve(1, estimated_cost_cny=".001")
        finally:
            store.close()

    asyncio.run(run())


def test_infrastructure_replays_idempotently_without_crediting_root(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        budget = IterativeBudget()
        budget.attach(store)
        budget.begin("x")
        reservation = await budget.reserve(1, estimated_cost_cny=".6")
        await budget.settle(reservation, None)
        await budget.classify_infrastructure(reservation, "APITimeoutError", "proof")
        await budget.reserve(1, estimated_cost_cny=".2")
        store.close()
        for _ in range(2):
            store = SessionStore(project=tmp_path, session_id="v3")
            try:
                budget = IterativeBudget()
                budget.attach(store)
                await budget.classify_infrastructure(reservation, "APITimeoutError", "proof")
                assert budget.spend_total() == budget.task_total("x") == Decimal(".8")
                assert budget.task_infrastructure_total("x") == Decimal(".6")
                assert budget.task_execution_total("x") == Decimal(".2")
                assert budget.task_calls("x") == budget.calls == 2
                assert budget.unknown_calls == 2
                assert len([r for r in store.records if r["kind"] == "budget_infrastructure"]) == 1
                with pytest.raises(ValueError):
                    await budget.classify_infrastructure(reservation, "APIConnectionError", "proof")
            finally:
                store.close()

    asyncio.run(run())


@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
def test_infrastructure_failure_rolls_back_projection_and_event(tmp_path, monkeypatch, error):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        try:
            budget = IterativeBudget()
            budget.attach(store)
            budget.begin("x")
            reservation = await budget.reserve(1, estimated_cost_cny=".5")
            await budget.settle(reservation, None)
            checkpoint = budget._checkpoint

            def fail():
                raise error("checkpoint failed")

            monkeypatch.setattr(budget, "_checkpoint", fail)
            with pytest.raises(error):
                await budget.classify_infrastructure(reservation, "APIConnectionError", "proof")
            assert budget.task_execution_total("x") == Decimal(".5")
            assert budget.task_infrastructure_total("x") == 0
            assert not any(r["kind"] == "budget_infrastructure" for r in store.records)
            monkeypatch.setattr(budget, "_checkpoint", checkpoint)
            await budget.classify_infrastructure(reservation, "APIConnectionError", "proof")
            assert budget.task_execution_total("x") == 0
        finally:
            store.close()

    asyncio.run(run())


def test_infrastructure_cannot_exclude_known_usage_or_unproven_unknowns(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        budget = IterativeBudget()
        budget.attach(store)
        budget.begin("x")
        known = await budget.reserve(1, estimated_cost_cny=".2")
        await budget.settle(known, 1, actual_cost_cny=".2", cost_known=False)
        unknown = await budget.reserve(1, estimated_cost_cny=".2")
        await budget.settle(unknown, None)
        pending = await budget.reserve(1, estimated_cost_cny=".2")
        for reservation in (known, pending, "missing"):
            with pytest.raises(ValueError):
                await budget.classify_infrastructure(reservation, "APIConnectionError", "proof")
        for reason in ("OSError", "HTTPError", "", None):
            with pytest.raises(ValueError):
                await budget.classify_infrastructure(unknown, reason, "proof")
        for proof in (None, "", 1):
            with pytest.raises(ValueError):
                await budget.classify_infrastructure(unknown, "APIConnectionError", proof)
        await budget.classify_infrastructure(unknown, "APIConnectionError", "proof")
        with pytest.raises(ValueError):
            await budget.reconcile(unknown, "0", "credit")
        store.close()
        store = SessionStore(project=tmp_path, session_id="v3")
        try:
            budget = IterativeBudget()
            budget.attach(store)
            with pytest.raises(ValueError):
                await budget.classify_infrastructure(pending, "APIConnectionError", "proof")
            assert budget.task_infrastructure_total("x") == Decimal(".2")
            assert budget.spend_total() == Decimal(".6")
        finally:
            store.close()

    asyncio.run(run())


def test_infrastructure_still_consumes_global_money_and_120_calls(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        try:
            budget = IterativeBudget(previous_cost_cny="39")
            budget.attach(store)
            budget.begin("x")
            reservation = await budget.reserve(1, estimated_cost_cny=".8")
            await budget.settle(reservation, None)
            await budget.classify_infrastructure(reservation, "APIConnectionError", "proof")
            await budget.reserve(1, estimated_cost_cny=".2")
            with pytest.raises(BudgetExceeded):
                await budget.reserve(1, estimated_cost_cny=".001")
            for _ in range(118):
                reservation = await budget.reserve(1, estimated_cost_cny="0")
                await budget.settle(reservation, None)
                await budget.classify_infrastructure(reservation, "APIConnectionError", "proof")
            assert budget.task_calls("x") == 120
            with pytest.raises(BudgetExceeded):
                await budget.reserve(1, estimated_cost_cny="0")
            assert budget.spend_total() + budget.previous_cost_cny == 40
        finally:
            store.close()

    asyncio.run(run())


def test_infrastructure_pool_activation_is_durable_and_bounded_by_original_forty(tmp_path):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        budget = IterativeBudget()
        budget.attach(store)
        assert budget.cost_limit_cny == Decimal("33.25")
        await budget.enable_infrastructure_pool("approval-proof")
        target = Decimal("40") - budget.previous_cost_cny
        assert budget.cost_limit_cny == target
        await budget.enable_infrastructure_pool("approval-proof")
        assert len([r for r in store.records if r["kind"] == "campaign_pool_revision"]) == 1
        budget.begin("x")
        budget.total_cost_cny = target
        with pytest.raises(BudgetExceeded):
            await budget.reserve(1, estimated_cost_cny=".001")
        budget.total_cost_cny = Decimal(0)
        store.close()
        for _ in range(2):
            store = SessionStore(project=tmp_path, session_id="v3")
            try:
                budget = IterativeBudget()
                budget.attach(store)
                assert budget.cost_limit_cny == target
                assert budget.spend_total() == 0
                await budget.enable_infrastructure_pool("approval-proof")
                metadata = [r["data"] for r in store.records if r["kind"] == "campaign_metadata"]
                assert len(metadata) == 1
                assert metadata[0]["v3_cost_limit_cny"] == "33.25"
                assert len([r for r in store.records if r["kind"] == "campaign_pool_revision"]) == 1
            finally:
                store.close()

    asyncio.run(run())


@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
def test_pool_revision_checkpoint_failure_rolls_back_limit_and_event(tmp_path, monkeypatch, error):
    async def run():
        store = SessionStore(project=tmp_path, session_id="v3")
        try:
            budget = IterativeBudget()
            budget.attach(store)
            checkpoint = budget._checkpoint

            def fail():
                raise error("checkpoint failed")

            monkeypatch.setattr(budget, "_checkpoint", fail)
            with pytest.raises(error):
                await budget.enable_infrastructure_pool("approval-proof")
            assert budget.cost_limit_cny == Decimal("33.25")
            assert not any(r["kind"] == "campaign_pool_revision" for r in store.records)
            monkeypatch.setattr(budget, "_checkpoint", checkpoint)
            await budget.enable_infrastructure_pool("approval-proof")
            assert budget.cost_limit_cny + budget.previous_cost_cny == 40
        finally:
            store.close()

    asyncio.run(run())


def test_pool_revision_requires_evidence_and_durable_ledger(tmp_path):
    async def run():
        budget = IterativeBudget()
        with pytest.raises(ValueError):
            await budget.enable_infrastructure_pool("approval-proof")
        store = SessionStore(project=tmp_path, session_id="v3")
        try:
            budget.attach(store)
            for source in (None, "", 1):
                with pytest.raises(ValueError):
                    await budget.enable_infrastructure_pool(source)
            await budget.enable_infrastructure_pool("approval-proof")
            with pytest.raises(ValueError):
                await budget.enable_infrastructure_pool("different-proof")
        finally:
            store.close()

    asyncio.run(run())

import asyncio
from decimal import Decimal
from benchmarks.polyglot.parallel import ParallelBudget


def test_parallel_scope_isolation_and_shared_root():
    async def run():
        b = ParallelBudget()

        async def task(name):
            b.begin(name)
            await asyncio.sleep(0)
            r = await b.reserve(1, estimated_cost_cny="1")
            await asyncio.sleep(0)
            await b.settle(r, 1, actual_cost_cny=".5")

        await asyncio.gather(task("python/a"), task("javascript/b"))
        assert b.task_cost("formal/python/a") == Decimal(".5")
        assert b.task_cost("formal/javascript/b") == Decimal(".5")
        assert b.total_cost_cny == 1
        assert len({r["scope"] for r in b.records}) == 2

    asyncio.run(run())

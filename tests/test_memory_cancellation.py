import asyncio
import time

import pytest

from weyra.memory import MemoryStore


def test_outer_recall_cancellation_drains_paid_selector(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path)
        memory.create("one", "project fact")
        started, stopped = asyncio.Event(), asyncio.Event()
        child = None

        async def selector(query, records):
            nonlocal child
            child = asyncio.current_task()
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        recall = asyncio.create_task(memory.recall_entries("project", selector))
        await started.wait()
        try:
            recall.cancel()
            with pytest.raises(asyncio.CancelledError):
                await recall
            assert stopped.is_set(), (
                "Outer recall returned while its paid selector was still active"
            )
            assert child.done()
        finally:
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)

    asyncio.run(run())


def test_timeout_returns_promptly_and_close_drains_cancellation_resistant_selector(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path)
        memory.create("one", "old project fact")
        ignored_cancel = asyncio.Event()
        release = asyncio.Event()
        child = None

        async def selector(query, records):
            nonlocal child
            child = asyncio.current_task()
            try:
                await release.wait()
            except asyncio.CancelledError:
                ignored_cancel.set()
                await release.wait()
            memory.update("one", "late changed source")
            return ["one"]

        try:
            start = time.monotonic()
            assert await memory.recall_entries("project", selector, timeout=0.01) == []
            assert time.monotonic() - start < 0.5
            await ignored_cancel.wait()
            assert not child.done()
            await memory.close()
            assert child.done()
            assert memory.get("one")["content"] == "old project fact"
            with pytest.raises(RuntimeError, match="closed"):
                await memory.recall_entries("project", selector)
        finally:
            if child is not None:
                child.cancel()
                await asyncio.gather(child, return_exceptions=True)

    asyncio.run(run())


def test_late_timeout_result_never_enters_later_recall(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path)
        memory.create("one", "old project fact")
        release = asyncio.Event()
        child = None

        async def selector(query, records):
            nonlocal child
            child = asyncio.current_task()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            memory.update("one", "updated source after deadline")
            return ["one"]

        try:
            assert await memory.recall_entries("project", selector, timeout=0.01) == []
            release.set()
            await child
            next_entries = await memory.recall_entries(
                "unrelated", lambda q, r: asyncio.sleep(0, result=[])
            )
            assert next_entries == []
            assert memory.get("one")["content"] == "updated source after deadline"
            await memory.close()
        finally:
            if child is not None:
                child.cancel()
                await asyncio.gather(child, return_exceptions=True)

    asyncio.run(run())


def test_close_waits_for_selector_to_finish_logging_before_owner_writer_closes(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path)
        memory.create("one", "project fact")
        started, released, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        writer_closed = False
        child = None

        async def selector(query, records):
            nonlocal child
            child = asyncio.current_task()
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await released.wait()
                assert not writer_closed
                settled.set()

        recall = asyncio.create_task(memory.recall_entries("project", selector))
        await started.wait()
        closing = None
        try:
            closing = asyncio.create_task(memory.close())
            await asyncio.sleep(0)
            assert not closing.done()
            released.set()
            await closing
            writer_closed = True
            assert settled.is_set() and child.done()
            with pytest.raises(asyncio.CancelledError):
                await recall
        finally:
            released.set()
            child.cancel()
            tasks = [child, recall] + ([closing] if closing is not None else [])
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_repeated_outer_cancellation_cannot_interrupt_paid_settlement(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path)
        memory.create("one", "project fact")
        started, settling, release, settled = (asyncio.Event() for _ in range(4))
        child = None

        async def selector(query, records):
            nonlocal child
            child = asyncio.current_task()
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                settling.set()
                await release.wait()
                settled.set()

        recall = asyncio.create_task(memory.recall_entries("project", selector))
        await started.wait()
        try:
            recall.cancel()
            await settling.wait()
            recall.cancel()
            await asyncio.sleep(0)
            assert not recall.done() and not child.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await recall
            assert settled.is_set() and child.done()
            await memory.close()
        finally:
            release.set()
            child.cancel()
            await asyncio.gather(child, recall, return_exceptions=True)

    asyncio.run(run())


def test_cancel_pending_requests_cancellation_immediately(tmp_path):
    async def run():
        memory = MemoryStore(tmp_path)
        memory.create("one", "project fact")
        started = asyncio.Event()
        child = None

        async def selector(query, records):
            nonlocal child
            child = asyncio.current_task()
            started.set()
            await asyncio.Event().wait()

        recall = asyncio.create_task(memory.recall_entries("project", selector))
        await started.wait()
        try:
            assert child in memory.cancel_pending()
            assert child.cancelling()
            with pytest.raises(asyncio.CancelledError):
                await recall
            await memory.close()
            await memory.close()
        finally:
            child.cancel()
            await asyncio.gather(child, recall, return_exceptions=True)

    asyncio.run(run())

"""Regression evidence from independent persistence and context review."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from agents.context import ContextManager, complete_groups
from agents.contracts import Message, ModelInfo, ToolCall
from agents.runtime_state import Budget
from agents.session import SessionStore


class CoreRegressionTests(unittest.TestCase):
    def test_invalid_usage_does_not_destroy_reservation(self):
        async def run():
            budget = Budget(token_limit=10)
            reservation = await budget.reserve(8)
            with self.assertRaises(ValueError):
                await budget.settle(reservation, -1)
            self.assertEqual(budget.pending_tokens, 8)
            await budget.settle(reservation, 4)
            self.assertEqual(budget.total_tokens, 4)

        asyncio.run(run())

    def test_append_return_value_cannot_mutate_persisted_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "s.jsonl", directory)
            self.addCleanup(store.close)
            record = store.add_message(Message("user", "original"))
            record["data"]["content"] = "corrupted"
            self.assertEqual(store.project()[0].content, "original")

    def test_latest_return_value_cannot_mutate_persisted_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "s.jsonl", directory)
            self.addCleanup(store.close)
            store.append("baseline", {"system": "original"})
            store.latest("baseline")["system"] = "corrupted"
            self.assertEqual(store.latest("baseline")["system"], "original")

    def test_recovered_completed_view_contains_synthetic_tool_results(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "s.jsonl", directory)
            self.addCleanup(store.close)
            store.append("turn_start", {"id": "turn"})
            store.add_message(Message("assistant", tool_calls=(ToolCall("call", "read"),)))
            store.append("tool_intent", {"call_id": "call"})
            store.recover_interrupted()
            # Completed child views must satisfy the same pairing invariant as requests.
            complete_groups(store.completed_view())
            self.assertEqual(store.completed_view()[-1].tool_call_id, "call")

    def test_changed_tool_declarations_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "s.jsonl"
            model = ModelInfo("test", "test", 10000, 100)
            store = SessionStore(path, directory)
            manager = ContextManager(store, model, "rules", policy={"headroom_tokens":100,"max_tokens":100})
            manager.request(({"name": "one"},))
            store.close()
            store = SessionStore(path, directory)
            self.addCleanup(store.close)
            manager = ContextManager(store, model, "rules", policy={"headroom_tokens":100,"max_tokens":100})
            old_version = manager.version
            manager.request(({"name": "two"},))
            self.assertGreater(manager.version, old_version)

    def test_parallel_summary_failure_cancels_remaining_components(self):
        from agents.contracts import ModelResponse
        from agents.runtime_state import RunContext

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                store = SessionStore(Path(directory) / "s.jsonl", directory)
                try:
                    model = ModelInfo("test", "test", 10000, 100)
                    manager = ContextManager(store, model, "rules", summary_mode="three_parallel", policy={"headroom_tokens":100,"max_tokens":100})
                    gate = asyncio.Event()
                    pending = set()

                    class Runtime:
                        routes = {"compression": model}

                        def resolve(self, route):
                            return model

                        async def complete(self, request, context):
                            if "only the episode_memory" in request.system:
                                return ModelResponse("not json")
                            task = asyncio.current_task()
                            pending.add(task)
                            try:
                                await gate.wait()
                                return ModelResponse("{}")
                            finally:
                                pending.discard(task)

                    context = RunContext(Path(directory), store, Budget())
                    try:
                        with self.assertRaises(ValueError):
                            await manager._summarize(Runtime(), context, [Message("user", "data")])
                        await asyncio.sleep(0)
                        self.assertEqual(
                            len(pending), 0, "Failed compaction left auxiliary model calls running"
                        )
                    finally:
                        for task in list(pending):
                            task.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                finally:
                    store.close()

        asyncio.run(run())

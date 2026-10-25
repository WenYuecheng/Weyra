import asyncio
import tempfile
import unittest
from pathlib import Path

from weyra.contracts import Message, ModelInfo, ModelResponse, ToolCall
from weyra.runtime_state import Budget, RunContext
from weyra.session import SessionStore


class ContextTests(unittest.TestCase):
    def test_ordinary_append_keeps_prior_messages_and_baseline(self):
        from weyra.context import ContextManager

        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / "s.jsonl", folder)
            model = ModelInfo("test", "test", 10000, 100)
            manager = ContextManager(store, model, "stable", policy={"headroom_tokens": 100})
            store.add_message(Message("user", "first"))
            before = manager.request(()).to_dict()
            store.add_message(Message("assistant", "second"))
            after = manager.request(()).to_dict()
            self.assertEqual(before["messages"], after["messages"][:1])
            self.assertEqual(before["system"], after["system"])
            store.close()

    def test_compaction_preserves_tool_pairs_and_raw_records(self):
        from weyra.context import ContextManager
        from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider

        async def run():
            with tempfile.TemporaryDirectory() as folder:
                store = SessionStore(Path(folder) / "s.jsonl", folder)
                model = ModelInfo("test", "test", 4000, 200)
                summary = '{"episode_memory":{"task":"keep"},"working_memory":{"next":"continue"},"tool_memory":{"facts":[]}}'
                registry = ProviderRegistry()
                registry.register("test", ScriptedProvider([ModelResponse(summary)] * 4))
                runtime = ModelRuntime(registry, {"main": model})
                store.add_message(Message("user", "must preserve this constraint"))
                for number in range(3):
                    call = ToolCall(str(number), "read", {})
                    store.add_message(Message("assistant", tool_calls=(call,)))
                    store.add_message(
                        Message("tool", "x" * 1200, tool_call_id=call.id, name="read")
                    )
                store.add_message(Message("user", "continue"))
                context = RunContext(Path(folder), store, Budget())
                manager = ContextManager(store, model, "stable", policy={"headroom_tokens": 100})
                count = len(store.records)
                self.assertTrue(await manager.compact(runtime, context))
                self.assertGreater(len(store.records), count)
                self.assertIn("continue", [message.content for message in store.project()])
                self.assertTrue(
                    any(
                        record["kind"] == "message" and len(record["data"]["content"]) == 1200
                        for record in store.records
                    )
                )
                store.close()

        asyncio.run(run())

    def test_empty_summary_does_not_publish(self):
        from weyra.context import validate_summary

        with self.assertRaises(ValueError):
            validate_summary("{}")

import asyncio
import tempfile
import unittest
from pathlib import Path


class FoundationTests(unittest.TestCase):
    def test_request_is_detached_and_fingerprinted(self):
        from weyra.contracts import Message, ModelInfo, PreparedRequest

        message = Message("user", "hello")
        request = PreparedRequest(ModelInfo("test", "small", 10000, 100), "rules", (message,))
        first = request.fingerprint
        message.content = "changed"
        self.assertEqual(request.messages[0].content, "hello")
        self.assertEqual(request.fingerprint, first)

    def test_reservations_cannot_overspend(self):
        from weyra.runtime_state import Budget, BudgetExceeded

        async def run():
            budget = Budget(token_limit=10)
            reservation = await budget.reserve(8)
            with self.assertRaises(BudgetExceeded):
                await budget.reserve(3)
            await budget.settle(reservation, 4)
            self.assertEqual(budget.total_tokens, 4)

        asyncio.run(run())

    def test_session_preserves_branch_and_recovers_partial_tail(self):
        from weyra.contracts import Message
        from weyra.session import SessionStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            store = SessionStore(path, project=directory)
            first = store.add_message(Message("user", "one"))
            store.add_message(Message("assistant", "two"))
            store.branch(first["id"])
            store.add_message(Message("assistant", "branch"))
            self.assertEqual([m.content for m in store.project()], ["one", "branch"])
            store.close()
            with path.open("ab") as output:
                output.write(b'{"unfinished"')
            resumed = SessionStore(path, project=directory)
            self.assertEqual([m.content for m in resumed.project()], ["one", "branch"])
            resumed.close()

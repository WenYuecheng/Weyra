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
            # Migration imports the exported branch tree and leaves its torn tail intact.
            store.export_jsonl(path)
            store.close()
            with path.open("ab") as output:
                output.write(b'{"unfinished"')
            import json

            imported_project = Path(directory) / "imported"
            imported_project.mkdir()
            lines = path.read_bytes().splitlines(keepends=True)
            header = json.loads(lines[0])
            header["data"]["project"] = str(imported_project.resolve())
            path.write_bytes(json.dumps(header).encode() + b"\n" + b"".join(lines[1:]))
            original = path.read_bytes()
            resumed = SessionStore(path, project=imported_project)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual([m.content for m in resumed.project()], ["one", "branch"])
            resumed.close()

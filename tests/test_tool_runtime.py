import asyncio
from types import SimpleNamespace
from weyra.contracts import ToolCall, ToolSpec
from weyra.tool_runtime import ToolRuntime


class Session:
    def __init__(self):
        self.events = []

    def append(self, kind, data, checkpoint=False):
        self.events.append((kind, data, checkpoint))


def test_schema_and_denial(tmp_path):
    async def run():
        session = Session()
        runtime = ToolRuntime(tmp_path, session, permission_mode="dontAsk")
        runtime.register(
            ToolSpec(
                "write",
                "write",
                {"type": "object", "additionalProperties": False},
                lambda a, c: "ok",
            )
        )
        ctx = SimpleNamespace(
            id="turn",
            permission_mode="dontAsk",
            services={},
            check_cancelled=lambda: None,
            emit=None,
        )
        assert (await runtime.execute(ToolCall("1", "write", {}), ctx)).status == "denied"
        assert (await runtime.execute(ToolCall("2", "write", {"bad": 1}), ctx)).status == "invalid"

    asyncio.run(run())


def test_file_versions_and_symlink(tmp_path):
    from weyra.tool_runtime import install_builtin_tools
    from weyra.jobs import Jobs

    async def run():
        session = Session()
        runtime = ToolRuntime(tmp_path, session, permission_mode="acceptEdits")
        jobs = Jobs(tmp_path)
        install_builtin_tools(runtime, jobs)
        ctx = SimpleNamespace(
            id="turn",
            permission_mode="acceptEdits",
            services={},
            read_versions={},
            check_cancelled=lambda: None,
            emit=None,
        )
        path = tmp_path / "a.txt"
        path.write_text("one")
        assert (
            await runtime.execute(
                ToolCall("1", "write_file", {"path": "a.txt", "content": "two"}), ctx
            )
        ).is_error
        await runtime.execute(ToolCall("2", "read_file", {"path": "a.txt"}), ctx)
        path.write_text("changed")
        assert (
            await runtime.execute(
                ToolCall(
                    "3", "edit_file", {"path": "a.txt", "old_text": "changed", "new_text": "new"}
                ),
                ctx,
            )
        ).is_error
        (tmp_path / "escape").symlink_to(tmp_path.parent)
        assert (
            await runtime.execute(ToolCall("4", "read_file", {"path": "escape/outside"}), ctx)
        ).is_error
        assert all(event[2] for event in session.events)

    asyncio.run(run())


def test_reader_barrier(tmp_path):
    async def run():
        runtime = ToolRuntime(tmp_path, Session(), permission_mode="bypass", concurrency=3)
        running = 0
        high = 0

        async def reader(a, c):
            nonlocal running, high
            running += 1
            high = max(high, running)
            await asyncio.sleep(0.01)
            running -= 1
            return "ok"

        async def writer(a, c):
            assert running == 0
            return "ok"

        runtime.register(ToolSpec("r", "read", {"type": "object"}, reader, "read", "read"))
        runtime.register(ToolSpec("w", "write", {"type": "object"}, writer))
        ctx = SimpleNamespace(
            id="t", permission_mode="bypass", services={}, check_cancelled=lambda: None, emit=None
        )
        results = await runtime.execute_batch(
            [ToolCall(str(i), "w" if i == 3 else "r", {}) for i in range(8)], ctx
        )
        assert all(not r.is_error for r in results)
        assert high == 3

    asyncio.run(run())


def test_ordered_finalization_and_execution_time_mode(tmp_path):
    async def run():
        session = Session()
        runtime = ToolRuntime(tmp_path, session)
        completed = []

        async def read(args, ctx):
            await asyncio.sleep(args["delay"])
            completed.append(args["delay"])
            ctx.permission_mode = "dontAsk"
            return "read"

        runtime.register(ToolSpec("read", "", {"type": "object"}, read, "read", "read"))
        runtime.register(ToolSpec("write", "", {"type": "object"}, lambda a, c: "effect"))
        ctx = SimpleNamespace(
            id="t", permission_mode="bypass", services={}, check_cancelled=lambda: None, emit=None
        )
        result = await runtime.execute_batch(
            [
                ToolCall("slow", "read", {"delay": 0.02}),
                ToolCall("fast", "read", {"delay": 0}),
                ToolCall("write", "write", {}),
            ],
            ctx,
        )
        assert completed == [0, 0.02]
        assert result[-1].status == "denied"
        assert [e[1]["call_id"] for e in session.events if e[0] == "tool_result"] == [
            "slow",
            "fast",
            "write",
        ]

    asyncio.run(run())


def test_cancellation_has_results_for_unstarted(tmp_path):
    async def run():
        runtime = ToolRuntime(tmp_path, Session())
        cancelled = False

        def check():
            if cancelled:
                raise asyncio.CancelledError

        async def first(args, ctx):
            nonlocal cancelled
            cancelled = True
            return "first"

        runtime.register(ToolSpec("x", "", {"type": "object"}, first, "exclusive", "read"))
        ctx = SimpleNamespace(
            id="t", permission_mode="bypass", services={}, check_cancelled=check, emit=None
        )
        result = await runtime.execute_batch([ToolCall(str(i), "x", {}) for i in range(3)], ctx)
        assert [r.status for r in result] == ["success", "cancelled", "cancelled"]
        assert [r.started for r in result] == [True, False, False]

    asyncio.run(run())


def test_permission_revocation_during_approval_prevents_start(tmp_path):
    from weyra.runtime_state import Budget, RunContext
    from weyra.session import SessionStore

    async def run():
        session = SessionStore(project=tmp_path)
        entered, answer = asyncio.Event(), asyncio.Event()
        effects = []

        async def confirm(spec, args, context):
            entered.set()
            await answer.wait()
            return True

        runtime = ToolRuntime(tmp_path, session, confirm=confirm)
        runtime.register(
            ToolSpec("write", "", {"type": "object"}, lambda a, c: effects.append("write"))
        )
        ctx = RunContext(tmp_path, session, Budget())
        task = asyncio.create_task(runtime.execute(ToolCall("a", "write", {}), ctx))
        await entered.wait()
        ctx.permission_mode = "dontAsk"
        answer.set()
        result = await task
        assert result.status == "denied" and not result.started
        assert effects == []
        assert not any(r["kind"] == "tool_intent" for r in session.records)
        session.close()

    asyncio.run(run())

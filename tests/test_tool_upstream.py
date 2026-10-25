import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from weyra.contracts import ToolCall, ToolSpec
from weyra.tool_runtime import ToolRuntime


class Session:
    session_id = "s"

    def __init__(self):
        self.events = []

    def append(self, kind, data, checkpoint=False):
        self.events.append((kind, data))


def context():
    return SimpleNamespace(
        id="turn",
        permission_mode="bypass",
        services={},
        read_versions={},
        check_cancelled=lambda: None,
        emit=None,
    )


def test_read_preview_is_one_based_and_raw_saved_before_preview(tmp_path):
    from weyra.tools import install_builtin_tools
    from weyra.jobs import Jobs

    async def run():
        runtime = ToolRuntime(tmp_path, Session())
        install_builtin_tools(runtime, Jobs(tmp_path))
        text = "\n".join(str(i) for i in range(1, 2101))
        (tmp_path / "a.txt").write_text(text)
        result = await runtime.execute(ToolCall("r", "read_file", {"path": "a.txt"}), context())
        assert result.content.startswith("1\n2\n")
        assert "\n2000\n\n[Showing lines 1-2000" in result.content
        assert "offset=2001" in result.content
        assert Path(result.output_ref).read_text() == text
        result = await runtime.execute(
            ToolCall("r2", "read_file", {"path": "a.txt", "offset": 2001, "limit": 1}), context()
        )
        assert result.content.startswith("2001\n\n[")
        assert "offset=2002" in result.content

    asyncio.run(run())


def test_shell_tail_and_utf8_byte_boundary(tmp_path):
    from weyra.output import truncate_tail, truncate_head

    text = "😀" * 20000
    tail = truncate_tail(text)
    assert len(tail.content.encode()) == 51200
    assert tail.last_line_partial
    head = truncate_head(text)
    assert head.content == "" and head.first_line_exceeds_limit
    assert len(truncate_head("x\n" * 2100).content.splitlines()) == 2000
    assert truncate_tail("\n".join(str(n) for n in range(2100))).content.startswith("100\n")


def test_scheduler_orders_intents_and_commit_with_rolling_ten_pool(tmp_path):
    async def run():
        session = Session()
        runtime = ToolRuntime(tmp_path, session)
        active = 0
        high = 0
        eleventh = asyncio.Event()

        async def read(a, c):
            nonlocal active, high
            active += 1
            high = max(high, active)
            if a["i"] == 0:
                await eleventh.wait()
            else:
                await asyncio.sleep(0)
            if a["i"] == 10:
                eleventh.set()
            active -= 1
            return str(a["i"])

        runtime.register(ToolSpec("r", "", {"type": "object"}, read, "read", "read"))
        await asyncio.wait_for(
            runtime.execute_batch([ToolCall(str(i), "r", {"i": i}) for i in range(12)], context()),
            1,
        )
        assert high == 10
        for kind in ("tool_intent", "tool_result"):
            assert [v["call_id"] for k, v in session.events if k == kind] == [
                str(i) for i in range(12)
            ]

    asyncio.run(run())


def test_task_cancel_persists_all_results_and_propagates(tmp_path):
    async def run():
        session = Session()
        runtime = ToolRuntime(tmp_path, session)
        started = asyncio.Event()

        async def read(a, c):
            started.set()
            await asyncio.Event().wait()

        runtime.register(ToolSpec("r", "", {"type": "object"}, read, "read", "read"))
        task = asyncio.create_task(
            runtime.execute_batch([ToolCall(str(i), "r", {}) for i in range(12)], context())
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        results = [v for k, v in session.events if k == "tool_result"]
        assert len(results) == 12
        assert all(v["status"] == "cancelled" for v in results)

    asyncio.run(run())


def test_grep_preview_matches_lines_and_bytes_with_complete_artifact(tmp_path):
    from weyra.tools import install_builtin_tools
    from weyra.jobs import Jobs

    async def run():
        runtime = ToolRuntime(tmp_path, Session())
        install_builtin_tools(runtime, Jobs(tmp_path))
        (tmp_path / "a.txt").write_text(("match " + "😀" * 600 + "\n") * 110)
        result = await runtime.execute(
            ToolCall("g", "grep_search", {"path": "a.txt", "pattern": "match"}), context()
        )
        assert "100 matches limit reached" in result.content
        assert "Some lines truncated to 500 chars" in result.content
        assert "50.0KB limit reached" in result.content
        assert len(Path(result.output_ref).read_text().splitlines()) == 110
        assert len(result.content.encode()) < 52000

    asyncio.run(run())


def test_session_job_transfer_is_only_explicit_target(tmp_path):
    from weyra.tools import install_builtin_tools

    class Jobs:
        def __init__(self):
            self.owner = "turn"

        def status(self, job_id):
            return {"job_id": job_id, "owner": self.owner}

        def transfer(self, job_id, owner, new_owner):
            self.owner = new_owner
            return self.status(job_id)

    async def run():
        session = Session()
        runtime = ToolRuntime(tmp_path, session)
        jobs = Jobs()
        install_builtin_tools(runtime, jobs)
        rejected = await runtime.execute(
            ToolCall("bad", "transfer_job", {"job_id": "j", "owner": "sibling"}), context()
        )
        assert rejected.is_error and jobs.owner == "turn"
        result = await runtime.execute(
            ToolCall("ok", "transfer_job", {"job_id": "j", "owner": "session"}), context()
        )
        assert result.status == "success" and jobs.owner == "session:s"
        next_turn = context()
        next_turn.id = "next"
        assert (
            await runtime.execute(ToolCall("st", "job_status", {"job_id": "j"}), next_turn)
        ).status == "success"
        assert any(k == "job_transfer" for k, v in session.events)

    asyncio.run(run())


def test_shell_preserves_full_output_and_reports_failed_exit(tmp_path):
    from weyra.tools import install_builtin_tools
    from weyra.jobs import Jobs

    async def run():
        session = Session()
        runtime = ToolRuntime(tmp_path, session)
        jobs = Jobs(tmp_path)
        install_builtin_tools(runtime, jobs)
        try:
            result = await runtime.execute(
                ToolCall(
                    "s",
                    "run_shell",
                    {
                        "command": "i=1; while [ $i -le 2100 ]; do echo $i; i=$((i+1)); done; exit 7",
                        "background": False,
                    },
                ),
                context(),
            )
            assert result.status == "error"
            assert result.content.startswith("101\n102\n")
            assert "2100" in result.content and "Full output:" in result.content
            assert Path(result.output_ref).read_text().startswith("1\n2\n")
            assert result.structured["returncode"] == 7
            assert any(k == "job_start" for k, v in session.events)
        finally:
            await jobs.close()

    asyncio.run(run())


def test_listing_and_search_inside_nested_runtime_directory(tmp_path):
    from weyra.tools import install_builtin_tools

    async def run():
        project = tmp_path / '.weyra' / 'fixtures' / 'project'
        project.mkdir(parents=True)
        (project / 'README.md').write_text('entry: app.py\n')
        (project / '.weyra').mkdir()
        (project / '.weyra' / 'private.txt').write_text('entry: private\n')
        runtime = ToolRuntime(project, Session())
        install_builtin_tools(runtime, None)
        listing = await runtime.execute(ToolCall('l', 'list_files', {'path': '.'}), context())
        assert listing.status == 'success'
        assert 'README.md' in listing.content
        assert 'private.txt' not in listing.content
        search = await runtime.execute(
            ToolCall('g', 'grep_search', {'path': '.', 'pattern': 'entry'}), context()
        )
        assert 'app.py' in search.content
        assert 'private' not in search.content

    asyncio.run(run())


def test_read_trailing_newline_and_explicit_page_semantics(tmp_path):
    from weyra.tools import install_builtin_tools

    async def run():
        (tmp_path / 'file.txt').write_text('first\nsecond\n')
        runtime = ToolRuntime(tmp_path, Session())
        install_builtin_tools(runtime, None)
        full = await runtime.execute(ToolCall('f', 'read_file', {'path': 'file.txt'}), context())
        assert full.content == 'first\nsecond\n'
        page = await runtime.execute(ToolCall('p', 'read_file', {'path': 'file.txt', 'limit': 1}), context())
        assert 'offset=2' in page.content
    asyncio.run(run())


def test_grep_regex_and_invalid_pattern(tmp_path):
    from weyra.tools import install_builtin_tools

    async def run():
        (tmp_path / 'file.txt').write_text('entry: app.py\nentry: other.txt\n')
        runtime = ToolRuntime(tmp_path, Session())
        install_builtin_tools(runtime, None)
        result = await runtime.execute(ToolCall('g', 'grep_search', {'pattern': r'app\.py$'}), context())
        assert 'app.py' in result.content
        assert 'other.txt' not in result.content
        invalid = await runtime.execute(ToolCall('b', 'grep_search', {'pattern': '['}), context())
        assert invalid.status == 'error'
    asyncio.run(run())

"""Tests for WeyraAppService, structured events, and presentation helpers."""

import asyncio
import tempfile
from pathlib import Path


from weyra.contracts import ModelInfo, ModelResponse, ToolCall, Usage
from weyra.events import EventBus
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.presentation import (
    generate_unified_diff,
    get_tool_icon,
    get_tool_summary,
    paginate_text,
    strip_ansi,
    truncate_preview,
)
from weyra.service import WeyraAppService


def make_test_runtime():
    registry = ProviderRegistry()
    registry.register(
        "test",
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("call-1", "read_file", {"path": "test.txt"}),
                    ),
                    usage=Usage(10, 5),
                ),
                ModelResponse(
                    "Here is the content of test.txt",
                    usage=Usage(20, 10),
                ),
            ]
        ),
    )
    model = ModelInfo("test", "scripted", 131072, 4096)
    runtime = ModelRuntime(
        registry, {purpose: model for purpose in ("main", "compression", "memory", "evolution")}
    )
    runtime.context_policy = {"headroom_tokens": 4096, "max_tokens": 4096}
    return runtime


def test_presentation_helpers():
    # 1. strip_ansi
    dirty = "\x1b[31mRed text\x1b[0m and \x1b[1mBold\x1b[0m"
    assert strip_ansi(dirty) == "Red text and Bold"

    # 2. tool icons and summaries
    assert get_tool_icon("read_file") == "📖"
    assert get_tool_icon("write_file") == "✏️"
    assert get_tool_icon("unknown_tool") == "🔨"
    assert get_tool_icon("read_file", ascii_only=True) == "[READ]"

    assert get_tool_summary("read_file", {"path": "README.md"}) == "README.md"
    assert get_tool_summary("run_shell", {"command": "echo hello"}) == "echo hello"

    # 3. diff generation
    old = "line 1\nline 2\n"
    new = "line 1\nline 2 modified\n"
    diff = generate_unified_diff("demo.txt", old, new)
    assert diff is not None
    assert "-line 2" in diff
    assert "+line 2 modified" in diff

    # 4. pagination
    text = "\n".join(f"line {i}" for i in range(500))
    page = paginate_text(text, "out_1", offset=0, limit=200)
    assert page.total_lines == 500
    assert len(page.lines) == 200
    assert page.has_more is True

    # 5. truncate_preview
    prev, trunc, total_l, total_c = truncate_preview(text, max_lines=50)
    assert trunc is True
    assert total_l == 500


def test_event_bus():
    bus = EventBus("test_srv")
    sub1 = bus.subscribe("sub_1")
    event1 = bus.create_event("sess_1", 1, "test_event", {"key": "val"})
    assert event1.seq == 1
    bus.publish(event1)

    received = sub1.get_nowait()
    assert received is not None
    assert received.kind == "test_event"
    assert received.data["key"] == "val"

    bus.unsubscribe("sub_1")
    assert sub1._closed is True


def test_service_lifecycle_and_commands():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "test.txt").write_text("Hello Weyra service")

        service = WeyraAppService(
            tmp,
            runtime_factory=make_test_runtime,
            session_id="test_sess",
            permission_mode="default",
        )

        async def _test():
            snapshot = await service.start()
            assert snapshot.lifecycle == "ready"
            assert snapshot.session["session_id"] == "test_sess"

            # Check commands
            help_res = await service.execute_command("/help", client_request_id="req_1")
            assert help_res.ok is True
            assert "/skills" in help_res.data

            usage_res = await service.execute_command("/usage", client_request_id="req_2")
            assert usage_res.ok is True
            assert "tokens" in usage_res.data

            skills_res = await service.execute_command("/skills", client_request_id="req_3")
            assert skills_res.ok is True

            # Submit prompt
            receipt = await service.submit("Read test.txt", client_request_id="sub_1")
            assert receipt.accepted is True
            assert receipt.mode == "prompt"

            # Test idempotency: same request_id and mode returns prior receipt
            receipt2 = await service.submit("Read test.txt", client_request_id="sub_1")
            assert receipt2.request_id == "sub_1"

            # Allow task to process
            if service._active_task:
                await service._active_task

            # Check history
            history = await service.get_history()
            assert history.session_id == "test_sess"
            assert len(history.cards) >= 1

            # Test close
            close_res = await service.close()
            assert close_res.ok is True
            assert service.lifecycle == "closed"

        asyncio.run(_test())

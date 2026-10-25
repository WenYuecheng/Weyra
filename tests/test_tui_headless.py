"""Headless automated tests for Weyra Textual TUI using Pilot."""

import asyncio
import tempfile
from pathlib import Path


from weyra.contracts import ModelInfo, ModelResponse, ToolCall, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.service import WeyraAppService
from weyra.tui.app import WeyraTUIApp
from weyra.tui.modals import ApprovalModal, HelpModal, ModelPickerModal
from weyra.tui.widgets import (
    AssistantMessageWidget,
    ChatView,
    EditorArea,
    FooterBar,
    StatusBar,
    ToolCardWidget,
    UserMessageWidget,
)


def make_tui_test_runtime():
    registry = ProviderRegistry()
    registry.register(
        "test",
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("call-tui-1", "read_file", {"path": "test.txt"}),
                    ),
                    usage=Usage(10, 5),
                ),
                ModelResponse(
                    "Here is the content of test.txt\n\n```python\nprint('hello')\n```",
                    usage=Usage(20, 10),
                ),
            ]
        ),
    )
    model = ModelInfo("test", "scripted", 131072, 4096)
    runtime = ModelRuntime(
        registry,
        {purpose: model for purpose in ("main", "compression", "memory", "evolution")},
    )
    runtime.context_policy = {"headroom_tokens": 4096, "max_tokens": 4096}
    return runtime


def test_tui_app_lifecycle_and_welcome():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "test.txt").write_text("Hello Weyra Headless")

        service = WeyraAppService(
            tmp,
            runtime_factory=make_tui_test_runtime,
            session_id="test_headless",
            permission_mode="default",
            enable_memory=False,
            enable_evolution=False,
        )

        app = WeyraTUIApp(service)

        async def _test():
            async with app.run_test() as pilot:
                # 1. Verify core structure
                assert isinstance(pilot.app.status_bar, StatusBar)
                assert isinstance(pilot.app.chat_view, ChatView)
                assert isinstance(pilot.app.editor_area, EditorArea)
                assert isinstance(pilot.app.footer_bar, FooterBar)

                # 2. Check initial status
                assert pilot.app.ui_state.session_id == "test_headless"
                assert pilot.app.ui_state.activity == "idle"

                # 3. Check welcome banner is present in empty session
                welcome = pilot.app.query("#welcome-banner")
                assert len(welcome) == 1

                # 4. Trigger Ctrl+C on empty input -> should exit app
                await pilot.press("ctrl+c")

        asyncio.run(_test())


def test_tui_submit_and_cards():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "test.txt").write_text("Sample file content for tool call")

        service = WeyraAppService(
            tmp,
            runtime_factory=make_tui_test_runtime,
            session_id="test_cards",
            permission_mode="default",
            enable_memory=False,
            enable_evolution=False,
        )

        app = WeyraTUIApp(service)

        async def _test():
            async with app.run_test() as pilot:
                # Type prompt into editor
                pilot.app.editor_area.set_text("Read test.txt")
                assert pilot.app.editor_area.get_text() == "Read test.txt"

                # Press Ctrl+S to submit prompt
                await pilot.press("ctrl+s")
                await pilot.pause(0.2)

                # Verify user message mounted
                user_msgs = pilot.app.query(UserMessageWidget)
                assert len(user_msgs) == 1
                assert user_msgs[0].content == "Read test.txt"

                # Wait for active task to complete
                if service._active_task:
                    await service._active_task
                await pilot.pause(0.2)

                # Verify tool card mounted and completed
                tool_cards = pilot.app.query(ToolCardWidget)
                assert len(tool_cards) == 1
                card = tool_cards[0]
                assert card.tool_name == "read_file"
                assert card.status == "success"

                # Verify assistant message mounted
                asst_msgs = pilot.app.query(AssistantMessageWidget)
                assert len(asst_msgs) == 1

                # Exit cleanly
                await pilot.press("ctrl+c")

        asyncio.run(_test())


def test_tui_modals():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        service = WeyraAppService(
            tmp,
            runtime_factory=make_tui_test_runtime,
            session_id="test_modals",
            enable_memory=False,
            enable_evolution=False,
        )
        app = WeyraTUIApp(service)

        async def _test():
            async with app.run_test() as pilot:
                # 1. Open HelpModal with F1
                await pilot.press("f1")
                await pilot.pause(0.1)
                assert isinstance(pilot.app.screen, HelpModal)

                # Close modal with Esc
                await pilot.press("escape")
                await pilot.pause(0.1)
                assert not isinstance(pilot.app.screen, HelpModal)

                # F4 avoids the Ctrl+M / Enter collision in terminal input.
                await pilot.press("f4")
                await pilot.pause(0.1)
                assert isinstance(pilot.app.screen, ModelPickerModal)

                # Close modal with Esc
                await pilot.press("escape")
                await pilot.pause(0.1)
                assert not isinstance(pilot.app.screen, ModelPickerModal)

                # 3. Test ApprovalModal default focus
                approval = ApprovalModal(
                    request_id="apr_test",
                    tool_name="write_file",
                    arguments={"path": "dangerous.sh"},
                    permission="write",
                )
                pilot.app.push_screen(approval)
                await pilot.pause(0.1)
                assert isinstance(pilot.app.screen, ApprovalModal)

                # Strict requirement: default focus MUST be on Reject button
                focused = pilot.app.focused
                assert focused is not None
                assert focused.id == "btn-reject"

                # Dismiss by pressing Escape
                await pilot.press("escape")
                await pilot.pause(0.1)
                assert not isinstance(pilot.app.screen, ApprovalModal)

                # Exit
                await pilot.press("ctrl+c")

        asyncio.run(_test())

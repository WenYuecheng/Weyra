"""Regression coverage for everyday terminal interactions."""

import asyncio
from unittest.mock import AsyncMock

from weyra.service import SubmissionReceipt, WeyraAppService
from weyra.presentation import project_raw_session_records
from weyra.tui.app import WeyraTUIApp
from weyra.tui.modals import ModelPickerModal, SessionPickerModal
from weyra.tui.modals.output_viewer import OutputViewerModal
from weyra.tui.widgets import AssistantMessageWidget, ToolCardWidget
from tests.test_tui_headless import make_tui_test_runtime


def service_at(path):
    return WeyraAppService(path, runtime_factory=make_tui_test_runtime,
                           enable_memory=False, enable_evolution=False)


def test_restored_chat_does_not_show_environment_as_user_prompt():
    cards = project_raw_session_records([
        {"id": "environment", "kind": "message", "data": {
            "role": "user", "source": "environment", "content": "internal environment JSON"}},
        {"id": "prompt", "kind": "message", "data": {
            "role": "user", "source": "user", "content": "Actual prompt"}},
    ])
    assert [c.content for c in cards] == ["Actual prompt"]


def test_enter_keeps_editor_and_rejected_submission_keeps_draft(tmp_path):
    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            app.editor_area.set_text("  keep indentation")
            await pilot.press("shift+enter")
            assert not isinstance(app.screen, ModelPickerModal)
            draft = app.editor_area.get_text()
            assert "\n" in draft
            service.submit = AsyncMock(return_value=SubmissionReceipt(False, "rejected", "prompt", "Busy"))
            await pilot.press("enter")
            await pilot.pause()
            assert app.editor_area.get_text() == draft
            assert service.submit.call_args.args[0] == draft
    asyncio.run(check())


def test_markdown_initial_text_is_not_duplicated(tmp_path):
    async def check():
        app = WeyraTUIApp(service_at(tmp_path))
        async with app.run_test() as pilot:
            widget = app.chat_view.start_assistant_message("Saved answer")
            widget.finish()
            await pilot.pause()
            assert widget._rendered_text == "Saved answer"
            assert widget._markdown.source == "Saved answer"
    asyncio.run(check())


def test_restored_cards_follow_service_schema(tmp_path):
    async def check():
        app = WeyraTUIApp(service_at(tmp_path))
        async with app.run_test() as pilot:
            app.chat_view.load_history_cards([
                {"kind": "assistant_message", "content": "Historical answer"},
                {"kind": "tool_call", "content": "saved result", "status": "denied",
                 "metadata": {"call_id": "old-call", "name": "run_shell", "arguments": {}}},
            ])
            await pilot.pause()
            assert len(app.query(AssistantMessageWidget)) == 1
            assert len(app.query(ToolCardWidget)) == 1
    asyncio.run(check())


def test_session_picker_and_create_session(tmp_path):
    async def check():
        app = WeyraTUIApp(service_at(tmp_path))
        async with app.run_test() as pilot:
            old_id = app.ui_state.session_id
            await pilot.press("f2")
            await pilot.pause()
            assert isinstance(app.screen, SessionPickerModal)
            await pilot.press("ctrl+n")
            await pilot.pause()
            assert app.ui_state.session_id != old_id
    asyncio.run(check())


def test_cancel_uses_public_service_api(tmp_path):
    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test():
            service.cancel_turn = AsyncMock()
            app.ui_state.activity = "generating"
            await app.action_handle_ctrl_c()
            service.cancel_turn.assert_awaited_once()
            app.ui_state.activity = "idle"
    asyncio.run(check())


def test_tool_result_remains_accessible_after_completion(tmp_path):
    (tmp_path / "test.txt").write_text("Expected tool content", encoding="utf-8")
    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test() as pilot:
            app.editor_area.set_text("Read test.txt")
            await pilot.press("ctrl+s")
            await service._active_task
            await pilot.pause()
            card = app.query_one(ToolCardWidget)
            assert "Expected tool content" in card.full_content
            answer = app.query_one(AssistantMessageWidget)
            assert "Here is the content" in answer._markdown.source
            assert app.chat_view.tool_cards[card.call_id] is card
            assert card.artifact_path
            card.collapsed = False
            await pilot.pause()
            # Bring the expanded card action into view before activating it;
            # the full response may have moved it above the scroll viewport.
            card.output_button.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, OutputViewerModal)
            assert "Expected tool content" in app.screen._content_static.render().plain
            await pilot.press("escape")
            history = await service.get_history()
            await app.chat_view.reset()
            app.chat_view.load_history_cards(history.cards)
            await pilot.pause()
            restored = app.query_one(ToolCardWidget)
            assert restored.artifact_path
            assert "Expected tool content" in restored.full_content
    asyncio.run(check())


def test_compact_layout_has_no_overlapping_editor_or_footer(tmp_path):
    async def check():
        app = WeyraTUIApp(service_at(tmp_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.editor_area.region.right <= 80
            assert app.editor_area.region.bottom <= app.footer_bar.region.y
            assert app.chat_view.region.height >= 10
            assert app.status_bar.render().cell_len <= 160
    asyncio.run(check())


def test_service_close_ends_event_listener(tmp_path):
    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test() as pilot:
            await service.close()
            await pilot.pause()
            assert app._event_task.done()
            assert app._event_task.exception() is None
    asyncio.run(check())

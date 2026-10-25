"""Automated tests for Enter-to-send, Shift+Enter newline, and slash command recommendation & draft protection."""

import asyncio

from textual import events

from weyra.tui.app import WeyraTUIApp
from weyra.tui.commands import TUI_COMMANDS, match_commands
from weyra.tui.modals import HelpModal
from tests.test_tui_review import service_at


def test_command_catalog_definitions_and_matching():
    # 1. Verify list contains all supported canonical commands in exact order
    names = [c.name for c in TUI_COMMANDS]
    assert names[0] == "/help"
    assert names[1] == "/session"
    assert names[2] == "/new"
    assert names[3] == "/skills"
    assert names[-1] == "/exit"
    assert len(TUI_COMMANDS) == 24

    # 2. Check removed commands do not exist
    for forbidden in ("/skill-eval",):
        assert forbidden not in names

    # 3. Match / returns all commands
    all_matched = match_commands("/")
    assert len(all_matched) == 24

    # 4. Prefix matching
    sk_matched = match_commands("/sk")
    assert [c.name for c in sk_matched] == ["/skills", "/skill-history", "/skill-rollback"]

    # 5. Alias matching
    alias_matched = match_commands("/routes")
    assert len(alias_matched) == 1
    assert alias_matched[0].name == "/models"

    alias_sessions = match_commands("/sessions")
    assert len(alias_sessions) == 1
    assert alias_sessions[0].name == "/session"

    # 6. Nonexistent command
    assert match_commands("/xyznonexistent") == []
    assert match_commands("not_a_slash") == []


def test_enter_submits_and_whitespace_does_not_submit(tmp_path):
    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            # 1. Pure whitespace should not submit
            app.editor_area.set_text("   \n\t  ")
            await pilot.press("enter")
            await pilot.pause()
            assert len(app.chat_view.children) == 1  # only welcome banner

            # 2. Text with Enter submits
            app.editor_area.set_text("Hello Weyra")
            await pilot.press("enter")
            await pilot.pause()
            if service._active_task:
                await service._active_task
            await pilot.pause()
            # User message added
            user_msgs = [c for c in app.chat_view.children if c.__class__.__name__ == "UserMessageWidget"]
            assert len(user_msgs) == 1
            assert user_msgs[0].content == "Hello Weyra"

            # 3. Ctrl+S also submits
            app.editor_area.set_text("Second message")
            await pilot.press("ctrl+s")
            await pilot.pause()
            if service._active_task:
                await service._active_task
            await pilot.pause()
            user_msgs = [c for c in app.chat_view.children if c.__class__.__name__ == "UserMessageWidget"]
            assert len(user_msgs) == 2
            assert user_msgs[1].content == "Second message"

    asyncio.run(check())


def test_newline_keys_and_selection_replacement_with_undo(tmp_path):
    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.editor_area.editor

            # 1. Shift+Enter inserts newline at cursor
            editor.text = "line1line2"
            editor.cursor_location = (0, 5)
            await pilot.press("shift+enter")
            assert editor.text == "line1\nline2"
            assert editor.cursor_location == (1, 0)

            # 2. Alt+Enter inserts newline
            editor.text = "abc"
            editor.cursor_location = (0, 3)
            await pilot.press("alt+enter")
            assert editor.text == "abc\n"
            assert editor.cursor_location == (1, 0)

            # 3. Ctrl+J inserts newline
            editor.text = "foo"
            editor.cursor_location = (0, 3)
            await pilot.press("ctrl+j")
            assert editor.text == "foo\n"
            assert editor.cursor_location == (1, 0)

            # 4. Selection replacement with newline
            editor.text = "hello REPLACE_ME world"
            editor.selection = editor.selection.__class__((0, 5), (0, 16))
            await pilot.press("shift+enter")
            assert editor.text == "hello\n world"
            assert editor.cursor_location == (1, 0)

            # 5. Undo restores replaced selection
            editor.undo()
            assert editor.text == "hello REPLACE_ME world"

    asyncio.run(check())


def test_slash_recommendation_filtering_and_tab_completion(tmp_path):
    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            recs = app.editor_area.recommendation_list
            editor = app.editor_area.editor

            # 1. Initially recommendations are hidden
            assert not recs.display

            # 2. Typing '/' shows all recommendations
            app.editor_area.set_text("/")
            app.editor_area.editor.cursor_location = (0, 1)
            await pilot.pause()
            assert recs.display
            assert recs.option_count == 24

            # 3. Typing '/sk' filters recommendations
            app.editor_area.set_text("/sk")
            app.editor_area.editor.cursor_location = (0, 3)
            await pilot.pause()
            assert recs.display
            assert recs.option_count == 3

            # 4. Pressing Tab completes highlighted option (/skills)
            await pilot.press("tab")
            await pilot.pause()
            assert editor.text == "/skills"
            assert not recs.display

            # 5. Argument typing does not trigger recommendations
            app.editor_area.set_text("/memory foo")
            app.editor_area.editor.cursor_location = (0, 11)
            await pilot.pause()
            assert not recs.display

            # 6. Body text slash does not trigger recommendations
            app.editor_area.set_text("look at /var/log")
            app.editor_area.editor.cursor_location = (0, 16)
            await pilot.pause()
            assert not recs.display

            # 7. Enter does not auto-execute uncompleted recommendation
            app.editor_area.set_text("/sk")
            app.editor_area.editor.cursor_location = (0, 3)
            await pilot.pause()
            assert recs.display
            await pilot.press("enter")
            await pilot.pause()
            # Submitted text was '/sk' (not '/skills')
            notice = [c for c in app.chat_view.children if c.__class__.__name__ == "SystemNoticeWidget"]
            assert any("命令失败: /sk" in n.notice_title for n in notice)

    asyncio.run(check())


def test_draft_protection_and_temporary_command_mode(tmp_path):
    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            # 1. Type normal draft
            valuable_draft = "my valuable code snippet\nwith multiple lines"
            app.editor_area.set_text(valuable_draft)
            assert app.editor_area.get_text() == valuable_draft
            assert not app.editor_area.has_saved_draft

            # 2. Click [ ⚡ / ] button -> enters temporary command mode
            await pilot.click("#btn-quick-slash")
            await pilot.pause()
            assert app.editor_area.has_saved_draft
            assert app.editor_area.get_text() == "/"
            assert app.editor_area.recommendation_list.display

            # 3. Repeated clicks on [ ⚡ / ] do not overwrite original draft
            await pilot.click("#btn-quick-slash")
            await pilot.pause()
            assert app.editor_area._saved_draft.text == valuable_draft

            # 4. Press Esc -> restores original draft
            await pilot.press("escape")
            await pilot.pause()
            assert not app.editor_area.has_saved_draft
            assert app.editor_area.get_text() == valuable_draft
            assert not app.editor_area.recommendation_list.display

            # 5. Enter temporary mode and delete leading / -> immediately restores draft
            await pilot.click("#btn-quick-slash")
            await pilot.pause()
            assert app.editor_area.has_saved_draft
            app.editor_area.set_text("")
            await pilot.pause()
            assert not app.editor_area.has_saved_draft
            assert app.editor_area.get_text() == valuable_draft

            # 6. Enter temporary mode, execute command successfully -> draft is restored
            await pilot.click("#btn-quick-slash")
            await pilot.pause()
            app.editor_area.set_text("/usage")
            await pilot.press("enter")
            await pilot.pause()
            assert not app.editor_area.has_saved_draft
            assert app.editor_area.get_text() == valuable_draft

            # 7. Enter temporary mode, execute command failure -> draft is kept, command stays
            await pilot.click("#btn-quick-slash")
            await pilot.pause()
            app.editor_area.set_text("/unknown_fail_command")
            await pilot.press("enter")
            await pilot.pause()
            assert app.editor_area.has_saved_draft
            assert app.editor_area.get_text() == "/unknown_fail_command"
            # Press Esc now restores draft
            await pilot.press("escape")
            await pilot.pause()
            assert not app.editor_area.has_saved_draft
            assert app.editor_area.get_text() == valuable_draft

            # 8. Enter temporary mode, open modal command (/help) -> closing modal restores draft
            await pilot.click("#btn-quick-slash")
            await pilot.pause()
            app.editor_area.set_text("/help")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, HelpModal)
            await pilot.press("escape")
            await pilot.pause()
            assert not app.editor_area.has_saved_draft
            assert app.editor_area.get_text() == valuable_draft

    asyncio.run(check())


def test_multiline_paste_does_not_send(tmp_path):
    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            # Simulate paste
            paste_text = "def hello():\n    print('world')\n"
            app.editor_area.editor.post_message(events.Paste(paste_text))
            await pilot.pause()
            assert app.editor_area.get_text() == paste_text
            # No user message was sent
            user_msgs = [c for c in app.chat_view.children if c.__class__.__name__ == "UserMessageWidget"]
            assert len(user_msgs) == 0

    asyncio.run(check())


def test_catalog_matches_supported_commands():
    names = {c.name for c in TUI_COMMANDS}
    assert not names & {"/checkout", "/permissions", "/approval"}
    assert {"/steer", "/refresh", "/mcp", "/mcp-trust"} <= names
    usages = {c.name: c.args_usage for c in TUI_COMMANDS}
    assert usages["/branch"] == "<record>"
    assert usages["/fork"] == "<name>"


def test_submission_keeps_newer_draft(tmp_path):
    from unittest.mock import AsyncMock
    from weyra.service import SubmissionReceipt

    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            async def accept(*args, **kwargs):
                app.editor_area.set_text("new draft typed while submitting")
                return SubmissionReceipt(True, "accepted", "prompt", "Accepted")

            service.submit = AsyncMock(side_effect=accept)
            app.editor_area.set_text("original prompt")
            await pilot.press("enter")
            await pilot.pause()
            assert app.editor_area.get_text() == "new draft typed while submitting"
            assert not app.editor_area.is_submitting

    asyncio.run(check())

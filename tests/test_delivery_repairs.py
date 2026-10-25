import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

from weyra.events import EventBus, Subscription
from weyra.contracts import ModelInfo
from weyra.skill_judge import SkillJudge
from weyra.runtime_state import Budget
from tests.test_skill_judge import FakeRuntime
from tests.test_service_compatibility import make_service


def test_subscription_drain_clears_byte_accounting():
    sub = Subscription('test')
    bus = EventBus('test')
    for size in (1, 6000, 300):
        assert sub.push(bus.create_event('s', 1, 'text_delta', {'delta': '中' * size}))
    while sub.get_nowait() is not None:
        pass
    assert sub.approx_bytes == 0


def test_overflow_wakes_consumer():
    async def check():
        sub = Subscription('test', max_bytes=10)
        assert not sub.push(EventBus('test').create_event('s', 1, 'text_delta', {'delta': 'large'}))
        assert await asyncio.wait_for(sub.get(), .1) is None
        assert sub.needs_resync
    asyncio.run(check())


def test_judge_accepts_explicit_custom_model():
    runtime = FakeRuntime([])
    runtime.routes['skill_judge'] = ModelInfo('openai', 'local-judge', 8192, 512)
    assert SkillJudge(runtime, Budget()).model.model_id == 'local-judge'


def test_daily_commands_and_busy_policy(tmp_path):
    async def check():
        service = make_service(tmp_path, permission_mode='acceptEdits')
        await service.start()
        try:
            agent = service.agent
            agent.budget.total_cost_cny = Decimal('1.25')
            cost = await service.execute_command('/cost', client_request_id='cost')
            assert cost.ok and cost.data['cost_cny'] == '1.25'
            assert (await service.execute_command('/plan on', client_request_id='on')).ok
            assert agent.permission_mode == agent.tools.permission_mode == 'plan'
            assert (await service.execute_command('/plan off', client_request_id='off')).ok
            assert agent.permission_mode == agent.tools.permission_mode == 'acceptEdits'
            agent.compact = AsyncMock(return_value=True)
            assert (await service.execute_command('/compact', client_request_id='compact')).ok
            agent.compact.assert_awaited_once()
            agent._running = True
            assert not (await service.execute_command('/plan on', client_request_id='busy')).ok
            agent._running = False
            old_id = service.session_id
            assert (await service.execute_command('/clear', client_request_id='clear')).ok
            assert old_id == service.session_id
            assert service.agent.session.project() == []
            assert service.agent.budget.total_cost_cny == Decimal("1.25")
            assert service.agent.permission_mode == 'acceptEdits'
        finally:
            service.agent._running = False
            await service.close()
    asyncio.run(check())


def test_resync_restores_partial_stream_and_receives_new_events(tmp_path):
    async def check():
        service = make_service(tmp_path)
        await service.start()
        try:
            await service._handle_agent_event('text', 'partial')
            snapshot, history, sub, live = await service.resubscribe('test')
            assert any(e['kind'] == 'text_delta' and e['data']['delta'] == 'partial' for e in live)
            await service._handle_agent_event('text', ' next')
            event = await asyncio.wait_for(sub.get(), .1)
            assert event.seq > snapshot.through_seq
            assert event.data['delta'] == ' next'
            assert history.session_id == service.session_id
        finally:
            await service.close()
    asyncio.run(check())


def test_manual_compact_empty_context_finishes_activity(tmp_path):
    async def check():
        service = make_service(tmp_path)
        await service.start()
        try:
            result = await service.execute_command('/compact', client_request_id='compact-empty')
            assert result.ok
            assert (await service.get_snapshot()).activity == 'idle'
            assert not service.agent._running
        finally:
            await service.close()
    asyncio.run(check())


def test_tui_recovers_overflow_without_repeating_partial_text(tmp_path):
    from weyra.tui.app import WeyraTUIApp
    from tests.test_tui_review import service_at

    async def check():
        service = service_at(tmp_path)
        app = WeyraTUIApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            old = app._subscription
            old.max_bytes = 1
            await service._handle_agent_event('text', 'recovered-prefix')
            await pilot.pause()
            assert app._subscription is not old
            assert not app._subscription.needs_resync
            await service._handle_agent_event('text', '-suffix')
            await pilot.pause()
            widget = app.chat_view.get_or_create_assistant_message()
            widget.finish()
            text = widget._rendered_text
            assert text == 'recovered-prefix-suffix'
    asyncio.run(check())


def test_file_tools_use_utf8_for_chinese_roundtrip(tmp_path, monkeypatch):
    from pathlib import Path
    from weyra.contracts import ToolCall
    from weyra.runtime_state import RunContext

    original_read, original_write = Path.read_text, Path.write_text

    def checked_read(path, *args, **kwargs):
        if path.name == '中文.txt':
            assert kwargs.get('encoding') == 'utf-8'
        return original_read(path, *args, **kwargs)

    def checked_write(path, *args, **kwargs):
        if path.name == '中文.txt':
            assert kwargs.get('encoding') == 'utf-8'
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', checked_read)
    monkeypatch.setattr(Path, 'write_text', checked_write)

    async def check():
        service = make_service(tmp_path, permission_mode='acceptEdits')
        await service.start()
        try:
            agent = service.agent
            ctx = RunContext(tmp_path, agent.session, agent.budget, 'acceptEdits')
            for call in (
                ToolCall('write', 'write_file', {'path': '中文.txt', 'content': '你好，世界🌍'}),
                ToolCall('edit', 'edit_file', {'path': '中文.txt', 'old_text': '世界', 'new_text': '中文'}),
            ):
                result = await agent.tools.execute(call, ctx)
                assert not result.is_error, result.content
            result = await agent.tools.execute(ToolCall('read', 'read_file', {'path': '中文.txt'}), ctx)
            assert '你好，中文🌍' in result.content
            assert (tmp_path / '中文.txt').read_bytes() == '你好，中文🌍'.encode('utf-8')
        finally:
            await service.close()
    asyncio.run(check())


def test_clear_blocks_old_completed_view_and_fork(tmp_path):
    from weyra.contracts import Message

    async def check():
        service = make_service(tmp_path)
        await service.start()
        try:
            agent = service.agent
            agent.session.add_message(Message('user', 'OLD_PRIVATE_CONTEXT'))
            agent.session.append('turn_end', {'state': 'completed'}, True)
            assert agent.session.completed_view()
            assert (await service.execute_command('/clear', client_request_id='clear')).ok
            assert agent.session.completed_view() == []
            child = await agent.spawn_child(mode='fork')
            try:
                assert child.session.project() == []
            finally:
                await child.close(close_runtime=False)
            agent.session.add_message(Message('user', 'NEW_CONTEXT'))
            agent.session.append('turn_end', {'state': 'completed'}, True)
            assert [m.content for m in agent.session.completed_view()] == ['NEW_CONTEXT']
        finally:
            await service.close()
    asyncio.run(check())


def test_tui_plan_command_refreshes_permission_without_clearing_chat(tmp_path):
    from weyra.tui.app import WeyraTUIApp
    from tests.test_tui_review import service_at

    async def check():
        app = WeyraTUIApp(service_at(tmp_path))
        async with app.run_test(size=(80, 24)):
            app.chat_view.add_user_message('KEEP_CHAT')
            for command, expected in (('/plan on', 'plan'), ('/plan off', 'default')):
                assert await app._handle_slash_command(command)
                assert app.ui_state.permission_mode == expected
                assert any(getattr(w, 'content', '') == 'KEEP_CHAT' for w in app.chat_view.children)
    asyncio.run(check())

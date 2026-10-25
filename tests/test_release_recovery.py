"""Release regressions for historical turns and explicit context clearing."""
import asyncio
from contextlib import closing

import pytest

from weyra.contracts import Message
from weyra.session import SessionStore
from tests.test_service_compatibility import make_service


@pytest.mark.parametrize('field', ['id', 'turn_id'])
def test_recovery_accepts_both_turn_formats_and_is_idempotent(tmp_path, field):
    with closing(SessionStore(project=tmp_path)) as store:
        store.append('turn_start', {field: 'finished'})
        store.append('turn_end', {field: 'finished', 'state': 'completed'})
        store.append('turn_start', {field: 'interrupted'})
        store.recover_interrupted()
        ends = [r['data'] for r in store.chain() if r['kind'] == 'turn_end']
        assert len(ends) == 2
        assert ends[-1]['state'] == 'cancelled'
        assert ends[-1].get('turn_id', ends[-1].get('id')) == 'interrupted'
        store.recover_interrupted()
        assert len([r for r in store.chain() if r['kind'] == 'turn_end']) == 2


def test_clear_preserves_journal_and_consumes_pending_inputs(tmp_path):
    async def run():
        service = make_service(tmp_path, permission_mode='acceptEdits')
        await service.start()
        agent = service.agent
        agent.session.add_message(Message('user', 'old private context'))
        queued = agent.follow_up('old queued task')
        agent.set_goal('old objective')
        agent.session.record_child_result({
            'task_id': 'old-child', 'state': 'completed', 'summary': 'old private result',
        })
        old_ids = {r['id'] for r in agent.session.records}
        try:
            result = await service.execute_command('/clear', client_request_id='clear')
            assert result.ok
            assert agent.session.project() == []
            assert old_ids <= {r['id'] for r in agent.session.records}
            assert agent._queues == {'steering': [], 'follow_up': []}
            assert agent.goal is None and agent._pending_window is None
            assert agent.session.pending_child_results() == []
            agent._accept_child_results()
            assert agent.session.project() == []
            assert any(r['kind'] == 'input_consumed' and r['data']['id'] == queued
                       for r in agent.session.records)
            session_id = agent.session.session_id
        finally:
            await service.close()
        resumed = make_service(tmp_path, session_id=session_id)
        await resumed.start()
        try:
            assert resumed.agent.session.project() == []
            assert not resumed.agent._queues['follow_up']
            assert resumed.agent.goal is None and not resumed.agent._pending_window
            assert resumed.agent.session.pending_child_results() == []
        finally:
            await resumed.close()
    asyncio.run(run())


def test_fork_command_creates_independent_session(tmp_path):
    async def run():
        service = make_service(tmp_path)
        await service.start()
        try:
            agent = service.agent
            agent.session.add_message(Message('user', 'shared context'))
            original = agent.session.session_id
            result = await service.execute_command('/fork', client_request_id='fork')
            assert result.ok
            assert agent.session.session_id == original
            assert len(await service.list_sessions()) == 2
        finally:
            await service.close()
    asyncio.run(run())


def test_completed_turn_emits_terminal_event_after_persisting(tmp_path):
    from weyra.coding.agent import CodingAgent
    from weyra.contracts import ModelResponse
    from tests.test_agent import scripted

    async def run():
        runtime, _ = scripted([ModelResponse('done')])
        events = []
        agent = CodingAgent(tmp_path, runtime, enable_memory=False,
                            on_event=lambda kind, data: events.append((kind, data)))
        try:
            assert await agent.run_once('hello') == 'done'
            terminal = [data for kind, data in events if kind == 'turn_end']
            assert len(terminal) == 1 and terminal[0]['state'] == 'completed'
            assert terminal[0]['turn_id'] == agent.session.latest('turn_end')['turn_id']
        finally:
            await agent.close()
    asyncio.run(run())


def test_additive_start_hook_preserves_actual_system_baseline(tmp_path):
    from weyra.coding.agent import CodingAgent
    from weyra.contracts import ModelResponse
    from weyra.core.hooks import AgentStartHook, HookResult
    from tests.test_agent import scripted

    async def run():
        runtime, provider = scripted([ModelResponse('done')])
        agent = CodingAgent(tmp_path, runtime, enable_memory=False)
        baseline = agent.context_manager.system
        agent.hooks.register(AgentStartHook, lambda hook: HookResult(
            updated_system_prompt=hook.system_prompt + '\nDomain-specific rule.'))
        try:
            await agent.run_once('This is user input, not a system instruction.')
            assert provider.requests[0].system == baseline + '\nDomain-specific rule.'
            assert agent.context_manager.system == baseline
        finally:
            await agent.close()
    asyncio.run(run())

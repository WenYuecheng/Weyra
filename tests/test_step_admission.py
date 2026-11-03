import asyncio
from dataclasses import replace

import pytest

from test_agent import scripted
from weyra.agent import Agent
from weyra.contracts import ModelInfo, ModelResponse, ToolCall
from weyra.core.context import ContextOverflow
from weyra.core.model_step import switch_model


def test_failed_destination_keeps_effective_route(tmp_path):
    async def run():
        runtime, _ = scripted([])
        agent = Agent(tmp_path, runtime, enable_evolution=False)
        previous = runtime.resolve('main')
        target = ModelInfo('test', 'small', 1000, 100)
        with pytest.raises((ContextOverflow, ValueError)):
            await switch_model(agent, target, agent.context, ())
        assert runtime.resolve('main') == previous
        assert agent.context_manager.model == previous
        assert agent.session.latest('model_change') is None
        await agent.close()
    asyncio.run(run())


def test_successful_destination_commits_route(tmp_path):
    async def run():
        runtime, _ = scripted([])
        agent = Agent(tmp_path, runtime, enable_evolution=False)
        target = ModelInfo('test', 'large', 200000, 1000)
        from weyra.runtime_state import RunContext, Budget
        ctx = RunContext(tmp_path, agent.session, Budget())
        await switch_model(agent, target, ctx, ())
        assert runtime.resolve('main') == target
        assert agent.context_manager.model == target
        assert agent.session.latest('model_change')['model_id'] == 'large'
        await agent.close()
    asyncio.run(run())


def test_replaced_registration_after_model_request_cannot_execute(tmp_path):
    async def run():
        runtime, provider = scripted([
            ModelResponse(tool_calls=(ToolCall('read', 'read_file', {'path': 'x'}),)),
            ModelResponse('done'),
        ])
        (tmp_path / 'x').write_text('original')
        agent = Agent(tmp_path, runtime, enable_evolution=False)
        invoked = []
        original = provider.stream
        async def stream(request, context):
            if not invoked:
                spec = agent.tools.specs['read_file']
                agent.tools.register(replace(spec, handler=lambda a, c: invoked.append('unsafe')))
            async for event in original(request, context):
                yield event
        provider.stream = stream
        await agent.run_once('read')
        assert invoked == []
        result = next(r['data'] for r in agent.session.records if r['kind'] == 'tool_result')
        assert result['status'] == 'invalid'
        assert 'changed' in result['content'].lower()
        await agent.close()
    asyncio.run(run())


def test_switch_log_failure_rolls_back_memory_and_history(tmp_path, monkeypatch):
    async def run():
        runtime, _ = scripted([])
        agent = Agent(tmp_path, runtime, enable_evolution=False)
        from weyra.runtime_state import RunContext, Budget
        ctx = RunContext(tmp_path, agent.session, Budget())
        previous = runtime.resolve('main')
        append = agent.session.append
        def fail(kind, *args, **kwargs):
            if kind == 'model_change':
                raise OSError('storage unavailable')
            return append(kind, *args, **kwargs)
        monkeypatch.setattr(agent.session, 'append', fail)
        with pytest.raises(OSError):
            await switch_model(agent, ModelInfo('test', 'large', 200000, 1000), ctx, ())
        assert runtime.resolve('main') == previous
        assert agent.context_manager.model == previous
        assert agent.session.latest('model_route_change') is None
        await agent.close()
    asyncio.run(run())

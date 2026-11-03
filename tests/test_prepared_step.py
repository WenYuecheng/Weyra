import asyncio

from test_agent import scripted
from weyra.contracts import ModelResponse, PreparedRequest


def test_prepared_model_call_retains_provider_binding(tmp_path):
    from weyra.runtime_state import Budget, RunContext
    from weyra.session import SessionStore
    async def run():
        runtime, original = scripted([ModelResponse('original')])
        request = PreparedRequest(runtime.resolve('main'), '', ())
        bound = runtime.prepare_call(request)
        _, replacement = scripted([ModelResponse('replacement')])
        runtime.registry.providers['test'] = replacement
        store = SessionStore(project=tmp_path)
        ctx = RunContext(tmp_path, store, Budget())
        response = None
        async for event in bound.stream(ctx):
            if event.kind == 'response':
                response = event.data
        assert response.content == 'original'
        assert len(original.requests) == 1 and not replacement.requests
        store.close()
    asyncio.run(run())


def test_owned_step_cleanup_and_request_series_logged(tmp_path):
    from weyra.agent import Agent
    from weyra.core.step import PreparedStep
    async def run():
        runtime, _ = scripted([ModelResponse('done')])
        agent = Agent(tmp_path,runtime,enable_memory=False,enable_evolution=False)
        tracked = []
        original = PreparedStep.aclose
        async def close(step):
            await original(step)
            tracked.append(step)
        from unittest.mock import patch
        with patch.object(PreparedStep,'aclose',close):
            assert await agent.run_once('hello') == 'done'
        assert tracked and all(s.closed and s.tool_set.closed for s in tracked)
        request = agent.session.latest('model_request')
        assert request['step_id'] and request['request_series_id']
        assert all('prepared_tools' not in step.ctx.services and 'step_tools' not in step.ctx.services for step in tracked)
        await agent.close()
    asyncio.run(run())

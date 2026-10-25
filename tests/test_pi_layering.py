import asyncio
from weyra.core.hooks import HookRegistry, HookResult, UserInputHook, ToolCallHook


def test_hooks_chain_and_empty_result():
    async def run():
        hooks = HookRegistry()
        hooks.register(UserInputHook, lambda h: HookResult(updated_input=h.input_text + '1'))
        hooks.register(UserInputHook, lambda h: None)
        hooks.register(UserInputHook, lambda h: HookResult(updated_input=h.input_text + '2'))
        assert (await hooks.emit(UserInputHook('x'))).updated_input == 'x12'
    asyncio.run(run())


def test_critical_hook_fails_closed():
    async def run():
        hooks = HookRegistry()
        def fail(h):
            raise RuntimeError('private')
        hooks.register(ToolCallHook, fail)
        assert (await hooks.emit(ToolCallHook('id', 'write', {}))).block
    asyncio.run(run())


def test_hooks_cancel_propagates_and_result_errors_preserve_data():
    from weyra.core.hooks import ToolResultHook
    async def run():
        hooks = HookRegistry()
        def fail(h):
            raise RuntimeError('error')
        hooks.register(ToolResultHook, fail)
        assert await hooks.emit(ToolResultHook('id', 'read', 'raw', False)) is None
        async def cancel(h):
            raise asyncio.CancelledError
        hooks.register(UserInputHook, cancel)
        import pytest
        with pytest.raises(asyncio.CancelledError):
            await hooks.emit(UserInputHook('x'))
    asyncio.run(run())


def test_integrated_views_overrides_and_input_transform(tmp_path):
    from test_agent import scripted
    from weyra.agent import Agent
    from weyra.contracts import ModelResponse, ToolCall
    from weyra.core.hooks import AgentStartHook, BeforeModelCallHook, ToolResultHook
    async def run():
        runtime, provider = scripted([ModelResponse(tool_calls=(ToolCall('r','read_file',{'path':'x'}),)), ModelResponse('done')])
        (tmp_path/'x').write_text('original')
        agent = Agent(tmp_path, runtime, enable_memory=False)
        baseline = agent.context_manager.system
        agent.hooks.register(UserInputHook, lambda h: HookResult(updated_input='transformed'))
        agent.hooks.register(AgentStartHook, lambda h: HookResult(updated_system_prompt='run override'))
        agent.hooks.register(BeforeModelCallHook, lambda h: HookResult(updated_messages=h.messages))
        agent.hooks.register(ToolResultHook, lambda h: HookResult(updated_result='visible'))
        assert await agent.run_once('input') == 'done'
        assert provider.requests[0].system == 'run override'
        assert agent.context_manager.system == baseline
        assert provider.requests[0].messages[0].content == 'transformed'
        raw = [r['data'] for r in agent.session.chain() if r['kind']=='tool_result'][-1]
        assert raw['content'] == 'original' and raw['status']=='success'
        assert any(m.tool_call_id=='r' and m.content=='visible' for m in agent.session.project())
        assert any(r['kind']=='tool_result_view' for r in agent.session.chain())
        await agent.close()
    asyncio.run(run())


def test_tool_hook_schema_and_permission_rechecked(tmp_path):
    from test_agent import scripted
    from weyra.agent import Agent
    from weyra.contracts import ModelResponse, ToolCall
    async def run():
        runtime, _ = scripted([ModelResponse(tool_calls=(ToolCall('w','write_file',{'path':'x','content':'x'}),)), ModelResponse('done')])
        agent = Agent(tmp_path, runtime, enable_memory=False, permission_mode='dontAsk')
        agent.hooks.register(ToolCallHook, lambda h: HookResult(updated_args={'path':'../escape','content':'bad'}))
        await agent.run_once('write')
        assert not (tmp_path/'x').exists()
        assert not any(r['kind']=='tool_intent' for r in agent.session.chain())
        await agent.close()
    asyncio.run(run())


def test_core_loop_standalone_tool_task():
    from contextlib import nullcontext
    from types import SimpleNamespace
    from weyra.core.loop import LoopHost
    from weyra.core.harness import AgentHarness
    from weyra.contracts import ModelResponse, ToolCall, ToolResult
    async def run():
        messages, records = [], []
        session = SimpleNamespace(project=lambda: messages, add_message=messages.append,
                                  append=lambda *a: records.append(a), transaction=nullcontext)
        responses = iter([ModelResponse(tool_calls=(ToolCall('r','read',{}),)), ModelResponse('done')])
        async def response(ctx):
            return next(responses)
        async def noop(ctx):
            return False
        async def tools(calls, ctx):
            return [ToolResult('r','read','value',status='success')]
        ctx = SimpleNamespace(check_cancelled=lambda:None, services={})
        host = LoopHost(3, session, SimpleNamespace(model=SimpleNamespace(supports_tools=True)),
                        None, SimpleNamespace(execute_batch=tools), HookRegistry(), noop,
                        response, noop, lambda r:None, lambda:False)
        assert await AgentHarness().run_turn(host, ctx) == 'done'
        assert ctx.turn_state == 'completed'
        assert [m.role for m in messages] == ['assistant','tool','assistant']
    asyncio.run(run())


def test_input_hook_serializes_concurrent_runs(tmp_path):
    from test_agent import scripted
    from weyra.agent import Agent
    from weyra.contracts import ModelResponse
    import pytest
    async def run():
        runtime, _ = scripted([ModelResponse('done')])
        agent = Agent(tmp_path, runtime, enable_memory=False)
        entered, release = asyncio.Event(), asyncio.Event()
        async def hold(h):
            entered.set()
            await release.wait()
        agent.hooks.register(UserInputHook, hold)
        task = asyncio.create_task(agent.run_once('first'))
        await entered.wait()
        with pytest.raises(RuntimeError):
            await agent.run_once('second')
        release.set()
        await task
        await agent.close()
    asyncio.run(run())


def test_read_only_observers_isolate_mutation_and_failure(tmp_path):
    from test_agent import scripted
    from weyra.agent import Agent
    from weyra.contracts import ModelResponse, ToolCall
    async def run():
        runtime, _ = scripted([ModelResponse(tool_calls=(ToolCall('w','write_file',{'path':'x','content':'original'}),)), ModelResponse('done')])
        agent = Agent(tmp_path, runtime, enable_memory=False, permission_mode='bypassPermissions')
        def observer(event):
            if event.kind == 'tool_start':
                event.data['arguments']['content'] = 'corrupted'
                raise ValueError('observer error')
        agent.subscribe(observer)
        assert await agent.run_once('write') == 'done'
        assert (tmp_path/'x').read_text() == 'original'
        await agent.close()
    asyncio.run(run())


def test_recovery_prefers_durable_view(tmp_path):
    from weyra.session import SessionStore
    from weyra.contracts import Message, ToolCall
    store = SessionStore(tmp_path/'session.sqlite', tmp_path)
    store.add_message(Message('assistant', tool_calls=(ToolCall('r','read',{}),)))
    store.append('tool_result', {'call_id':'r', 'name':'read','status':'success','content':'raw'}, True)
    store.append('tool_result_view', {'call_id':'r','content':'view'}, True)
    store.recover_interrupted()
    result = store.project()[-1]
    assert result.content=='view' and result.provider_data['tool_status']=='success'
    store.close()


def test_transformed_request_overflow_rejected_before_checkpoint(tmp_path):
    from test_agent import scripted
    from weyra.agent import Agent
    from weyra.contracts import ModelResponse
    from weyra.core.hooks import AgentStartHook
    from weyra.core.context import ContextOverflow
    import pytest
    async def run():
        runtime, provider = scripted([ModelResponse('unused')])
        agent = Agent(tmp_path, runtime, enable_memory=False)
        item = agent.follow_up('queued')
        agent.hooks.register(AgentStartHook, lambda h: HookResult(updated_system_prompt='x'*1000000))
        with pytest.raises(ContextOverflow):
            await agent.resume_pending()
        assert not provider.requests
        assert agent._queues['follow_up'][0]['id']==item
        assert not any(r['kind']=='input_consumed' for r in agent.session.chain())
        await agent.close()
    asyncio.run(run())


def test_standalone_harness_owns_cancellation_and_rejects_concurrency():
    from types import SimpleNamespace
    from weyra.core.harness import AgentHarness
    import pytest
    async def run():
        harness = AgentHarness()
        entered = asyncio.Event()
        async def wait(ctx):
            entered.set()
            await asyncio.Event().wait()
        host = SimpleNamespace(max_steps=1, before_step=wait)
        ctx = SimpleNamespace(check_cancelled=lambda:None)
        task = asyncio.create_task(harness.run_turn(host, ctx))
        await entered.wait()
        assert harness.running
        with pytest.raises(RuntimeError):
            await harness.run_turn(host, ctx)
        harness.abort()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not harness.running and harness.active_task is None
    asyncio.run(run())

import asyncio
from types import SimpleNamespace

from weyra.contracts import ToolSpec
from weyra.core.tools import ToolRuntime


def test_registration_snapshot_and_conditional_set(tmp_path):
    async def run():
        runtime = ToolRuntime(tmp_path, None)
        spec = ToolSpec('x', 'x', {'type': 'object'}, lambda a,c:'old', permission='read')
        registration = runtime.register(spec, when=lambda activation: activation.model == 'allowed')
        spec.handler = lambda a,c:'new'
        assert runtime.specs['x'].handler({}, None) == 'old'
        assert not runtime.declarations()
        denied = await runtime.resolve_tools(SimpleNamespace(model='denied',ctx=None,scope=None))
        assert not denied.declarations()
        admitted = await runtime.resolve_tools(SimpleNamespace(model='allowed',ctx=None,scope=None))
        assert admitted.validate('x')
        registration.dispose()
        assert not admitted.validate('x')
        await admitted.aclose()
    asyncio.run(run())


def test_condition_execution_close_and_foreign_set(tmp_path):
    from test_patch_tool import setup
    from weyra.contracts import ToolCall
    async def run():
        runtime, ctx = setup(tmp_path)
        called = []
        runtime.register(ToolSpec('conditional', 'x', {'type':'object'}, lambda a,c: called.append(1), permission='read'), when=lambda _: True)
        assert (await runtime.execute(ToolCall('1','conditional',{}),ctx)).status == 'invalid'
        admitted = await runtime.resolve_tools(SimpleNamespace(model=None,ctx=ctx,scope=None))
        assert (await runtime.execute(ToolCall('2','conditional',{}),ctx,tool_set=admitted)).status == 'success'
        await admitted.aclose()
        assert (await runtime.execute(ToolCall('3','conditional',{}),ctx,tool_set=admitted)).status == 'invalid'
        assert called == [1]
    asyncio.run(run())


def test_failed_condition_does_not_publish_partial_set(tmp_path):
    import pytest
    async def run():
        runtime = ToolRuntime(tmp_path,None)
        runtime.register(ToolSpec('a','a',{'type':'object'},lambda a,c: None))
        async def fail(_):
            raise RuntimeError('activation failed')
        runtime.register(ToolSpec('z','z',{'type':'object'},lambda a,c: None),when=fail)
        with pytest.raises(RuntimeError,match='activation failed'):
            await runtime.resolve_tools(SimpleNamespace(model=None,ctx=None,scope=None))
    asyncio.run(run())


def test_registration_close_drains_started_body(tmp_path):
    from test_patch_tool import setup
    from weyra.contracts import ToolCall
    async def run():
        runtime, ctx = setup(tmp_path)
        started, finish = asyncio.Event(), asyncio.Event()
        async def body(a,c):
            started.set()
            await finish.wait()
            return 'completed'
        registration = runtime.register(ToolSpec('work','work',{'type':'object'},body,permission='read'))
        call = asyncio.create_task(runtime.execute(ToolCall('1','work',{}),ctx))
        await started.wait()
        closing = asyncio.create_task(registration.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        finish.set()
        assert (await call).content == 'completed'
        await closing
        assert 'work' not in runtime.specs
    asyncio.run(run())


def test_persistence_failure_retains_execution_outcome(tmp_path, monkeypatch):
    import pytest
    from test_patch_tool import setup
    from weyra.contracts import ToolCall
    from weyra.core.tools import ToolBatchExecutionError
    async def run():
        runtime, ctx = setup(tmp_path)
        effects = []
        runtime.register(ToolSpec('effect','effect',{'type':'object'}, lambda a,c: effects.append('done') or 'result'))
        def broken(_):
            raise OSError('disk unavailable')
        monkeypatch.setattr(runtime,'_persist',broken)
        with pytest.raises(ToolBatchExecutionError) as error:
            await runtime.execute(ToolCall('call','effect',{}),ctx)
        assert effects == ['done']
        assert error.value.results[0].started and error.value.results[0].content == 'result'
    asyncio.run(run())

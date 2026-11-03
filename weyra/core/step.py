"""One model/tool step owns admission, bound calls and deterministic cleanup."""
import copy
from weyra.contracts import new_id
from .tool_admission import ToolActivationContext


class PreparedStep:
    def __init__(self, owner, ctx):
        self.owner, self.ctx = owner, ctx
        self.step_id = new_id()
        self.request_series_id = new_id()
        self.tool_set = None
        self.manager = copy.copy(owner.context_manager)
        self.model = self.manager.model
        self.call = None
        self.closed = False

    async def __aenter__(self):
        try:
            await self.resolve_tools()
            return self
        except BaseException:
            await self.aclose()
            raise

    async def resolve_tools(self):
        self.tool_set = await self.owner.tools.resolve_tools(
            ToolActivationContext(self.model, self.ctx))

    def declarations(self):
        return self.tool_set.declarations()

    def prepare_request(self, request):
        if self.closed:
            raise RuntimeError('Step is closed')
        self.call = self.owner.runtime.prepare_call(request)
        self.call.step_id = self.step_id
        self.call.request_series_id = self.request_series_id
        return self.call

    def new_series(self):
        self.request_series_id = new_id()
        self.call = None

    async def reselect(self):
        await self.tool_set.aclose()
        self.manager = copy.copy(self.owner.context_manager)
        self.model = self.manager.model
        self.new_series()
        await self.resolve_tools()

    async def model_response(self):
        from .model_step import run_model_step
        return await run_model_step(self.owner, self.ctx, prepared_step=self)

    async def execute_tools(self, calls, ctx):
        return await self.owner.tools.execute_batch(calls, ctx, tool_set=self.tool_set)

    async def aclose(self):
        if not self.closed:
            self.closed = True
            self.owner.context_manager.version = max(self.owner.context_manager.version, self.manager.version)
            self.owner.context_manager._tools_hash = self.manager._tools_hash
            self.owner.context_manager._tools = self.manager._tools
            if self.tool_set is not None:
                await self.tool_set.aclose()
            self.call = None

    async def __aexit__(self, *exc):
        await self.aclose()


class LegacyStepAdapter:
    """Compatibility for LoopHost callers supplying the original callback API."""
    def __init__(self, host, ctx):
        self.host, self.ctx, self.model = host, ctx, host.context_manager.model

    async def __aenter__(self):
        return self

    async def model_response(self):
        response = await self.host.prepare_response(self.ctx)
        self.model = self.host.context_manager.model
        return response

    async def execute_tools(self, calls, ctx):
        return await self.host.tools.execute_batch(calls, ctx)

    async def __aexit__(self, *exc):
        return None


def prepare_step(host, ctx):
    return PreparedStep(host, ctx)

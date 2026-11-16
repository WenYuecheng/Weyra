"""模型与工具交互单步生命周期管理与准入持有者 (Prepared Step Lifecycle & Admission)。

【架构定位】(Architectural Role)
    位于 `weyra.core` 执行内核层，作为 ReAct 循环中单个推理-工具执行步（Step）的生命周期管理门面。
    承担单步级工具准入解析（Tool Admission）、请求序列追踪（Request Series Tracking）、
    上下文管理器影子克隆（Manager Shadowing）以及确定性异步释放（Deterministic Teardown）职责。

【开源设计思想与架构血统】(Design Pedigree)
    1. 深度对齐 DeepSeek-Harness 的单步持有者规范（Prepared Step Pattern）：
       将步骤粒度的资源（已解析工具集、请求序列号、模型绑定、取消上下文）收敛在单一上下文管理器内；
    2. 继承 Pi Agent 确定性清理与异常安全哲学（Fail-Safe Cleanup）：
       无论步骤因模型调用异常、工具执行失败或外部取消而中断，均在 `__aexit__` 中原子释放步级资源，
       并向父级上下文管理器反向同步版本与工具指纹（`_tools_hash`）。

【核心不变性与安全约束】(Invariants & Safety)
    - 异步上下文安全：必须通过 `async with PreparedStep(...) as step:` 方式使用，杜绝未关闭的泄漏；
    - 状态同步单调性：步骤关闭时，父级上下文版本号必须取 `max(parent.version, step.version)`，保证版本单调自增；
    - 闭包防重用：已关闭（`closed=True`）的步骤严禁再次准备请求。
"""

import copy

from weyra.contracts import new_id

from .tool_admission import ToolActivationContext


class PreparedStep:
    """单步推理与工具执行的生命周期隔离持有者 (Prepared Step Instance)。

    【架构定位】
    持有单步内的专属模型元数据、工具准入集合与请求序列号，驱动模型单步执行并同步终态。
    """

    def __init__(self, owner, ctx):
        """初始化就绪步骤实例。

        Args:
            owner: 拥有该步骤的宿主对象（如 LoopHost）。
            ctx: 轮次执行上下文。
        """
        self.owner, self.ctx = owner, ctx
        self.step_id = new_id()
        self.request_series_id = new_id()
        self.tool_set = None
        self.manager = copy.copy(owner.context_manager)
        self.model = self.manager.model
        self.call = None
        self.closed = False

    async def __aenter__(self):
        """异步进入步骤上下文，动态解析并准入该步骤可用的工具集。

        Returns:
            PreparedStep: 完成工具准入解析的自身实例。

        Raises:
            BaseException: 任何初始化异常均触发确定性清理后向外抛出。
        """
        try:
            await self.resolve_tools()
            return self
        except BaseException:
            await self.aclose()
            raise

    async def resolve_tools(self) -> None:
        """根据当前绑定的模型与执行上下文动态解析工具准入集合。"""
        self.tool_set = await self.owner.tools.resolve_tools(
            ToolActivationContext(self.model, self.ctx)
        )

    def declarations(self) -> tuple[dict, ...]:
        """获取当前步骤准入工具的 JSON Schema 声明元组。

        Returns:
            tuple[dict, ...]: 用于发送给大模型的工具定义列表。
        """
        return self.tool_set.declarations()

    def prepare_request(self, request):
        """为大模型物理请求绑定单步追踪 ID 与序列标识。

        Args:
            request: 模型请求规范对象。

        Returns:
            PreparedModelCall: 准备就绪的调用对象。

        Raises:
            RuntimeError: 当步骤已被关闭时抛出。
        """
        if self.closed:
            raise RuntimeError("Step is closed")
        self.call = self.owner.runtime.prepare_call(request)
        self.call.step_id = self.step_id
        self.call.request_series_id = self.request_series_id
        return self.call

    def new_series(self) -> None:
        """重置并开启新的请求序列标识（如模型重试或降级切换场景）。"""
        self.request_series_id = new_id()
        self.call = None

    async def reselect(self) -> None:
        """在步骤内部重选模型路由并重新解析工具准入集合。"""
        await self.tool_set.aclose()
        self.manager = copy.copy(self.owner.context_manager)
        self.model = self.manager.model
        self.new_series()
        await self.resolve_tools()

    async def model_response(self):
        """驱动模型运行时生成当前步骤的流式或整块回复。

        Returns:
            ModelResponse: 模型返回的响应对象。
        """
        from .model_step import run_model_step

        return await run_model_step(self.owner, self.ctx, prepared_step=self)

    async def execute_tools(self, calls: list, ctx) -> list:
        """在当前步骤准入的工具集约束下批量执行工具调用。

        Args:
            calls: 模型发起的工具调用请求列表。
            ctx: 轮次执行上下文。

        Returns:
            list[ToolResult]: 工具执行结果列表。
        """
        return await self.owner.tools.execute_batch(calls, ctx, tool_set=self.tool_set)

    async def aclose(self) -> None:
        """确定性安全释放当前步骤持有的全部资源并同步状态版本。

        【核心不变性】
        1. 幂等清理：多次调用安全无副作用；
        2. 反向同步：将步骤影子 manager 的版本号与工具哈希写回父级上下文管理器；
        3. 工具集销毁：等待并释放已准入工具集的异步资源。
        """
        if not self.closed:
            self.closed = True
            self.owner.context_manager.version = max(
                self.owner.context_manager.version, self.manager.version
            )
            self.owner.context_manager._tools_hash = self.manager._tools_hash
            self.owner.context_manager._tools = self.manager._tools
            if self.tool_set is not None:
                await self.tool_set.aclose()
            self.call = None

    async def __aexit__(self, *exc) -> None:
        """异步退出上下文，触发确定性释放。"""
        await self.aclose()


class LegacyStepAdapter:
    """旧版回调风格 LoopHost 的兼容步骤适配器 (Legacy Step Adapter)。

    【架构定位】
    为沿用旧版回调接口的外部调用方提供无缝向下兼容包装。
    """

    def __init__(self, host, ctx):
        """初始化兼容适配器。

        Args:
            host: 循环宿主对象。
            ctx: 轮次执行上下文。
        """
        self.host, self.ctx, self.model = host, ctx, host.context_manager.model

    async def __aenter__(self):
        """进入上下文。"""
        return self

    async def model_response(self):
        """委托宿主生成模型响应。"""
        response = await self.host.prepare_response(self.ctx)
        self.model = self.host.context_manager.model
        return response

    async def execute_tools(self, calls: list, ctx) -> list:
        """委托宿主批量执行工具。"""
        return await self.host.tools.execute_batch(calls, ctx)

    async def __aexit__(self, *exc) -> None:
        """退出上下文。"""
        return None


def prepare_step(host, ctx) -> PreparedStep:
    """创建并绑定指定宿主与上下文的就绪步骤实例（工厂函数）。

    Args:
        host: 循环宿主对象。
        ctx: 轮次执行上下文。

    Returns:
        PreparedStep: 初始化的就绪步骤对象。
    """
    return PreparedStep(host, ctx)

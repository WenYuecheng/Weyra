"""不可变工具元数据快照、安全冻结容器与单步准入控制引擎 (Tool Admission & Frozen Schemas)。

【架构定位】(Architectural Role)
    位于 `weyra.core` 工具体系底层契约层，负责工具 Schema 的不可变冻结（Frozen Schema）、
    工具注册快照（`ToolSnapshot`）、运行时活跃任务生命周期追踪（`ToolRegistration`）
    以及单步上下文驱动的动态工具准入集合（`ResolvedToolSet`）。

【开源设计思想与架构血统】(Design Pedigree)
    1. 深度对齐 DeepSeek-Harness 的工具准入模型（Tool Step Admission）：
       并非所有注册工具都在每一步无条件暴露给模型，而是根据上下文策略与动态条件条件式准入；
    2. 继承防御性编程原则：通过不可变字典 `FrozenDict` 与列表 `FrozenList` 强行阻断运行时对 Tool Schema 的意外就地篡改；
    3. 上下文变量追踪（ContextVar）：利用 `ACTIVE_CALL` 在协程树间隐式传递当前激活的工具集与调用者身份，防御跨协程污染。

【核心不变性与安全约束】(Invariants & Safety)
    - Schema 不可变性：任何对冻结 Schema 的写操作（`__setitem__`, `update` 等）立即抛出 `TypeError`；
    - 活跃任务排空：在工具注销或释放（`aclose`）时，必须异步等待所有关联的并发子任务执行完毕；
    - MCP 工具命名隔离：非 MCP 调度器严禁越权直接准入 `mcp__` 前缀的私有派发工具。
"""

import asyncio
from contextvars import ContextVar
import copy
from dataclasses import dataclass, field

ACTIVE_CALL = ContextVar("weyra_active_call", default=(None, None))


class FrozenDict(dict):
    """不可变字典容器 (Immutable Frozen Dictionary)。

    【架构定位】
    用于冻结工具 JSON Schema 定义，防止在模型序列化或参数校验过程中被意外篡改。
    """

    def _deny(self, *a, **kw):
        """拒绝任何修改操作并抛出 TypeError，确保工具 Schema 严格只读不可变。"""
        raise TypeError("Tool schema is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _deny

    def __deepcopy__(self, memo):
        """深拷贝时返回普通深拷贝字典副本。"""
        return {k: copy.deepcopy(v, memo) for k, v in self.items()}


class FrozenList(list):
    """不可变列表容器 (Immutable Frozen List)。

    【架构定位】
    用于冻结工具 Schema 中的枚举列表或属性名元组。
    """

    def _deny(self, *a, **kw):
        """拒绝任何修改操作并抛出 TypeError，确保 Schema 列表严格只读不可变。"""
        raise TypeError("Tool schema is immutable")

    __setitem__ = (
        __delitem__
    ) = (
        append
    ) = (
        extend
    ) = insert = pop = remove = clear = sort = reverse = __iadd__ = __imul__ = _deny

    def __deepcopy__(self, memo):
        """深拷贝时返回普通深拷贝列表副本。"""
        return [copy.deepcopy(v, memo) for v in self]


def freeze(value):
    """递归将字典与列表转换为不可变的 FrozenDict 与 FrozenList。

    Args:
        value: 待冻结的原生数据结构。

    Returns:
        递归冻结后的不可变结构副本。
    """
    if isinstance(value, dict):
        return FrozenDict({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return FrozenList(freeze(v) for v in value)
    return copy.deepcopy(value)


@dataclass(frozen=True)
class ToolSnapshot:
    """工具不可变元数据规格快照 (Immutable Tool Specification Snapshot)。

    Attributes:
        name: 工具全局唯一名称。
        description: 工具功能与使用规范描述文本。
        schema: 工具入参 JSON Schema 定义（冻结结构）。
        handler: 实际可执行的同步或异步处理函数。
        execution_mode: 并发执行模式（'exclusive', 'read', 'dispatch'）。
        permission: 权限等级（'read', 'write', 'execute'）。
    """

    name: str
    description: str
    schema: dict
    handler: object
    execution_mode: str = "exclusive"
    permission: str = "write"

    def declaration(self) -> dict:
        """生成符合 OpenAI / Anthropic 规范的工具函数声明字典。

        Returns:
            dict: 包含 name, description 与 parameters 的字典。
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": copy.deepcopy(self.schema),
        }


@dataclass(frozen=True)
class ToolActivationContext:
    """工具单步激活判定上下文 (Tool Activation Context)。

    Attributes:
        model: 当前激活的 ModelInfo 模型元数据。
        ctx: 轮次执行上下文。
        scope: 可选的作用域标识。
    """

    model: object = None
    ctx: object = None
    scope: object = None


@dataclass(eq=False)
class ToolRegistration:
    """动态工具注册条目实体 (Tool Registration Lifecycle Entry)。

    Attributes:
        runtime: 所属的 ToolRuntime 运行时实例。
        spec: 工具元数据规格快照。
        generation: 注册代数纪元号。
        when: 可选的动态条件准入判定谓词。
        live: 是否仍处于活跃可用状态。
        tasks: 当前正在执行该工具的异步任务集合。
    """

    runtime: object
    spec: ToolSnapshot
    generation: int
    when: object = None
    live: bool = True
    tasks: set = field(default_factory=set)

    def dispose(self) -> None:
        """注销工具并从运行时中解绑。"""
        self.live = False
        if self.runtime._registrations.get(self.spec.name) is self:
            self.runtime._registrations.pop(self.spec.name, None)
            self.runtime.specs.pop(self.spec.name, None)

    async def aclose(self) -> None:
        """注销工具并异步排空所有正在执行该工具的外部子任务。"""
        self.dispose()
        pending = [task for task in self.tasks if task is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class ResolvedToolSet:
    """单步准入解析完成的工具集合快照 (Resolved Admitted Tool Set)。

    【架构定位】
    持有针对特定步骤已准入的工具清单，提供工具合法性实时校验与声明提取。
    """

    def __init__(self, runtime, registrations: dict):
        """初始化准入工具集合。

        Args:
            runtime: 所属运行时。
            registrations: 已准入的工具注册映射字典。
        """
        self.runtime = runtime
        self.registrations = dict(registrations)
        self.closed = False

    def validate(self, name: str) -> bool:
        """校验指定名称的工具在当前步骤中是否依然合法可用。

        Args:
            name: 工具名称。

        Returns:
            bool: 合法返回 True，否则返回 False。
        """
        registration = self.registrations.get(name)
        return bool(
            not self.closed
            and registration
            and registration.live
            and self.runtime._registrations.get(name) is registration
            and self.runtime.specs.get(name) is registration.spec
        )

    def get(self, name: str) -> ToolSnapshot | None:
        """获取已准入工具的规格快照。

        Args:
            name: 工具名称。

        Returns:
            ToolSnapshot | None: 存在且有效时返回快照，否则返回 None。
        """
        return self.registrations[name].spec if self.validate(name) else None

    def declarations(self) -> tuple[dict, ...]:
        """获取按名称排序的所有普通公开工具声明元组（自动过滤私有 MCP 工具）。

        Returns:
            tuple[dict, ...]: 工具声明字典元组。
        """
        return tuple(
            self.registrations[name].spec.declaration()
            for name in sorted(self.registrations)
            if not name.startswith("mcp__")
        )

    async def aclose(self) -> None:
        """关闭工具集合快照。"""
        self.closed = True

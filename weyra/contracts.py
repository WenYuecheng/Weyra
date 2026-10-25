"""智能体核心跨层共享领域契约定义 (Shared Domain Contracts & Value Objects)。

【架构定位】
本模块聚合定义了 Weyra 智能体在核心微内核、工具调度引擎、子代理系统以及模型适配层之间传递的不可变值对象与数据契约。
作为工程的顶层实体定义中心，本模块保持无外部业务依赖的纯净性，并承接历史导入兼容。

【开源与工程设计借鉴】
1. 借鉴 Pi Agent (`packages/agent/`) 的轻量解耦值对象：
   - `ToolResult`：工具执行结果载荷，明确携带状态、执行副作用（effect）、错误判定与落盘产物引用 (`output_ref`)；
   - `ToolSpec`：工具能力契约声明，声明工具入参模式、并发独占级别与权限门禁；
2. 借鉴 DeepSeek-Harness (`packages/subagent/`) 的子代理交付物契约：
   - `ChildResult`：子任务执行完成后的结构化交付物（包含任务状态、摘要、证据链、生成产物与未决问题），
     支撑主智能体对子任务结果的快速决策。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .llm.types import (
    Message,
    ModelInfo,
    ModelResponse,
    PreparedRequest,
    StreamEvent,
    ToolCall,
    Usage,
    canonical,
    digest,
    new_id,
)


@dataclass
class ToolResult:
    """工具沙箱执行的标准结果载荷契约 (Standard Tool Execution Result)。

    Attributes:
        call_id: 工具调用绑定的唯一请求标识符。
        name: 被执行的工具注册名称。
        content: 供模型消费的文本结果（可能经过截断或前置格式化）。
        status: 执行生命周期状态（"success" 或 "error"）。
        started: 工具底层子进程或网络调用是否已实质启动。
        effect: 执行产生的副作用分类（"none", "read", "write" 等）。
        structured: 伴随文本返回的富结构化数据载荷字典。
        output_ref: 全量原始输出在 `.weyra/artifacts/` 目录中的持久化产物路径。
    """
    call_id: str
    name: str
    content: str
    status: str = "success"
    started: bool = False
    effect: str = "none"
    structured: Any = None
    output_ref: str | None = None

    @property
    def is_error(self) -> bool:
        """快速判断工具执行是否遭遇了非预期错误或非零退出。

        Returns:
            bool: 若 status 不是 "success" 则返回 True。
        """
        return self.status != "success"


@dataclass
class ToolSpec:
    """工具声明与执行规范，定义参数模式与权限隔离级别 (Tool Declaration & Execution Specification)。

    Attributes:
        name: 工具全局唯一注册标识。
        description: 工具语义描述，注入给 LLM 的 System Prompt 或 Tools 字段。
        schema: 工具入参的 JSON Schema 严格模式定义。
        handler: 工具底层的实际执行异步或同步处理函数。
        execution_mode: 并发执行模式，"exclusive"（写排他独占，并发度 1）或 "concurrent"（只读共享，并发度 10）。
        permission: 权限隔离等级（"read", "write", "shell", "dangerous" 等）。
    """
    name: str
    description: str
    schema: dict
    handler: Callable
    execution_mode: str = "exclusive"
    permission: str = "write"

    def declaration(self) -> dict:
        """导出符合 OpenAI / Anthropic 规范的标准模型 Function Calling 工具声明字典。

        Returns:
            dict: 包含 name, description, parameters 的标准声明结构。
        """
        return {"name": self.name, "description": self.description, "parameters": self.schema}


@dataclass
class ChildResult:
    """子智能体执行完成后的结构化交付物契约 (Subagent Structured Deliverable Contract)。

    【设计思想与开源血统】
    严格遵循 DeepSeek-Harness (`packages/subagent/`) 的任务交付物规范：
    子智能体在隔离的独立沙箱中运行完毕后，不能只返回一段不可解析的自由对话，
    必须提炼为包含任务状态、摘要要点、关键事实证据、产物列表与未决遗留问题的结构化契约。

    Attributes:
        task_id: 子代理执行任务的唯一标识。
        state: 任务完结状态（"completed", "failed", "cancelled"）。
        summary: 核心产出与执行总结（Terse Markdown）。
        evidence: 支撑结论的关键技术事实与验证证据清单。
        artifacts: 生成或修改的核心产物路径列表。
        unresolved: 本次未决或需主代理进一步决策的遗留事项清单。
        usage: 子代理在本次独立运行中消耗的累计 Token 数量。
    """
    task_id: str
    state: str
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    usage: int = 0


__all__ = [
    "Message",
    "ModelInfo",
    "ModelResponse",
    "PreparedRequest",
    "StreamEvent",
    "ToolCall",
    "Usage",
    "canonical",
    "digest",
    "new_id",
    "ToolResult",
    "ToolSpec",
    "ChildResult",
]

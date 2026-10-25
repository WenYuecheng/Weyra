"""核心领域协议与厂商中立数据契约 (Data Contracts & Domain Entities)。

【架构定位】
本模块定义 Weyra 智能体在整个运行时内核中共享的标准数据模型。
通过抽象出与具体 LLM 厂商（OpenAI, Anthropic, DeepSeek, Ollama）完全解耦的中立契约，
支撑状态持久化、并发工具执行、上下文容量计量与事件溯源体系。

【开源与工程设计借鉴】
1. 深度借鉴 Pi Agent (`packages/agent/` & `packages/ai/src/providers/`):
   - `Message`：统一抽象系统/用户/模型/工具观察各角色的消息负载；
   - `ToolCall` 与 `ToolResult`：标准化表示模型的函数调用意图与沙箱执行产物；
2. 深度借鉴 DeepSeek-Harness (`packages/typert/` & `packages/llm/`):
   - `canonical` 与 `digest`：通过字典键严格排序、去空白紧凑 JSON 格式以及 SHA-256 哈希，
     实现确定性内容寻址（Content-Addressed Blobs）与上下文缓存指纹核对；
   - `PreparedRequest`：将模型规格、系统提示词、消息序列与工具声明固化为带有版本指纹（baseline_fingerprint）的不可变信封，
     为 Anthropic Prompt Caching 与 KV 缓存对齐提供确定性基线。
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def new_id() -> str:
    """生成 32 位全局唯一十六进制 UUID 标识符。"""
    return uuid.uuid4().hex


def canonical(value: Any) -> str:
    """【DSH 规范】生成紧凑、按键升序排序的确定性 JSON 规范化字符串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    """【DSH 规范】基于 canonical 序列化计算 SHA-256 内容寻址哈希摘要。"""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolCall:
    """大模型发起的标准化单次工具调用意图。"""
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """标准化会话消息对象 (PiAgent 规范中立抽象)。"""
    role: str
    content: str = ""
    id: str = field(default_factory=new_id)
    source: str = "user"
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    provider_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """将消息对象序列化为标准字典表示。

        Returns:
            dict: 消息属性字典。
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Message:
        """从字典反序列化生成包含 ToolCall 元组的 Message 实例。

        Args:
            data: 原始数据字典。

        Returns:
            Message: 构造完成的消息实体。
        """
        data = copy.deepcopy(data)
        data["tool_calls"] = tuple(ToolCall(**call) for call in data.get("tool_calls", ()))
        return cls(**data)


@dataclass(frozen=True)
class ModelInfo:
    """模型元数据规范，定义物理上下文容量与工具/推理能力特性。"""
    provider_id: str
    model_id: str
    context_window: int
    max_output_tokens: int
    supports_tools: bool = True
    supports_reasoning: bool = False
    supports_cache: bool = False
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    reasoning_format: str | None = None

    def __post_init__(self):
        """校验模型元数据有效性，断言上下文窗口与输出上限的物理约束。

        Raises:
            ValueError: 缺少必要参数、未知推理格式或输出容量超限。
        """
        if self.reasoning_format not in {None, "openai", "thinking", "anthropic"}:
            raise ValueError("Unknown model reasoning format")
        if not self.provider_id or not self.model_id:
            raise ValueError("provider_id and model_id are required")
        if (
            type(self.context_window) is not int
            or type(self.max_output_tokens) is not int
            or not 0 < self.max_output_tokens <= self.context_window
        ):
            raise ValueError(
                "Model output capacity must be positive and at most the context window"
            )


@dataclass(frozen=True)
class Usage:
    """模型实际 Token 消耗核算，包含标准输入输出与 KV 缓存读写细分项。"""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    def __post_init__(self):
        """校验 Token 计量非负性并断言缓存分项不超过总输入计数。

        Raises:
            ValueError: 计数为负或缓存命中量超过总输入。
        """
        for value in vars(self).values():
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Usage counts must be nonnegative integers or unknown")
        if self.input_tokens is not None and (
            (self.cache_read_tokens or 0) + (self.cache_write_tokens or 0) > self.input_tokens
        ):
            raise ValueError("Cache categories exceed normalized total input")

    @property
    def total_tokens(self) -> int | None:
        """计算总 Token 消耗（输入 tokens + 输出 tokens）。

        Returns:
            int | None: 总 Token 数，若输入或输出未知则返回 None。
        """
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass
class ModelResponse:
    """模型单次推理结束后的标准化返回结果。"""
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    provider_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StreamEvent:
    """流式推理事件，包含文本分块、工具调用分块及最终响应。"""
    kind: str
    data: Any = None


@dataclass(frozen=True, init=False)
class PreparedRequest:
    """不可变模型调用请求信封 (DeepSeek-Harness 风格确定性指纹绑定)。

    【架构定位】
    将模型元数据、系统提示词、多轮消息序列与工具声明打包为完全不可变的请求对象。
    消息序列与超参数在构造时经由 `canonical` 紧凑序列化固化于 `_payload` 中，防御并发篡改。
    """
    model: ModelInfo
    system: str
    purpose: str = "main"
    context_version: int = 0
    id: str = field(default_factory=new_id)
    _payload: str = ""

    def __init__(
        self,
        model: ModelInfo,
        system: str,
        messages: list[Message] | tuple[Message, ...],
        tools: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        purpose: str = "main",
        context_version: int = 0,
        parameters: dict[str, Any] | None = None,
        id: str | None = None,
    ):
        """初始化不可变请求信封并执行 canonical 紧凑序列化固化。

        Args:
            model: 目标模型元数据规范。
            system: 系统提示词。
            messages: 会话消息列表或元组。
            tools: 工具声明规范元组。
            purpose: 请求用途标识符（"main", "compression", "memory", "evolution"）。
            context_version: 上下文代数版本。
            parameters: 模型推理超参数（如 temperature, max_output_tokens, thinking 等）。
            id: 请求唯一标识符，若为 None 则自动生成。
        """
        for key, value in {
            "model": model,
            "system": system,
            "purpose": purpose,
            "context_version": context_version,
            "id": id or new_id(),
        }.items():
            object.__setattr__(self, key, value)
        object.__setattr__(
            self,
            "_payload",
            canonical(
                {
                    "messages": [m.to_dict() for m in messages],
                    "tools": tools,
                    "parameters": parameters or {},
                }
            ),
        )

    @property
    def messages(self) -> tuple[Message, ...]:
        """反序列化获取不可变消息对象元组。

        Returns:
            tuple[Message, ...]: 消息实体元组。
        """
        return tuple(Message.from_dict(m) for m in json.loads(self._payload)["messages"])

    @property
    def tools(self) -> tuple[dict[str, Any], ...]:
        """获取工具声明规范元组。

        Returns:
            tuple[dict[str, Any], ...]: 工具规范元组。
        """
        return tuple(json.loads(self._payload)["tools"])

    @property
    def parameters(self) -> dict[str, Any]:
        """获取模型超参数字典。

        Returns:
            dict[str, Any]: 超参数字典。
        """
        return json.loads(self._payload)["parameters"]

    def to_dict(self) -> dict:
        """将请求信封全量展开序列化为字典表示。

        Returns:
            dict: 包含 model, system, purpose, messages, tools, parameters 的字典。
        """
        return {
            "model": asdict(self.model),
            "system": self.system,
            "purpose": self.purpose,
            "context_version": self.context_version,
            "id": self.id,
            **json.loads(self._payload),
        }

    @property
    def fingerprint(self) -> str:
        """计算当前完整请求信封的内容哈希指纹。"""
        data = self.to_dict()
        data.pop("id")
        return digest(data)

    @property
    def baseline_fingerprint(self) -> str:
        """计算系统提示词与工具声明基线指纹，用于前缀缓存对齐判断。"""
        return digest({"model": asdict(self.model), "system": self.system, "tools": self.tools})



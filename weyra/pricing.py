"""高精度财务级定价、预算闭环防御与消耗计量引擎 (High-Precision Pricing & Budget Enforcement Engine)。

【架构定位】
本模块负责 Weyra 智能体在多模型调用中的实时成本估算与精确计费。
通过引入金融级 Decimal 算术，杜绝浮点数累加漂移，并支持精细化 KV 缓存分段计费，
为系统的 Fail-Closed 预算熔断防御体系提供坚实的数据基石。

【开源与工程设计借鉴】
1. 借鉴 DeepSeek-Harness (`packages/core/`, `packages/guard/`) 的预算闭环防御机制 (Fail-Closed Budget Guard)：
   - 采用标准定点数 `decimal.Decimal` 运算，确保人民币（CNY）费率计算绝对精确；
   - 支持多层级模型资费体系：输入 Token 单价、输出 Token 单价、缓存读取单价、缓存写入单价；
2. 预留扣除与精确结算双阶段模型 (Two-Phase Cost Accounting)：
   - `estimate`：请求发起前取费率上界估算保守成本并执行配额预扣，若超支立即终止调用；
   - `actual`：请求返回后根据供应商精确 Usage 结算真实扣费并返还差额。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Price:
    """模型调用计费价格策略规范 (Pricing Policy Specification)。

    【设计思想与开源血统】
    对齐 DeepSeek-Harness 工业级费率配置标准：
    支持输入、输出、KV 缓存命中读取 (Cache Read)、KV 缓存创建写入 (Cache Write) 的分段计价。
    所有费率统一以“每百万 Token 人民币元 (CNY per 1M tokens)”形式声明并存储为定点数 Decimal。

    Attributes:
        input_per_million: 每百万未缓存输入 Token 单价（CNY）。
        output_per_million: 每百万模型生成输出 Token 单价（CNY）。
        cache_read_per_million: 每百万从 KV 缓存中读取的命中 Token 单价（CNY）。
        cache_write_per_million: 每百万写入 KV 缓存的 Token 单价（CNY）。
        currency: 计费货币单位，强制约束为 "CNY"。
        cost_basis: 计费基准模式，"exact"（严格精确计费）或 "ceiling"（上限估算模式）。
        cache_write_billing: 缓存写入计费策略，"separate"（独立分段计费）或 "included"（已含在输入单价内）。
    """
    input_per_million: Decimal
    output_per_million: Decimal
    cache_read_per_million: Decimal | None = None
    cache_write_per_million: Decimal | None = None
    currency: str = "CNY"
    cost_basis: str = "exact"
    cache_write_billing: str = "separate"

    @classmethod
    def from_dict(cls, values: dict) -> Price:
        """从配置字典中解析并严格校验费率策略规范 (Parse & Validate Price from Dict)。

        【核心不变性与安全约束】
        1. 必须显式声明 CNY 货币，拒绝未知外币配置；
        2. 必须包含基础的 `input_per_million` 与 `output_per_million`；
        3. 所有费率值必须能够被解析为非负且有限的 `Decimal` 数值；
        4. 语义冲突校验：若 `cache_write_billing="included"`，严禁重复指定独立的 `cache_write_per_million`。

        Args:
            values: 原始价格配置字典。

        Returns:
            Price: 实例化且不可变的 Price 价格规格对象。

        Raises:
            ValueError: 当缺少必要费率、出现非法字段或数值不合法时抛出。
        """
        allowed = {
            "currency",
            "input_per_million",
            "output_per_million",
            "cache_read_per_million",
            "cache_write_per_million",
            "cost_basis",
            "cache_write_billing",
        }
        if set(values) - allowed or values.get("currency", "CNY") != "CNY":
            raise ValueError("Pricing requires explicit CNY rates")
        if not {"input_per_million", "output_per_million"} <= set(values):
            raise ValueError("Pricing requires input and output rates")
        basis = values.get("cost_basis", "exact")
        write_billing = values.get("cache_write_billing", "separate")
        if basis not in {"exact", "ceiling"} or write_billing not in {"separate", "included"}:
            raise ValueError("Invalid explicit billing policy")
        if write_billing == "included" and values.get("cache_write_per_million") is not None:
            raise ValueError("Included cache writes cannot have a separate tariff")
        rates = {}
        for key in allowed - {"currency", "cost_basis", "cache_write_billing"}:
            value = values.get(key)
            if value is None:
                rates[key] = None
                continue
            try:
                rate = Decimal(str(value))
            except (InvalidOperation, ValueError):
                raise ValueError("Invalid price rate") from None
            if not rate.is_finite() or rate < 0:
                raise ValueError("Price rates must be finite and nonnegative")
            rates[key] = rate
        return cls(**rates, cost_basis=basis, cache_write_billing=write_billing)

    def to_dict(self) -> dict[str, str]:
        """将价格规范序列化为字符串化的纯净字典 (Serialize Price to Dict)。

        Returns:
            dict[str, str]: 可直接进行 JSON 序列化并完整保留定点精度的键值字典。
        """
        return {
            "currency": self.currency,
            **{
                key: str(value)
                for key, value in vars(self).items()
                if key != "currency" and value is not None
            },
        }

    def estimate(self, input_tokens: int, output_tokens: int) -> Decimal:
        """在请求发出前保守估算本次调用的最高金额上限 (Estimate Conservative Ceiling Cost)。

        【Fail-Closed 预扣安全算法】
        在网络请求真实发出前，系统尚无法预知下游供应商服务端的 KV 缓存命中率。
        为保证资金账户绝对不透支，算法取输入、缓存读、缓存写三者单价的绝对最大值（Max Tariff Rate）：
        `MaxRate = max(input_rate, cache_read_rate, cache_write_rate)`
        以该最大费率预扣全部输入 Token，以模型物理最大输出预扣输出 Token。

        Args:
            input_tokens: 估算的输入 Token 数量。
            output_tokens: 预留的最大输出 Token 数量。

        Returns:
            Decimal: 计算出的人民币上限金额（保留完整定点精度）。
        """
        rate = max(
            self.input_per_million,
            self.cache_read_per_million or self.input_per_million,
            self.cache_write_per_million or self.input_per_million,
        )
        return (
            Decimal(input_tokens) * rate + Decimal(output_tokens) * self.output_per_million
        ) / Decimal(1000000)

    def actual(self, usage) -> Decimal | None:
        """根据供应商返回的精确 Token 消耗核算实际产生的人民币金额 (Calculate Exact Actual Cost)。

        【多维分段精确核销体系】
        1. 前置完备性检查：若计费基准非 "exact" 或输入/输出 Token 未返回，返回 None；
        2. 缓存一致性校验：若存在缓存使用量但未定义对应单价，返回 None（拒绝模糊入账）；
        3. 总量守恒约束：`cache_read + cache_write <= input_tokens`，否则抛出越界异常；
        4. 未缓存 Token 切片：
           `uncached = input_tokens - cache_read - (cache_write if separate else 0)`
        5. 四段式加权加和：未缓存输入 + 缓存命中读 + 缓存创建写 + 模型生成输出。

        Args:
            usage: 包含 input_tokens, output_tokens, cache_read_tokens, cache_write_tokens 的 Usage 对象。

        Returns:
            Decimal | None: 精确计算出的人民币金额；若因供应商未返回明细而无法精确核算则返回 None。

        Raises:
            ValueError: 当缓存使用量为负数或缓存 Token 之和超出输入总量时抛出。
        """
        if self.cost_basis != "exact" or usage.input_tokens is None or usage.output_tokens is None:
            return None
        if (
            (self.cache_read_per_million is not None and usage.cache_read_tokens is None)
            or (self.cache_write_per_million is not None and usage.cache_write_tokens is None)
            or (
                usage.cache_write_tokens
                and self.cache_write_per_million is None
                and self.cache_write_billing != "included"
            )
            or (usage.cache_read_tokens and self.cache_read_per_million is None)
        ):
            return None
        read, write = usage.cache_read_tokens or 0, usage.cache_write_tokens or 0
        if min(read, write) < 0 or read + write > usage.input_tokens:
            raise ValueError("Cache usage exceeds total input tokens")
        uncached = (
            usage.input_tokens - read - (write if self.cache_write_billing == "separate" else 0)
        )
        cost = (
            Decimal(uncached) * self.input_per_million
            + Decimal(usage.output_tokens) * self.output_per_million
        )
        cost += Decimal(read) * (
            self.cache_read_per_million
            if self.cache_read_per_million is not None
            else self.input_per_million
        )
        cost += Decimal(write if self.cache_write_billing == "separate" else 0) * (
            self.cache_write_per_million
            if self.cache_write_per_million is not None
            else self.input_per_million
        )
        return cost / Decimal(1000000)

    def upper_bound(self, usage) -> Decimal | None:
        """在无法获得完整账单明细时计算受限的保守成本上界 (Calculate Bounded Ceiling Cost)。

        【容错与降级计费】
        当供应商返回了部分分段指标但不足以支持 `actual()` 的法定财务结算时，
        优先采用已知分段的实际费率；若依旧缺失，则平滑降级调用 `estimate()` 获得保守上界。

        Args:
            usage: 供应商返回的 Usage 统计对象。

        Returns:
            Decimal | None: 保守成本上界金额；若基本输入输出缺失则返回 None。

        Raises:
            ValueError: 当缓存使用量超过输入总 Token 数时抛出。
        """
        if usage.input_tokens is None or usage.output_tokens is None:
            return None
        read, write = usage.cache_read_tokens or 0, usage.cache_write_tokens or 0
        if min(read, write) < 0 or read + write > usage.input_tokens:
            raise ValueError("Cache usage exceeds total input tokens")
        if (usage.cache_read_tokens and self.cache_read_per_million is None) or (
            usage.cache_write_tokens
            and self.cache_write_per_million is None
            and self.cache_write_billing != "included"
        ):
            return None
        # Reported categories can use their individual ceiling rates. This does
        # not turn ceiling tariffs into verified invoice prices: actual() on the
        # original Price still returns None and settlement remains cost_unknown.
        categorized = replace(self, cost_basis="exact").actual(usage)
        if categorized is not None:
            return categorized
        return self.estimate(usage.input_tokens, usage.output_tokens)

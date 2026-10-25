"""大模型请求指数退避与抖动重试策略 (Exponential Backoff with Full Jitter Retry Policy)。

【架构定位】
本模块定义大模型通信过程中的重试时间衰减策略。
严禁依赖下游模型供应商 SDK 的黑盒静默重试（如 OpenAI / Anthropic SDK 默认自带的重试），
将重试决定权、退避间隔计算与尝试计数（attempt_budget）严格收敛在上层协调器中。

【设计思想与开源血统】
1. 深度借鉴 DeepSeek-Harness (packages/llm/ & packages/core/):
   - 经典工业级容灾参数：最大重试次数 `max_retries=2`，基准退避时间 `base_delay=0.5s`，退避上限 `max_delay=10.0s`；
   - 严格指数级时间衰减模型：退避延迟随重试轮次指数递增 `base_delay * 2 ** retry`。
2. 借鉴 Pi Agent 与分布式系统最佳实践 (AWS Architecture):
   - 引入 Full Jitter 随机抖动因子（`uniform(0.9, 1.1)`）：通过在理论退避时间上附加 ±10% 随机偏移，
     有效打散分布式客户端在高并发故障重试时引发的服务端惊群风暴（Thundering Herd Problem）。

【核心不变性与安全约束】
- 不变性约束：RetryPolicy 为冻结不可变数据类（frozen=True），计算方法为纯数学无状态计算。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """不可变重试策略配置与延迟时间计算器 (Immutable Retry Policy)。

    Attributes:
        max_retries (int): 允许的最大瞬态故障重试次数，默认为 2。
        base_delay (float): 首次重试的基准退避等待秒数，默认为 0.5 秒。
        max_delay (float): 退避等待时间的硬性天花板秒数，默认为 10.0 秒。
    """
    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 10.0

    def delay(self, retry: int, *, uniform) -> float:
        """计算第 n 次重试所需的退避等待秒数（带随机抖动）。

        【数学计算公式】
        `t = min(max_delay, base_delay * (2 ** retry)) * uniform(0.9, 1.1)`

        Args:
            retry: 当前重试轮次序号 (0-based 整数)。
            uniform: 随机均匀分布生成函数 `uniform(a, b) -> float`。

        Returns:
            float: 最终计算得到的退避等待秒数。
        """
        return min(self.max_delay, self.base_delay * 2**retry) * uniform(0.9, 1.1)

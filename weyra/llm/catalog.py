"""预置模型能力元数据目录（Model Capacity Catalog）。

架构定位与设计哲学:
    本模块维护主流大模型（DeepSeek, OpenAI, Anthropic 等）的硬件级能力基线数据。
    包括真实上下文窗口尺寸（context_window）、单次最大输出 Token 限制（max_output_tokens）、
    是否原生支持长思考链推理（supports_reasoning）以及是否支持服务端提示词缓存（supports_cache）。
    通过硬编码静态安全基线，防止运行时因配置缺失导致错误的截断或越界请求。

开源框架借鉴与设计原则:
    1. PiAgent (packages/ai/src/providers/data/{provider}.json):
       - 直接继承 Pi 项目形式化生成的模型元数据字典结构，保证底层规格定义的工业级对齐。
    2. DeepSeek-Harness:
       - 实行“能力属性（Capacity）与计费资费（Pricing）分离”原则：
         本目录仅记录上下文上限与特性支持等技术能力参数；任何货币汇率、输入/输出/缓存读取阶梯费率
         必须由外部配置（weyra.toml 或环境变量）显式声明，杜绝在核心代码中硬编码易变商业价格。
"""

# 预置模型能力表：键为 (provider_name, model_name) 二元元组
MODEL_CATALOG = {

    ("deepseek", "deepseek-flash"): {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "context_window": 1000000,
        "max_output_tokens": 384000,
        "supports_reasoning": True,
        "supports_cache": True,
    },
    ("deepseek", "deepseek-v4-pro"): {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "context_window": 1000000,
        "max_output_tokens": 384000,
        "supports_reasoning": True,
        "supports_cache": True,
    },
    ("openai", "gpt-4.1"): {
        "provider": "openai",
        "model": "gpt-4.1",
        "context_window": 1047576,
        "max_output_tokens": 32768,
        "supports_reasoning": False,
        "supports_cache": True,
    },
    ("openai", "gpt-4.1-mini"): {
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "context_window": 1047576,
        "max_output_tokens": 32768,
        "supports_reasoning": False,
        "supports_cache": True,
    },
    ("openai", "gpt-4o"): {
        "provider": "openai",
        "model": "gpt-4o",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "supports_reasoning": False,
        "supports_cache": True,
    },
    ("anthropic", "claude-sonnet-4-5"): {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "context_window": 200000,
        "max_output_tokens": 64000,
        "supports_reasoning": True,
        "supports_cache": True,
    },
}

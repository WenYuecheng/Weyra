"""Pinned subset of Pi packages/ai/src/providers/data/{provider}.json.

Copied 2026-10-16 from the local Pi checkout generated catalog. Capacity
metadata only: pricing requires explicit currency/rates in configuration.
"""

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

"""Nonsecret provider configuration with explicit model capacity contracts."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from .contracts import ModelInfo
from .model_runtime import AnthropicProvider, ModelRuntime, OpenAIProvider, ProviderRegistry

PURPOSES = ("main", "compression", "memory", "evolution")
PROVIDER_DEFAULTS = {
    "openai": ("openai", "OPENAI_API_KEY", None),
    "deepseek": ("openai", "DEEPSEEK_API_KEY", "https://api.deepseek.com"),
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY", None),
}


@dataclass(frozen=True)
class Config:
    providers: dict
    routes: dict[str, ModelInfo]
    fallbacks: dict[str, list[ModelInfo]] = field(default_factory=dict)

    mcp_servers: dict[str, dict] = field(default_factory=dict)

    def build_runtime(self):
        registry = ProviderRegistry()
        for name, provider in self.providers.items():
            adapter = AnthropicProvider if provider["protocol"] == "anthropic" else OpenAIProvider
            registry.register(
                name,
                adapter(
                    provider_id=name,
                    api_key_env=provider["api_key_env"],
                    base_url=provider.get("base_url"),
                ),
            )
        runtime = ModelRuntime(registry, self.routes)
        runtime.fallbacks = self.fallbacks
        runtime.mcp_servers = self.mcp_servers
        return runtime


def _provider(name, values):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
        raise ValueError("Invalid provider identifier")
    if set(values) - {"protocol", "api_key_env", "base_url"}:
        raise ValueError("Provider configuration accepts only protocol, api_key_env and base_url")
    defaults = PROVIDER_DEFAULTS.get(name, (None, None, None))
    protocol = values.get("protocol", defaults[0])
    env = values.get("api_key_env", defaults[1])
    url = values.get("base_url", defaults[2])
    if protocol not in {"openai", "anthropic"}:
        raise ValueError("Provider protocol must be openai or anthropic")
    if not isinstance(env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env):
        raise ValueError("api_key_env must name an environment variable")
    if url:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTP endpoint without credentials or query")
    return {"protocol": protocol, "api_key_env": env, "base_url": url}


def load_config(project, overrides=None):
    project = Path(project).resolve()
    path = project / "weyra.toml"
    if path.exists() and not path.resolve().is_relative_to(project):
        raise ValueError("Configuration path escapes project")
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    if set(data) - {"providers", "routes", "fallbacks", "mcp"}:
        raise ValueError("Unknown configuration section")
    providers = dict(data.get("providers", {}))
    routes = {name: dict(value) for name, value in data.get("routes", {}).items()}
    override = {key: value for key, value in (overrides or {}).items() if value is not None}
    if override:
        main = routes.setdefault("main", {})
        provider_id = override.get("provider", main.get("provider", "openai"))
        main["provider"] = provider_id
        provider = dict(providers.get(provider_id, {}))
        if "protocol" in override:
            provider["protocol"] = override["protocol"]
        for key in ("base_url", "api_key_env"):
            if key in override:
                provider[key] = override[key]
        providers[provider_id] = provider
        for key in ("model", "context_window", "max_output_tokens"):
            if key in override:
                main[key] = override[key]
    if "main" not in routes:
        raise ValueError(
            "Configuration requires a main model with explicit context_window and max_output_tokens"
        )
    if set(routes) - set(PURPOSES):
        raise ValueError("Unknown model route purpose")
    fallback_routes = data.get("fallbacks", {})
    if set(fallback_routes) - set(PURPOSES):
        raise ValueError("Unknown fallback purpose")
    if any(not isinstance(values, list) for values in fallback_routes.values()):
        raise ValueError("Fallback routes must be lists")
    all_routes = list(routes.values()) + [
        route for values in fallback_routes.values() for route in values
    ]
    for route in all_routes:
        identifier = route.get("provider")
        if identifier in PROVIDER_DEFAULTS:
            providers.setdefault(identifier, {})
    normalized = {name: _provider(name, values) for name, values in providers.items()}

    def model(route):
        capabilities = {"supports_tools", "supports_reasoning", "supports_cache"}
        if set(route) - (
            {"provider", "model", "context_window", "max_output_tokens"} | capabilities
        ):
            raise ValueError("Unknown model route field")
        if not {"provider", "model", "context_window", "max_output_tokens"} <= set(route):
            raise ValueError(
                "Each model route requires provider, model, context_window and max_output_tokens"
            )
        if route["provider"] not in normalized:
            raise ValueError("Model route references an unknown provider")
        for key in ("context_window", "max_output_tokens"):
            if type(route[key]) is not int:
                raise ValueError("Model capacities must be integers")
        if not isinstance(route["model"], str) or not route["model"].strip():
            raise ValueError("Model identifier must be a nonempty string")
        for key in capabilities & route.keys():
            if type(route[key]) is not bool:
                raise ValueError("Model capability flags must be booleans")
        return ModelInfo(
            route["provider"],
            route["model"],
            route["context_window"],
            route["max_output_tokens"],
            **{key: route[key] for key in capabilities & route.keys()},
        )

    models = {purpose: model(route) for purpose, route in routes.items()}
    fallbacks = {
        purpose: [model(route) for route in values] for purpose, values in fallback_routes.items()
    }
    return Config(
        normalized,
        {purpose: models.get(purpose, models["main"]) for purpose in PURPOSES},
        fallbacks,
        _mcp_servers(data.get("mcp", {})),
    )


def _mcp_servers(section):
    if not isinstance(section, dict) or set(section) - {"servers"}:
        raise ValueError("MCP configuration accepts only servers")
    servers = section.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("MCP servers must be a mapping")
    result = {}
    for name, config in servers.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            raise ValueError("Invalid MCP server identifier")
        if not isinstance(config, dict) or set(config) - {"command", "args", "env"}:
            raise ValueError("MCP server accepts command, args and environment references only")
        command = config.get("command")
        args = config.get("args", [])
        env = config.get("env", {})
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("MCP command must be a nonempty executable name")
        if not isinstance(args, list) or any(
            not isinstance(arg, str) or "\0" in arg for arg in args
        ):
            raise ValueError("MCP args must be a list of strings")
        if not isinstance(env, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key)
            or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value)
            for key, value in env.items()
        ):
            raise ValueError("MCP env must map variable names to environment variable references")
        result[name] = {"command": command, "args": list(args), "env": dict(env)}
    return result

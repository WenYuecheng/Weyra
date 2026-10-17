"""Nonsecret provider configuration with explicit model capacity contracts."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import dotenv_values

from .contracts import ModelInfo, PreparedRequest
from .model_catalog import MODEL_CATALOG
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
    context_policy: dict = field(default_factory=dict)
    inherited_purposes: frozenset[str] = frozenset()
    pricing: dict = field(default_factory=dict)
    _credentials: dict = field(default_factory=dict, repr=False, compare=False)
    route_parameters: dict = field(default_factory=dict)
    fallback_parameters: dict = field(default_factory=dict)

    def safe_summary(self):
        return {
            "providers": self.providers,
            "routes": {p: asdict(m) for p, m in self.routes.items()},
            "fallbacks": {p: [asdict(m) for m in values] for p, values in self.fallbacks.items()},
            "context_policy": self.context_policy,
            "pricing": self.pricing,
            "route_parameters": self.route_parameters,
        }

    def build_runtime(self):
        registry = ProviderRegistry()
        for name, provider in self.providers.items():
            adapter = AnthropicProvider if provider["protocol"] == "anthropic" else OpenAIProvider
            instance = adapter(
                provider_id=name,
                api_key_env=provider["api_key_env"],
                base_url=provider.get("base_url"),
            )
            instance._api_key = self._credentials.get(name)
            instance._credential_snapshot = True
            registry.register(name, instance)
        runtime = ModelRuntime(registry, self.routes)
        runtime.fallbacks = self.fallbacks
        runtime.mcp_servers = self.mcp_servers
        runtime.context_policy = self.context_policy
        runtime.inherited_purposes = set(self.inherited_purposes)
        runtime.pricing = self.pricing
        runtime.route_parameters = self.route_parameters
        runtime.fallback_parameters = self.fallback_parameters
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
    if set(data) - {"providers", "routes", "fallbacks", "mcp", "context_policy", "pricing"}:
        raise ValueError("Unknown configuration section")
    providers = {name: dict(value) for name, value in data.get("providers", {}).items()}
    routes = {name: dict(value) for name, value in data.get("routes", {}).items()}
    dotenv_path = project / ".env"
    if dotenv_path.exists() and not dotenv_path.resolve().is_relative_to(project):
        raise ValueError("Environment configuration path escapes project")
    dotenv = dict(dotenv_values(dotenv_path, interpolate=False)) if dotenv_path.exists() else {}
    environment = dict(os.environ)
    override = {key: value for key, value in (overrides or {}).items() if value is not None}
    # Resolve one layer at a time: provider-specific names win within a layer.
    main = routes.get("main", {})
    provider_id = (
        override.get("provider")
        or environment.get("PROVIDER")
        or environment.get("WEYRA_PROVIDER")
        or dotenv.get("PROVIDER")
        or dotenv.get("WEYRA_PROVIDER")
        or main.get("provider")
    )
    if provider_id is None:
        for layer in (environment, dotenv):
            candidates = [name for name in PROVIDER_DEFAULTS if layer.get(name.upper() + "_MODEL")]
            if len(candidates) == 1:
                provider_id = candidates[0]
                break
    if provider_id is None and (
        override.get("model") or environment.get("MODEL") or dotenv.get("MODEL")
    ):
        provider_id = "openai"
    credentials = {}
    if provider_id:
        main = routes.setdefault("main", {})
        main["provider"] = provider_id
        provider = dict(providers.get(provider_id, {}))
        prefix = provider_id.upper().replace("-", "_")
        for layer in (dotenv, environment):
            model_name = layer.get(prefix + "_MODEL") or layer.get("MODEL")
            if model_name:
                main["model"] = model_name
            for key in ("context_window", "max_output_tokens"):
                value = layer.get(prefix + "_" + key.upper()) or layer.get(key.upper())
                if value is not None:
                    try:
                        main[key] = int(value)
                    except (TypeError, ValueError):
                        raise ValueError("Model capacities must be integers") from None
            url = layer.get(prefix + "_BASE_URL") or layer.get(prefix + "_API") or layer.get("API")
            if url:
                provider["base_url"] = url
            env_name = (
                override.get("api_key_env")
                or provider.get("api_key_env")
                or PROVIDER_DEFAULTS.get(provider_id, (None, prefix + "_API_KEY", None))[1]
            )
            secret = layer.get(env_name) or layer.get(prefix + "_API_KEY") or layer.get("APIKEY")
            if secret:
                credentials[provider_id] = secret
        for key in ("protocol", "base_url", "api_key_env"):
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
            {
                "provider",
                "model",
                "context_window",
                "max_output_tokens",
                "parameters",
                "reasoning_format",
            }
            | capabilities
        ):
            raise ValueError("Unknown model route field")
        metadata = MODEL_CATALOG.get((route.get("provider"), route.get("model")), {})
        route = {**metadata, **route}
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
            reasoning_format=route.get("reasoning_format"),
        )

    models = {purpose: model(route) for purpose, route in routes.items()}
    fallbacks = {
        purpose: [model(route) for route in values] for purpose, values in fallback_routes.items()
    }
    for name, provider in normalized.items():
        if name not in credentials:
            env_name = provider["api_key_env"]
            secret = environment.get(env_name) or dotenv.get(env_name)
            if secret:
                credentials[name] = secret
    policy = data.get("context_policy", {})
    if policy:
        from .context import resolve_context_policy

        resolve_context_policy(policy)
    pricing = _pricing(data.get("pricing", {}))
    parameters = {purpose: dict(route.get("parameters", {})) for purpose, route in routes.items()}
    fallback_parameters = {
        (purpose, parsed.provider_id, parsed.model_id): dict(route.get("parameters", {}))
        for purpose, values in fallback_routes.items()
        for route, parsed in zip(values, fallbacks[purpose])
    }
    config = Config(
        providers=normalized,
        routes={purpose: models.get(purpose, models["main"]) for purpose in PURPOSES},
        fallbacks=fallbacks,
        mcp_servers=_mcp_servers(data.get("mcp", {})),
        context_policy=policy,
        inherited_purposes=frozenset(set(PURPOSES) - set(models)),
        pricing=pricing,
        _credentials=credentials,
        route_parameters=parameters,
        fallback_parameters=fallback_parameters,
    )
    from .model_runtime import _validate_parameters

    runtime = config.build_runtime()
    for purpose, parsed in models.items():
        request = PreparedRequest(
            parsed,
            parameters[purpose].get("cache_system_prefix", ""),
            (),
            purpose=purpose,
            parameters=parameters[purpose],
        )
        try:
            _validate_parameters(request, runtime.registry.get(parsed.provider_id))
            output = parameters[purpose].get("max_output_tokens", parsed.max_output_tokens)
            if type(output) is not int or not 0 < output <= parsed.max_output_tokens:
                raise ValueError("Output limit exceeds configured model capacity")
        except Exception as error:
            raise ValueError("Invalid model route parameters") from error
    return config


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


def check_config(project, overrides=None):
    """Return resolved nonsecret configuration without opening a provider client."""
    from .context import resolve_context_policy

    config = load_config(project, overrides)
    runtime = config.build_runtime()
    main = runtime.resolve("main")
    # Capacity metadata is actual capability; the context pressure reserve is request-local.
    reserve = runtime.parameters_for("main").get("max_output_tokens", main.max_output_tokens)
    policy = resolve_context_policy(config.context_policy, main, reserved_output=reserve)
    summary_model = (
        main
        if (policy.summary_mode == "dsh_checkpoint" and "compression" in runtime.inherited_purposes)
        else runtime.resolve("compression")
    )
    if policy.summarization_provider:
        try:
            summary_model = runtime.resolve_model(
                policy.summarization_provider, policy.summarization_model
            )
        except (KeyError, ValueError):
            raise ValueError(
                "Summary model requires an explicit configured model route or pinned catalog entry"
            ) from None

    if policy.max_tokens > summary_model.max_output_tokens:
        raise ValueError("Summary output limit exceeds configured model capacity")
    summary = config.safe_summary()
    from dataclasses import fields

    summary["context_policy"] = {f.name: getattr(policy, f.name) for f in fields(policy) if f.init}
    return summary


def _pricing(section):
    from .pricing import Price

    if not isinstance(section, dict):
        raise ValueError("Pricing must map provider/model identifiers to explicit CNY rates")
    result = {}
    for identity, values in section.items():
        if not isinstance(identity, str) or "/" not in identity or not isinstance(values, dict):
            raise ValueError("Pricing identity must be provider/model")
        result[identity] = Price.from_dict(values).to_dict()
    return result

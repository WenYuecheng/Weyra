"""非涉密工程配置加载器、模型容量契约校验与运行时组装器 (Configuration Loader & Capacity Contract Verifier)。

【架构定位】
本模块负责解析项目根目录的 `weyra.toml` 配置文件，并协同 `.env` 与环境变量，
完成模型供应商、细分用途路由映射、容灾回退链、MCP 外部服务器、上下文容量策略与计费价格表的组装与静态校验。

【开源与工程设计借鉴】
1. 借鉴 Hermes Agent (`config.yaml` + `.env`) 的行为配置与机密凭据严格分离设计：
   - `weyra.toml` 仅保存模型名称、路由目的、上下文阈值等非涉密架构拓扑；
   - API Key 等高敏凭证严禁落盘入库，统一通过进程环境变量或 `.env` 注入；
2. 借鉴 DeepSeek-Harness 的配置强校验与“错误尽早大声失败”原则 (Misconfiguration Fails Loud)：
   - 严格限定顶层配置节白名单：`{"providers", "routes", "fallbacks", "mcp", "context_policy", "pricing", "skill_evolution"}`，
     一旦出现未知节或字段立即抛出异常拒绝启动，杜绝因拼写错误导致静默退化；
3. 严格的模型物理容量契约 (Model Capacity Contracts)：
   - 结合内置 `MODEL_CATALOG` 严格校验模型的 context_window 与 max_output_tokens，
     防止模型参数虚标或超出容量导致的下游推理中断。
"""

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

PURPOSES = ("main", "compression", "memory", "evolution", "skill_judge")
PROVIDER_DEFAULTS = {
    "openai": ("openai", "OPENAI_API_KEY", None),
    "deepseek": ("openai", "DEEPSEEK_API_KEY", "https://api.deepseek.com"),
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY", None),
}


@dataclass(frozen=True)
class Config:
    """系统完整解析配置对象，具备运行时构建与脱敏摘要输出能力。"""
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
    skill_evolution: dict = field(default_factory=lambda: {"enabled": False})

    def safe_summary(self):
        """生成脱敏且不包含任何真实 API 密钥的配置摘要字典。

        Returns:
            dict: 仅包含公开路由、兜底拓扑、策略与定价参数的脱敏字典。
        """
        return {
            "providers": self.providers,
            "routes": {p: asdict(m) for p, m in self.routes.items()},
            "fallbacks": {p: [asdict(m) for m in values] for p, values in self.fallbacks.items()},
            "context_policy": self.context_policy,
            "pricing": self.pricing,
            "route_parameters": self.route_parameters,
            "skill_evolution": self.skill_evolution,
        }

    def build_runtime(self):
        """基于当前配置实例化完整的 ModelRuntime 运行时调度器。

        【架构定位】
        装配各个 Provider 的适配器实例（AnthropicProvider / OpenAIProvider），
        注入密钥快照，配置路由拓扑、MCP 服务、上下文策略与定价模型。

        Returns:
            ModelRuntime: 包含完整路由能力的模型运行时实例。
        """
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
        runtime.skill_evolution = dict(self.skill_evolution)
        return runtime


def _provider(name, values):
    """校验并规整化单个提供商的基础配置字典。

    【核心不变性与安全约束】
    1. 标识符校验：提供商名称必须符合 `[A-Za-z][A-Za-z0-9_-]*` 命名格式；
    2. 字段白名单：仅接受 protocol, api_key_env, base_url；
    3. URL 安全：base_url 必须为 HTTP/HTTPS 端点，严禁嵌入查询参数、片段或凭据；
    4. 环境变量规约：api_key_env 必须符合环境变量命名规则。

    Args:
        name: 提供商标识符。
        values: 提供商配置字典。

    Returns:
        dict: 规整化后的提供商配置。

    Raises:
        ValueError: 当参数不合法时抛出。
    """
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
    """从工程根目录分层加载并解析 Weyra 运行时系统配置。

    【架构定位】
    系统配置加载核心入口。结合 Hermes 的凭据与行为分离范式与 DeepSeek-Harness 的配置强校验。

    【多层级覆盖合并规则（优先级从低到高）】
    1. 提供商出厂默认值（`PROVIDER_DEFAULTS`）；
    2. 项目静态配置文件（`<project>/weyra.toml`）；
    3. 项目机密凭据文件（`<project>/.env`）；
    4. 操作系统进程环境变量（`os.environ`）；
    5. 命令行启动覆盖参数（`overrides`）。

    【核心不变性与安全约束】
    1. 路径隔离：严禁 `weyra.toml` 与 `.env` 指向项目工程根目录之外（防目录遍历逃逸）；
    2. 字段严格白名单：严禁未知未声明的配置段落；
    3. 凭据快照安全：机密密钥仅存入 `_credentials` 隔离字典，杜绝注入序列化输出。

    Args:
        project: 项目工程根目录路径。
        overrides: 可选的显式覆盖参数字典。

    Returns:
        Config: 解析并校验完毕的只读配置对象。

    Raises:
        ValueError: 当配置文件路径逃逸、配置段未知或参数非法时抛出。
    """
    project = Path(project).resolve()
    path = project / "weyra.toml"
    if path.exists() and not path.resolve().is_relative_to(project):
        raise ValueError("Configuration path escapes project")
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    if set(data) - {"providers", "routes", "fallbacks", "mcp", "context_policy", "pricing", "skill_evolution"}:
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
        """解析单条模型路由配置，合并出厂目录元数据并严格校验字段合法性。"""
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
    evolution = dict(data.get("skill_evolution", {}))
    if set(evolution) - {"enabled", "judge_route", "validation_manifest"}:
        raise ValueError("Unknown skill_evolution field")
    evolution.setdefault("enabled", False)
    evolution.setdefault("judge_route", "skill_judge")
    if type(evolution["enabled"]) is not bool:
        raise ValueError("skill_evolution.enabled must be a boolean")
    if evolution["judge_route"] != "skill_judge":
        raise ValueError("skill_evolution requires the explicit skill_judge route")
    if evolution["enabled"]:
        judge = models.get("skill_judge")
        if judge is None:
            raise ValueError("Enabled skill evolution requires explicit skill_judge route")
        if fallbacks.get("skill_judge"):
            raise ValueError("skill_judge cannot silently fall back to another model")
        manifest = evolution.get("validation_manifest")
        if not isinstance(manifest, str) or not manifest:
            raise ValueError("Enabled skill evolution requires validation_manifest")
        manifest_path = project / manifest
        from .frontmatter import reject_symlinks
        reject_symlinks(manifest_path)
        if not manifest_path.resolve().is_relative_to(project) or not manifest_path.is_file():
            raise ValueError("validation_manifest must be a regular file inside the project")
        evolution["validation_manifest"] = str(manifest_path.resolve())
    parameters = {purpose: dict(route.get("parameters", {})) for purpose, route in routes.items()}
    fallback_parameters = {
        (purpose, parsed.provider_id, parsed.model_id): dict(route.get("parameters", {}))
        for purpose, values in fallback_routes.items()
        for route, parsed in zip(values, fallbacks[purpose])
    }
    config = Config(
        providers=normalized,
        routes={purpose: models.get(purpose, models["main"]) for purpose in PURPOSES
                if purpose != "skill_judge" or purpose in models},
        fallbacks=fallbacks,
        mcp_servers=_mcp_servers(data.get("mcp", {})),
        context_policy=policy,
        inherited_purposes=frozenset(set(PURPOSES) - {"skill_judge"} - set(models)),
        pricing=pricing,
        _credentials=credentials,
        route_parameters=parameters,
        fallback_parameters=fallback_parameters,
        skill_evolution=evolution,
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
    """校验并规整化 MCP (Model Context Protocol) 外部服务配置块。

    【核心不变性与安全约束】
    1. 键名校验：仅允许声明 `servers` 字典段落；
    2. 安全可执行文件：`command` 必须为有效非空执行命令，严禁嵌入空字节 `\0`；
    3. 参数列表强类型：`args` 必须为纯字符串列表，严禁嵌入空字节；
    4. 环境变量引用安全：`env` 映射的键与值均必须严格符合环境变量命名规则。

    Args:
        section: `weyra.toml` 中的 `[mcp]` 配置段落。

    Returns:
        dict: 规整化后的各 MCP 服务配置映射。

    Raises:
        ValueError: 当 MCP 配置不合法时抛出。
    """
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
    """静态检查并返回脱敏的系统解析配置，杜绝在检查阶段实际打开 Provider 网络连接。

    【架构定位】
    用于 CLI `weyra config check`、启动前健康检查与测试用例验证。校验模型容量是否足以承载
    压缩策略与输出上限预算。

    Args:
        project: 项目工程根目录。
        overrides: 可选的覆盖参数。

    Returns:
        dict: 包含脱敏配置与完整解析策略的字典。

    Raises:
        ValueError: 当配置参数冲突或输出预算超载时抛出。
    """
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
    """校验并转换定价策略配置段为标准人民币（CNY）费率字典。

    【架构定位】
    遵循 DeepSeek-Harness 金融级 Decimal 计费标准，将配置表中的百万 token 计费率安全规整化。

    Args:
        section: `weyra.toml` 中的 `[pricing]` 配置段。

    Returns:
        dict: 键为 `provider/model`，值为 Price 字典的映射。

    Raises:
        ValueError: 当计费标识符非 `provider/model` 格式或费率配置非法时抛出。
    """
    from .pricing import Price

    if not isinstance(section, dict):
        raise ValueError("Pricing must map provider/model identifiers to explicit CNY rates")
    result = {}
    for identity, values in section.items():
        if not isinstance(identity, str) or "/" not in identity or not isinstance(values, dict):
            raise ValueError("Pricing identity must be provider/model")
        result[identity] = Price.from_dict(values).to_dict()
    return result

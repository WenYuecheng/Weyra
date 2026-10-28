# 配置参考

行为配置放在项目根目录 `weyra.toml`，凭据放在 `.env` 或进程环境变量中。标准模板见 [weyra.example.toml](../weyra.example.toml)。

## 加载优先级

主路由的可覆盖字段按 CLI 参数 → 进程环境 → 项目 `.env` → TOML → 内置模型元数据解析；同层中提供商专属变量优先于通用变量。`load_config(project, overrides=None)` 只读取指定项目的文件。

支持的顶层段落：`providers`、`routes`、`fallbacks`、`mcp`、`context_policy`、`pricing`、`skill_evolution`。未知字段会报错。

## 提供商与路由

```toml
[providers.deepseek]
protocol = "openai"
api_key_env = "DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com"

[routes.main]
provider = "deepseek"
model = "deepseek-chat"
context_window = 131072
max_output_tokens = 8192

[routes.main.parameters]
max_output_tokens = 4096
```

提供商字段为 `protocol`（`openai` 或 `anthropic`）、`api_key_env`、`base_url`。模型容量按实际端点核实；路由层 `max_output_tokens` 声明容量，`parameters.max_output_tokens` 指定本次请求上限。

用途包括 `main`、`compression`、`memory`、`evolution`、`skill_judge`。未配置的普通辅助用途继承主路由；启用技能演进时，裁判路由必须显式配置。能力可用 `supports_tools`、`supports_reasoning`、`supports_cache` 和 `reasoning_format` 描述。

备用路由采用 `[[fallbacks.main]]` 数组，字段与路由一致。

## 上下文策略

```toml
[context_policy]
threshold_ratio = 0.8
headroom_tokens = 65536
retain_ratio = 0.16
summary_max_tokens = 8192
compaction_retries = 1
max_overflow_retries = 1
summary_mode = "dsh_checkpoint"
```

该组参数与上面的 131072 上下文容量搭配。`threshold_ratio` 控制压力阈值；`headroom_tokens` 留出安全空间；`retain_ratio` 控制保留尾部比例；`summary_max_tokens` 限制摘要输出。还支持 `retain_tokens`、`summary_mode="single_json"` / `"three_parallel"` 和 `model_policies` 的按模型覆盖。容量与输出预算会一起校验；完整参数以 `weyra/core/context.py` 的 `ContextPolicy` 为准。

## 预算与计价

交互式 CLI 使用 `--max-tokens`、`--max-calls`、`--task-token-limit` 控制调用；应用服务使用 `budget_limit_tokens`、`budget_limit_calls`、`budget_limit_cost_cny`。脚本可显式构造 `Budget(cost_limit_cny=...)`。当前 TOML 不接受 `[budget]`。

人民币费用表使用 `[pricing."提供商/模型"]`，字段包括 `currency="CNY"`、`input_per_million`、`output_per_million`、缓存读写费率、`cost_basis`（`exact` 或 `ceiling`）和 `cache_write_billing`（`separate` 或 `included`）。费率需按所用端点填写；配置金额上限后，缺少可用计价会在请求前拒绝调用。`exact` 的精确结算还取决于服务端返回完整 Usage。

## MCP

```toml
[mcp.servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/dir"]

[mcp.servers.filesystem.env]
ACCESS_TOKEN = "FILESYSTEM_ACCESS_TOKEN"
```

`env` 的值引用环境变量名，不是实际凭据。配置后通过 `/mcp-trust filesystem` 授予信任，服务器才可启动；修改命令配置后需要重新信任。

## 技能演进

```toml
[skill_evolution]
enabled = false
judge_route = "skill_judge"
validation_manifest = ".weyra/validation/evidence.json"
```

开启前配置显式 `routes.skill_judge`、对应费率与固定验证清单。裁判不允许隐式继承或备用路由。在线冻结最多 2 个开发及 2 个独立验证样本，只生成 1 个候选；原任务作为开发证据，缺少独立验证不发布。纯文本清单使用 `output_mode = "text"` 与明确的 `rubric`；自定义在线回调需提供 `validation_cases`。完整清单保留给显式评测；窗口、缓存和共用维护预算细节见[技能演进](SKILL_EVOLUTION.md)。

## 环境变量与检查

常见凭据变量：`OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`ANTHROPIC_API_KEY`。主路由可通过 `PROVIDER` / `WEYRA_PROVIDER`、`MODEL`、`CONTEXT_WINDOW`、`MAX_OUTPUT_TOKENS` 覆盖；提供商专属变量形如 `DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL`。通用兼容变量包括 `APIKEY` 和 `API`。

```bash
weyra check-config --project .
```

检查配置、路由容量与凭据就绪情况，不发起模型请求，也不测试端点连通性。

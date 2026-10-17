# 验证记录与未完成项

截至 2026-10-17，工程检查与离线机制验证已执行；CUN 的 DeepSeek 模型已通过最小生成和真实文件读取工具链，完整效果评估正在按固定版本执行。

## 可复现工程证据

运行 `python examples/verify.py`，重新执行完整测试、静态检查、统一入口的两类演示、离线二十场景与五组对照、构建和隔离安装检查。该脚本不调用模型 API。最新命令、退出码、输出及源码哈希保存在 [verification.json](verification.json)，具体离线结果在 [offline-evaluation.json](offline-evaluation.json)。

安装检查使用新虚拟环境安装构建的 wheel，复用开发环境已经安装的 SDK 依赖；在仓库之外的工作目录核对包的实际导入位置、二十场景资源和离线 CLI。它验证打包和入口，不声称完成了所有操作系统或最低依赖版本矩阵。

| 工程范围 | 覆盖证据 |
|---|---|
| 厂商、路由、请求、参数和用量 | `test_model_runtime`、`test_config_runtime_completion` |
| SQLite、迁移、分支、磁盘失败、事务与预算恢复 | `test_sqlite_sessions`、`test_budget_cost`、`test_review_regressions` |
| 固定提示词、阈值、剪枝、UTF-8 边界、usage 锚点 | `test_context_upstream`、`test_context_usage_anchor`、`test_tool_upstream` |
| 辅助模型容量、分段覆盖、费用与发布失败 | `test_summary_segments` |
| 审批期间撤权、并发、取消和工具结果 | `test_tool_runtime`、`test_delivery_boundaries` |
| 子任务、空闲复用、交付、截断及关闭 | `test_subagents`、`test_core_regressions` |
| Memory 可见性、重叠召回、取消与外部更新 | `test_memory_visibility`、`test_memory_cancellation`、`test_knowledge` |
| 技能补丁、来源、部分失败、恢复与回滚 | `test_evolution`、`test_evolution_recovery` |
| MCP 子进程握手、真实调用、Schema 变化、超时和退出 | `test_mcp_stdio`，使用 `data/fixtures/mcp_server.py` |
| 队列、Goal、交互、演示和评估检查器 | `test_agent_completion`、`test_cli`、`test_examples`、`test_evaluation` |

独立复核指出的审批撤权、压缩发布原子性、截断子任务三个问题均增加了先失败、修复后通过的回归测试。运行状态由新生成的证据文件确认，旧的 115 项测试记录不再作为当前证明。

## 离线场景和对照

统一入口为 `weyra demo --scenario analysis|writing --offline`。两种场景共用 Agent、工具、SQLite 和模型运行层。演示会话与产物保留在目标项目 `.weyra/demos/`。

`weyra eval --offline` 执行 20 个基准场景和 30 个专项对照结果，共 50 个结果。基准分母始终为 20；专项同时报告 20 的总分母和实际适用分母，失败、错误和预算停止均保留。

| 专项 | 分组 | 结果数 |
|---|---|---:|
| 缓存 | stable / rebuild | 4 |
| 压缩 | 普通单次摘要 / DSH 检查点 / 三层并行实验 | 6 |
| 子任务 | single / spawn / fork | 6 |
| Memory | absent / 历史空格匹配召回 / fixed | 6 |
| 技能演化 | full_merge / experience_bounded | 8 |

这些脚本实际执行工具和辅助调用，通过落盘产物、读取事实与机制轨迹验收。它们使用固定模型响应，验证工程行为；写作类夹具主要检查约束和适用边界，不能用其成功率证明文章质量、缓存收益或真实模型提升。压缩对照用固定历史和相同完整尾部构造，避免付费生成历史；这不代表小夹具触发了生产默认压力阈值。真实首字延迟、缓存用量未获得时保留未知。

## 真实接入与费用

[真实验证记录](live-validation.json) 和 [价格来源](真实验证配置来源.json) 分开保存。密钥仅保存在本地被忽略的配置中。用户提供的 CUN default 分组价格用于计费，公开状态接口提供美元兑人民币换算值 6.95；不使用更低的基础价格代替分组价格。

| CUN 模型 ID | 真实接入结果 |
|---|---|
| `glm-5.3-flash` | 四次生成尝试失败，包括最小请求 HTTP 504；服务端用量未知 |
| `gpt-5.6-luna` | 最小请求 HTTP 504；服务端用量未知 |
| `DeepSeek-V4.1-Flash` | 最小生成通过；Agent 实际读取文件并据证回答通过；用于固定模型效果评估 |

DeepSeek 按动态计费的 2 倍 default 分组价格预留，作为保守费用上界；缓存写入包含在普通输入中，没有另加一个未提供的写入单价。GPT 标准分档在显式配置的容量内适用。未知用量保留完整预留，不能记成免费。20 元根账本 `validation-live-cny20` 跨模型、命令和中断保留，所有失败和调试尝试都计入。

首次冒烟子批次合计八次物理请求，未超过两万 token。后续二十场景及专项对照属于同一总额内的独立阶段。中断的工程诊断运行保留轨迹和费用；修复后以新的源码指纹建立评估会话，不将不同版本结果合并成一组。

真实评估结果会逐场景保留失败、超时、预算停止及检查依据。每场景最多等待 180 秒；超时会取消并收敛当前任务，不把未生成的产物判为成功。首字延迟从客户端物理请求开始计至首个文本事件，包含网络等待；只有工具调用而没有文本的响应不填首字延迟。缓存字段未返回时仍为未知。

原生 OpenAI、DeepSeek、Anthropic 服务未取得逐厂商真实验收；CUN 是自定义兼容厂商，不能将代理上的模型名称当成原生服务验证。写作夹具检查事实和格式约束，文章质量人工复核及科研级多模型、多随机种子实验仍后置。

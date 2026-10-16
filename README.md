# Weyra

**支持统一多厂商接入、缓存友好上下文与经验驱动技能演化的轻量 Python Agent Harness。**

Weyra 将模型提出的意图转为可检查的执行过程：冻结请求、验证参数、执行时授权、记录副作用、提交工具结果，再决定继续或停止。主任务、子任务、摘要和技能维护共用运行层与预算账本。

## 快速运行

需要 Python 3.11+，当前进程管理实现面向 macOS/Linux。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/weyra --help
.venv/bin/weyra --demo --project /tmp/weyra-demo
.venv/bin/python examples/project_analysis.py
.venv/bin/python examples/personalized_writing.py
```

三个演示均使用脚本化模型，实际运行文件工具、会话记录或技能补丁，不访问模型服务。它们验证工程链路，不证明真实模型质量或缓存收益。

## 使用已有模型配置

配置只保存环境变量名称，不保存密钥。Weyra 在真正调用厂商时读取该环境变量；不自动搜索、复制或修改其他应用的凭据。

在需要操作的项目目录创建 `weyra.toml`，可参照 `weyra.example.toml`。已有兼容接口可配置独立厂商身份：

```toml
[providers.workspace]
protocol = "openai"
base_url = "https://your-existing-endpoint.example/v1"
api_key_env = "API_KEY"

[routes.main]
provider = "workspace"
model = "your-existing-model-id"
context_window = 65536
max_output_tokens = 2048
```

示例容量只是配置示例。请按实际模型容量设置；未知模型不猜测窗口。`openai`、`deepseek`、`anthropic` 内置厂商默认端点/环境变量名称；兼容厂商可自由命名。

```sh
.venv/bin/weyra --project /path/to/project "分析项目，先提供证据和计划"
.venv/bin/weyra --project /path/to/project --max-tokens 30000 --max-calls 10 --no-evolution
.venv/bin/weyra --project /path/to/project --resume
```

`compression`、`memory`、`evolution` 可分别定义路由，缺省沿用 `main`。显式故障切换链写入 `[fallbacks]`，默认关闭。输入、输出、辅助调用和子任务共用额度；无用量数据时按预留保守记账，不虚构费用。

MCP 服务器通过配置声明，首次发现前需在交互中执行 `/mcp-trust <name>`。配置本身不能自授信任；命令或参数变化使旧信任失效。环境映射的值是已有变量名称：

```toml
[mcp.servers.local]
command = "python3"
args = ["tools/server.py"]
env = { SERVICE_TOKEN = "EXISTING_SERVICE_TOKEN" }
```

## 交互

```text
/steer 补充当前任务约束       当前完整工具批次后接纳
/followup 下一项任务         当前任务完成后执行
/cancel                     取消当前任务及关联活动子任务
/goal 明确持续目标           显式开启有界续行
/tree                       查看会话条目
/branch <record-id>          从完整边界切换历史分支
/fork <new-session-name>     创建独立会话
/memory                     查看项目记忆
/memory save 名称 "事实"      保存项目事实
/skills                     查看技能目录
/skill-history "名称"        查看版本及来源
/skill-rollback "名称" 1.0.0  恢复内容并创建递增新版本
/refresh                    刷新项目规则和目录基线
/usage                      查看包含辅助调用的用量
/mcp                        查看服务器与信任状态
/mcp-trust <name>            显式信任当前服务器配置；发现时才启动
/quit                       等待关闭与资源清理
```

默认模式允许项目内读取，修改和 Shell 需要审批。`plan` 仅允许读取及指定计划文件；`acceptEdits` 允许文件编辑，Shell 仍审批；`dontAsk` 拒绝需审批的操作；`bypassPermissions` 为显式跳过确认模式。工具路径限制在项目目录，Shell 获准后仍具有宿主进程权限，不是 OS 沙箱。

## 核心能力

| 能力 | 实现 |
|---|---|
| 多厂商 | 实例级 Provider 注册，OpenAI-compatible / Anthropic 流式适配，厂商与协议分离 |
| 稳定请求 | 固定 System、排序工具声明、不可变请求快照、前缀指纹和变化原因 |
| 长程上下文 | 原文外置、压力缩减、三层结构化摘要、完整尾部、一次 overflow 恢复 |
| 知识加载 | 中文词法检索、最多五条 Memory、按需技能、正文与资源哈希固定 |
| 执行与并发 | Schema 校验、执行时权限、安全读池、独占屏障、有序结果提交 |
| 子任务 | spawn/fork、后台任务、等待、通信、中断、根预算及父级权限约束 |
| 会话 | 单写者 JSONL、分支投影、检查点、坏尾记录恢复、未知副作用不重放 |
| 后台进程 | 作业 ID、磁盘输出、等待/取消/交接、进程组清理 |
| MCP | 先信任后启动、稳定搜索/调用桥、真实 Schema 校验、共享连接所有权 |
| 技能演化 | 延迟反馈、经验参与维护、两处以内补丁、来源、快照及版本回滚 |

## 验证与设计资料

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests examples
.venv/bin/python -m build
```

- [架构与行为边界](docs/ARCHITECTURE.md)
- [技术来源与采用范围](docs/SOURCES.md)
- [验证方法和后续效果实验](docs/VALIDATION.md)
- [实施追踪](docs/IMPLEMENTATION.md)
- [二十个固定效果检查场景](examples/scenarios.json)

Weyra 借鉴 DSH、Pi、Hermes 的运行时组织，以及 WikiSkill、SkillOpt 的经验与技能修改机制。具体实现与研究实验边界分别记录；不将上游成绩作为本项目成绩。

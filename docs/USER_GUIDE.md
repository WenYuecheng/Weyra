# 使用指南

Weyra 面向本地项目任务，提供流式 CLI、Textual TUI 和可嵌入服务。建议使用 Python 3.11+ 与 macOS / Linux。

## 安装与首次体验

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,tui]'
weyra demo --scenario analysis --offline --project /tmp/weyra-analysis
weyra demo --scenario writing --offline --project /tmp/weyra-writing
```

离线演示使用预设模型响应并实际执行本地工具，适合熟悉运行流程。

```bash
cp weyra.example.toml weyra.toml
cp .env.example .env
# 填写端点与密钥
weyra check-config --project .
weyra --project . --ui plain
weyra --project . --ui tui
weyra "阅读项目并生成结构说明" --project .
```

配置字段见[配置参考](CONFIGURATION_REFERENCE.md)。

## 权限与工具执行

| 模式 | 行为 |
| --- | --- |
| `default` | 只读工具直接运行，其他操作需审批 |
| `plan` | 只读分析，允许修改专用计划文件，禁止 Shell |
| `acceptEdits` | 允许常规文件编辑，Shell 等仍需审批 |
| `dontAsk` | 拒绝非只读操作，不弹出审批 |
| `bypassPermissions` | 跳过一般审批，仍执行路径和参数校验 |

通过 `--permission-mode` 选择模式。文件工具限制在项目目录；Shell 命令在宿主环境执行，项目工作目录和进程组用于路径组织与生命周期管理，不提供容器级文件系统或网络隔离。需要运行不可信代码时，应在容器中启动任务环境。

## 会话与长任务

- `weyra --resume --project .` 恢复最近会话，`--resume <id>` 指定会话。
- `/plan on` 进入计划模式；`/plan off` 恢复进入前的权限。
- `/compact` 生成结构化检查点；完整历史仍保存在事件日志中。
- `/clear` 清空活动上下文、旧排队输入和当前目标，保留会话历史与累计账本。
- `/branch <record-id>` 从已有事件继续；`/fork [name]` 创建独立会话副本。
- `/steer <text>` 插入运行中的引导；`/followup <text>` 排队后续任务。

CLI 和 TUI 的指令、快捷键见[交互参考](CLI_TUI_REFERENCE.md)。

## 技能、记忆与外部工具

技能目录使用 `SKILL.md`；`/skills` 查看目录，`/skill-history <name>` 查看更新，`/skill-rollback <name> <version>` 发布回滚版本。自动演进默认关闭，配置独立裁判与验证清单后开启，见[技能演进](SKILL_EVOLUTION.md)。

`/memory` 查看事实记忆。MCP 配置放在 `[mcp.servers.<name>]`；`/mcp-trust <name>` 显式确认服务器命令，`/mcp` 查看已配置服务器。

`/cost` 查看金额及其精确、估算或未知依据。应用集成可配置人民币上限，模型调用会先预留，再按 Usage 结算。

## Python 接入与验证

[Python API](API_REFERENCE.md) 提供真实签名；完整离线示例可执行 `python -m examples.sdk_service`。

```bash
python -m pytest -q
weyra eval --offline --project /tmp/weyra-eval
```

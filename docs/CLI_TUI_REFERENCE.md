# CLI / TUI 交互参考

## 启动

```bash
weyra --project . --ui plain
weyra --project . --ui tui
weyra "阅读项目并总结执行链路" --project .
weyra --project . --resume
weyra --project . --resume <session-id>
```

`--ui auto` 根据终端与依赖选择界面。Textual 依赖通过 `pip install -e '.[tui]'` 安装。CLI 为原生流式文本输出。

常用参数：`--provider`、`--protocol openai|anthropic`、`--model`、`--base-url`、`--api-key-env`、`--context-window`、`--max-output-tokens`、`--permission-mode`、`--max-tokens`、`--max-calls`、`--max-steps`、`--task-token-limit`、`--no-memory`、`--no-evolution`、`--no-icons`。完整参数使用 `weyra --help`。

## 运维子命令

```bash
weyra check-config --project .
weyra sessions --project .
weyra demo --offline --scenario analysis --project /tmp/weyra-analysis
weyra demo --offline --scenario writing --project /tmp/weyra-writing
weyra eval --offline --project /tmp/weyra-eval
weyra export-session --project . --session-id <id> --file /tmp/session.jsonl
weyra import-session --project . --file /tmp/session.jsonl
```

`demo` / `eval` 的在线模式会调用模型，可用 `--max-cost-cny`、`--max-calls`、`--max-tokens` 和 `--budget-id` 约束；这些参数属于运维子命令，交互启动参数以 `weyra --help` 为准。

## 会话指令

| 指令 | 行为 |
| --- | --- |
| `/help` | 查看帮助 |
| `/cancel` | 取消当前轮次 |
| `/steer <text>` | 步骤间插入引导 |
| `/followup <text>` | 排队后续任务 |
| `/plan on|off` | 进入计划模式或恢复原权限；允许专用计划文件写入 |
| `/compact` | 手动压缩上下文 |
| `/clear` | 清空活动投影、排队输入与目标，保留历史与账本 |
| `/branch [record-id]` | 从指定事件继续，默认当前叶节点 |
| `/fork [name]` | 创建独立会话副本 |
| `/tree` | 查看事件树 |
| `/cost`、`/usage`、`/budget` | 查看费用依据、用量与预算 |
| `/skills` | 查看技能目录 |
| `/skill-history <name>` | 查看版本记录 |
| `/skill-rollback <name> <version>` | 发布回滚版本 |
| `/memory` | 查看记忆 |
| `/mcp` | 查看已配置 MCP 服务器 |
| `/mcp-trust <name>` | 显式信任服务器命令 |
| `/quit` | 退出 |

影响会话状态的指令需等待当前任务结束；清空和分支还受子任务与维护状态约束。

## TUI 快捷键

| 快捷键 | 动作 |
| --- | --- |
| Ctrl+P / F2 | 会话选择 |
| F4 | 模型查看 |
| F3 / Ctrl+K | 技能查看 |
| F1 | 帮助 |
| Enter / Shift+Enter | 发送 / 换行 |
| Ctrl+C | 运行时取消；空闲且有草稿时清空；空闲且无草稿时退出 |

工具卡片支持展开结果，大输出通过查看器分页读取。提交失败时保留输入草稿。权限模式见[使用指南](USER_GUIDE.md)。

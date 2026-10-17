# Weyra

Weyra 是独立的轻量 Python agent harness，支持项目分析、个性化写作、工具执行、会话恢复、Memory 与 Skills 维护。实现位于 `agents/`，Python 包名为 `agents`，命令入口为 `agents.main:main`。

## 安装与离线体验

要求 Python 3.11 及以上。在仓库根目录执行：

```sh
python -m pip install -e '.[dev]'
weyra demo --scenario analysis --offline --project /tmp/weyra-analysis
weyra demo --scenario writing --offline --project /tmp/weyra-writing
weyra eval --offline --project /tmp/weyra-eval
pytest
ruff check agents tests examples
```

离线演示使用脚本响应，实际执行工具并将会话、报告和产物保存在目标项目 `.weyra/demos/`；离线分数验证执行路径，不能证明模型质量或机制收益。`examples/` 保留两个场景入口，统一使用上述 CLI。

## 配置与真实运行

将 `weyra.example.toml` 复制为目标项目的 `weyra.toml`，按实际端点核对容量；将 `.env.example` 复制为 `.env` 并在本地填写密钥。不要提交密钥。

```sh
weyra check-config --project /path/to/project
weyra demo --scenario analysis --provider deepseek --project /path/to/project
weyra demo --scenario writing --provider deepseek --project /path/to/project
weyra '分析这个项目的结构和主要风险' --project /path/to/project
```

配置优先级为 CLI > 进程环境 > 项目 `.env` > TOML > 默认值；支持 `APIKEY`、`API`、`MODEL` 兼容变量。`main`、`compression`、`memory`、`evolution` 可分别路由，未配置的用途继承主模型。未知模型必须显式指定上下文容量与输出上限。`check-config` 只检查配置，不发模型请求，也不验证密钥是否有效。

示例中的 DeepSeek 容量仅为说明：`C=131072`、模型输出上限 `8192`、请求输出预算 `R=4096`、压缩预留 `H=65536`、压缩输出上限显式覆盖为 `8192`。请以实际服务为准。

## 状态与预算

`.weyra/state.db` 是结构化运行事实的唯一存储；Memory 与 Skills 使用 Markdown。JSONL 用于兼容导入和导出，不作为主存储。

```sh
weyra sessions --project /path/to/project
weyra export-session --project /path/to/project --session-id SESSION_ID --file /tmp/session.jsonl
weyra import-session --project /path/to/project --file /tmp/session.jsonl
weyra --project /path/to/project --resume latest
```

真实演示和评测支持 `--max-cost-cny 20`。同一项目的真实 demo/eval 默认复用持久化总账 `validation-live-cny20`，重启命令也不会清零费用。`--budget-id` 可显式选择另一项独立活动，本轮验收始终使用默认总账，额度为人民币 20 元。调用前按完整模型容量保守预留费用，返回后区分精确费用、上界与未知费用。价格仅接受显式人民币单价；控制台组美元价格须来自已核实来源并注明汇率，示例不猜测价格。真实运行前配置有效价格。

默认权限模式可能要求确认写入或执行；交互中使用 `/help` 查看 steering、follow-up、取消、分支、Memory、Skills、MCP 等命令。

## 验证边界

工程验证、真实调用验证、效果验证分别记录。评测清单包含 20 个场景，另有缓存、压缩、子任务、Memory、技能演化五组独立机制对照；每组仅改变对应机制，不构造全部组合。当前 287 项工程测试通过；CUN DeepSeek 的真实基准为 20/20，两类真实演示已通过，整轮保守记账约 11.13 元；五组对照包含失败与错误，完整结果、独立复测及解释边界见 [效果报告](wiki/EFFECTS.md) 和 [验证记录](wiki/VALIDATION.md)。

运行 `python examples/verify.py` 可重新生成工程证据，包括测试、静态检查、演示、离线对照、构建与隔离安装检查。

当前交付状态见 [阶段完成清单](wiki/COMPLETION.md)，简历口径见 [项目展示](wiki/PROJECT_SHOWCASE.md)。

进一步阅读：[架构](wiki/ARCHITECTURE.md)、[来源对齐](wiki/SOURCES.md)、[实现与验证](wiki/IMPLEMENTATION.md)、[研究边界](paper/README.md)。

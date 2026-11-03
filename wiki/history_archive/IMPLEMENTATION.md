# 实现与验证

## 模块

| 文件 | 职责 |
|---|---|
| `agents/main.py`、`commands.py`、`ui.py` | CLI、单输入所有权与流式显示 |
| `contracts.py`、`model_runtime.py`、`config.py` | 请求契约、Provider、用途路由、容量校验 |
| `session.py` | SQLite 事实、分支、恢复、JSONL 交换 |
| `context.py`、`prompt.py`、`output.py` | 压力预算、检查点、预览与清理 |
| `tools.py`、`tool_runtime.py`、`jobs.py`、`mcp_client.py` | 本地工具、调度、任务、MCP |
| `subagent.py`、`runtime_state.py` | 子任务、Goal、共享预算 |
| `memory.py`、`skills.py`、`skill_evolution.py`、`knowledge.py` | Markdown 知识、技能快照、有界演化 |
| `demos.py`、`evaluation.py`、`data/scenarios.json` | 双场景演示与可执行评测 |

## 检查方式

在仓库根目录运行 `pytest` 与 `ruff check agents tests examples`。发布前还应验证安装后的 `weyra` 命令与打包资源，不以仓库内运行成功替代安装验证。

`weyra check-config` 不打开 Provider 网络连接；示例必须先放入独立项目目录并命名为 `weyra.toml`。真实配置的密钥与私有端点不进入文档或报告。

`weyra eval --offline` 运行 20 个基准场景，校验真实生成的文件及成功工具记录，另执行五组单因素机制对照：缓存 stable/rebuild，压缩 plain/dsh/three_layer，子任务 single/spawn/fork，Memory absent/baseline/fixed，演化 full_merge/experience_bounded。对照使用各自适用场景，报告同时保留总分母 20 和适用分母，不能混淆。结果写入 SQLite，可恢复已落盘的场景。

离线脚本只能证明路径、边界与持久化工作。真实模型验证仍须按同一场景执行，记录失败、错误、预算停止和未执行；所有真实调用共用人民币 20 元总预算。工程检查状态、真实调用状态、机制效果状态独立陈述。任何完成或收益结论都必须能回溯到对应运行证据；本页不替代验证报告。

## 尚需研究的范围

当前采用固定夹具和局部工程机制，尚未完成论文级任务抽样、随机重复、显著性分析、盲评与长期演化研究。完整研究计划后置，不能把上游论文数字或脚本成功率作为 Weyra 的效果数字。

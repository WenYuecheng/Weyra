# 来源与实现对齐

只读源码基线：DSH `d743267388641bc76f17c45ce8b4c231aed1d32c`；Pi `4270522473e677ddba7b093caa0064a3df51d459`；Hermes `590af66a58c2b3eb3c5edd1dcfefa09c625ae18a`。本表记录机制来源和差异，不表示完整复现。

| 来源 / 固定版本 | 上游文件 / 符号 | 上游默认 | Weyra 生效行为 | 差异 | 对应测试 |
|---|---|---|---|---|---|
| [DSH](https://github.com/deepseek-ai/deepseek-harness/tree/d743267388641bc76f17c45ce8b4c231aed1d32c) | `packages/compaction/compaction-basic/src/config.ts` / `resolveConfig` | 压力 0.8、H=65536、retain=0.16、maxTokens=H、压缩/溢出重试各 1 | `ContextPolicy` 同值；按请求 R 计算阈值 | 小输出模型需显式覆盖 summary_max_tokens | `test_context_upstream.py`、`test_config_runtime_completion.py` |
| DSH，同上 | `packages/compaction/compaction-basic/src/summarizer.ts` / 检查点指令与前言 | 固定英文八节检查点、同前缀重放 | `agents/prompt.py`；原文保存在 `data/upstream/`，含哈希元数据 | single_json、three_parallel 为实验扩展 | `test_context_upstream.py` |
| DSH，同上 | `packages/compaction/compaction-tool-result-pruner/` / 工具结果清理 | 超过 8192 码点，头 4096、尾 1024、固定 marker | `output.prune_tool_result` 保留同一界限与标记 | 原始结果仍在 SQLite 事实中 | `test_context_upstream.py`、`test_tool_upstream.py` |
| DSH，同上 | `packages/compaction/compaction-basic/src/` / token 估计 | 字符数 / 4 | `context.estimate_tokens` 使用 UTF-8 字节 / 2 | 显式保守启发式；金额预留使用完整容量 | `test_context_usage_anchor.py`、`test_budget_cost.py` |
| [Pi](https://github.com/earendil-works/pi/tree/4270522473e677ddba7b093caa0064a3df51d459) | `packages/coding-agent/src/core/tools/truncate.ts` / `DEFAULT_MAX_LINES`、`DEFAULT_MAX_BYTES`；`read.ts`、`bash.ts` | 2000 行、50 KiB；read 头部、shell 尾部 | `output.truncate_head/truncate_tail`，UTF-8 字节边界 | Python 实现，原始输出单独保存 | `test_tool_upstream.py` |
| Pi，同上 | `packages/coding-agent/src/core/tools/grep.ts`、`truncate.ts` / grep limit、`GREP_MAX_LINE_LENGTH` | 100 条、500 字符/条、50 KiB | 同界限预览 | 不把预览当作原始事实 | `test_tool_upstream.py` |
| [Hermes](https://github.com/NousResearch/hermes-agent/tree/590af66a58c2b3eb3c5edd1dcfefa09c625ae18a) | `agent/agent_runtime_helpers.py` / `_raw_cache_ttl_from_config`；`agent/prompt_caching.py` | Anthropic 缓存 TTL 5m | `AnthropicProvider` 默认 5m；稳定系统前缀与动态事实分离 | 命中需读取实际 usage，不由设计推定 | `test_model_runtime.py`、`test_context_usage_anchor.py` |
| DSH，同上 | `packages/core/agent-loop/src/constants.ts` / `DEFAULT_MAX_PARALLEL_TOOL_CALLS` | 10 | 安全只读滚动池 10，写入和状态变化独占 | Python 适配，有序准备和提交 | `test_tool_upstream.py` |
| DSH，同上 | `packages/llm/llm/src/retry-policy.ts` / 重试默认 | 额外 5 次；500ms 起始、10000ms 上限、0.1 抖动 | 额外 2 次；同退避；主请求含恢复和切换最多 4 次物理尝试 | 按用户低成本要求显式缩减 | `test_config_runtime_completion.py` |
| Pi，同上 | `packages/agent/src/agent.ts` / `PendingMessageQueue` | steering、follow-up 均 one-at-a-time | 两队列逐条接纳，持久化输入 ID 与消费确认 | 准备失败保留 pending | `test_agent_completion.py`、`test_review_regressions.py` |
| Hermes，同上 | `hermes_state_common.py` / sessions、messages、session_model_usage 表 | SQLite 结构化状态 | `session.SessionStore`，WAL、FULL、单写者、事务、内容去重 | 轻量事件表与收件箱；不复制网关表 | `test_sqlite_sessions.py` |
| Weyra 工程限制 | `subagent.py`、`runtime_state.py` | 不适用 | 子任务并发 3/深度 1；Goal 续行 3/无进展 2 | 用户方案限制，不宣称上游默认 | `test_subagents.py`、`test_agent_completion.py` |
| Weyra 工程限制 | `memory.py`、`skill_evolution.py` | 不适用 | Memory 5 条/8 秒/4 KiB/60 KiB；补丁 2×1000 字符；经验 10×300、注入 2000 字符 | 项目预算边界 | `test_memory_visibility.py`、`test_evolution.py`、`test_knowledge.py` |
| [WikiSkill 2608.27454v1](https://arxiv.org/abs/2608.27454v1) | 经验、持续知识与执行技能分离 | 论文设计 | 保留经验、来源和适用边界 | 完整论文研究后置 | `test_knowledge.py` |
| [SkillOpt 2605.23904v1](https://arxiv.org/abs/2605.23904v1) | 四种编辑操作与验证优化 | 论文四种编辑操作 | 采用有界局部修改与校验 | 两处补丁是 Weyra 限制，非论文默认；未声明效果复现 | `test_evolution.py`、`test_evolution_recovery.py` |

本地参考仓库分别为 `/Users/wenyuecheng/code/python/deepseek-harness`、`/Users/wenyuecheng/code/python/piagent`、`/Users/wenyuecheng/code/python/hermes-agent`，仅用于只读对照。上游论文和仓库的指标不作为 Weyra 实测结果。测试文件是机制回归入口，具体执行状态以验证报告为准。

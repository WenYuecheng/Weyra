# 框架借鉴与 Weyra 的工程贡献

Weyra 围绕 Python 本地 Agent 的持续运行，将执行循环、会话树、工具治理、上下文压缩与技能验证组织到统一运行时。以下说明设计来源和项目中的具体实现，便于读者沿源码理解机制。

| 来源 | 借鉴的机制 | Weyra 中的实现 |
| --- | --- | --- |
| [Pi](https://github.com/earendil-works/pi/tree/4270522473e677ddba7b093caa0064a3df51d459) / [my-pi-agent](https://github.com/zxj-2023/my-pi-agent) / dg-ai-notes | 执行循环分层、会话树、steering / follow-up、工具消息配对、缓存前缀组织 | `core/loop.py`、`core/harness.py`、`coding/lifecycle.py`、`session.py` |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/tree/d743267388641bc76f17c45ce8b4c231aed1d32c) | 八节检查点、工具输出中间剪枝、预算预留、目标推进、作业与子任务治理 | `core/context.py`、`output.py`、`core/state.py`、`jobs.py`、`subagent.py` |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent/tree/590af66a58c2b3eb3c5edd1dcfefa09c625ae18a) | 行为与凭据分开配置、可读 Markdown 记忆、技能目录 | `config.py`、`memory.py`、`skills.py` |
| [WikiSkill，2026](https://arxiv.org/abs/2608.27454) | 执行经验、持久知识和技能的分离与积累 | `knowledge.py`、`skill_evolution.py` |
| [SkillOpt，2026](https://arxiv.org/abs/2605.23904) | 有界文本编辑、验证改善驱动的技能更新 | `skill_evolution.py`、`skill_judge.py` |

## 集成贡献

Weyra 将这些机制接入同一条可审计执行链：

1. **统一的执行治理**：权限检查、模型用途路由和共享预算贯穿主任务、辅助调用与子任务。
2. **持久化与投影配合**：SQLite 会话树保存事实，独立上下文投影服务模型请求，分支、清空与恢复保留历史依据。
3. **技能更新的工程门禁**：固定任务在临时工作区真实运行，产物硬断言与次序交换匿名裁判共同形成发布条件；快照和审计记录支持版本追踪。
4. **轻量可嵌入接口**：协议、执行核心、领域装配和 UI 分层，既可直接运行 CLI / TUI，也可通过服务 API 接入应用。

这套组合的价值体现在持续任务、预算治理和可检查技能更新，相关结果见[工程评测](BENCHMARKS.md)。

## 代码与研究来源

my-pi-agent 的循环、宿主装配和 Hook 接口改编版本及完整 MIT 许可保留在[第三方声明](../THIRD_PARTY_NOTICES.md)。DSH 检查点提示词保留了上游原文及 MIT 许可，相关文件和版本同列于声明中。论文按实际 arXiv 版本引用。验证分数改善是 SkillOpt 已有原则，Weyra 的贡献在于本项目中的工具重执行、裁判协议和发布恢复链路，共同服务于可检查的技能更新。

# 技术来源与实际采用内容

Weyra 的技术贡献是将明确的运行契约整合为轻量 Python 实现，并为项目场景增加一致的记录、预算、版本和恢复边界。以下是设计参考，不表示完整复现。

| 来源 | 参考机制 | Weyra 的采用方式 |
|---|---|---|
| [DSH](https://github.com/deepseek-ai/deepseek-harness) | 请求冻结、原始事实与模型投影、有界调度、spawn/fork、Goal | 统一检查点、完整批次、可追踪的子任务和有界续行 |
| [Pi](https://github.com/earendil-works/pi) | Provider 组合、规范消息、双队列、会话分支 | 实例注册表、公共循环、Steering/Follow-up、JSONL 投影 |
| [Hermes](https://github.com/NousResearch/hermes-agent) | 稳定 Prompt、长期事实快照、阶段式执行、资源清理 | 稳定基线和动态上下文分离、任务与会话所有权 |
| [WikiSkill](https://arxiv.org/abs/2608.27454) | 原始经验、持续知识、执行技能分离 | 维护时读取少量经验，保留来源和反例边界 |
| [SkillOpt](https://arxiv.org/abs/2605.23904) | 有界文本修改及验证优化 | 第一版采用局部修改预算；效果验证门禁后置 |

源码研究基线为 DSH `d7432673`、Pi `427052247`、Hermes `590af66a`。上游后续版本可能变化。

Weyra 没有引入完整插件容器、事务任务框架、SQLite、多租户网关或分布式工作节点。技能知识库保持少量本地记录，正文更新以确定性校验约束。上游论文指标不作为 Weyra 的实测结果。

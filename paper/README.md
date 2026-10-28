# 研究方法与工程实现

Weyra 将持久知识积累与验证驱动的技能优化接入同一套 Agent 运行链路，让每次更新具备来源、任务证据和版本记录。论文提供方法依据，项目聚焦这些方法在本地工具环境中的落地。

| 论文 | 引用版本 | 方法与项目映射 |
| --- | --- | --- |
| [AutoSkill: Experience-Driven Lifelong Learning via Skill Self-Evolution](https://arxiv.org/abs/2603.01145v2) | arXiv:2603.01145v2，2026-03-05 | 从近期用户经验提炼和维护可复用技能 |
| [WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution](https://arxiv.org/abs/2608.27454v1) | arXiv:2608.27454v1，2026-08-27 | 将执行经验、积累知识和可执行技能分开组织；对应知识积累与技能维护链路 |
| [SkillOpt: Executive Strategy for Self-Evolving Agent Skills](https://arxiv.org/abs/2605.23904v1) | arXiv:2605.23904v1，2026-05-22 | 有界文本编辑与验证分数严格改善的接受原则；对应局部补丁与验证发布 |

Weyra 在工程实现中组合了受控临时工作区重执行、产物硬断言、次序交换匿名裁判和版本恢复机制。固定任务提供可检查的执行结果，匿名次序交换帮助识别位置偏好，版本记录保存更新决策与回退依据。

[AutoSkill 官方 SkillEvo](https://github.com/ECNU-ICALK/AutoSkill/blob/main/SkillEvo/README.md) 是上游实现参考：冻结真实回放，区分 `mutate_dev` 与 `promotion_test`，使用固定候选预算，样本不足时保留孵化状态。Weyra 复用原 BearCode 的原问题、回答及下一轮反馈窗口，采用每轮最多 4 例、1 个候选和共用维护预算；这些是本项目的轻量参数，不是论文原始设置。SkillOpt 的哈希缓存扩展为绑定资源、样本、规则和执行配置；历史证据不自动变成在线全量回归。

这一组合服务于项目的核心目标：让技能演进融入日常任务，同时保持更新过程可验证、可追溯。实现细节与评测分别见：

- [WikiSkill 方法映射](WIKISKILL_SYNTHESIS.md)
- [SkillOpt 方法映射](SKILLOPT_SYNTHESIS.md)
- [技能演进实现](../docs/SKILL_EVOLUTION.md)
- [项目评测与证据](../docs/BENCHMARKS.md)

# Weyra 技能自进化与过程审计架构说明（对齐 2026 顶会研究）

本文档说明 `weyra` 在 **技能检索、防退化演化、验证门禁短路、结构化 Trace 反馈与只读过程审计** 方面的设计与实现；所有改进点均严格对照已公开的 2026 学术研究，不使用无文献依据的启发式魔法数值。

## 1. 论文逐点对照与代码实现映射

| 能力模块 | 对应源码 | 参考论文 | 落地机制与不变量 |
| --- | --- | --- | --- |
| 激活条件优先召回 | [weyra/skills.py](../weyra/skills.py)<br>[weyra/coding/resources.py](../weyra/coding/resources.py)<br>[weyra/coding/lifecycle.py](../weyra/coding/lifecycle.py) | **Skill-Pro** (`ICML 2026, PMLR v306`)<br>**SkillAlign** (`EMNLP 2026 Main, 2026.emnlp-main.2565`) | 将技能视为 `(C, π, T)`：`SkillStore.search()` 优先对激活条件 `C`（`name` / `aliases` / `when_to_use` / `description`）做 BM25 排序，命中不足 `limit` 时才回落正文 `π`；`ResourceLoader` 与 `CodingLifecycle._knowledge()` 在不改变静态系统提示词的前提下暴露 `when_to_use`（兼容缺少该字段的最小桩对象）。 |
| 专家锚点不可篡改护栏 | [weyra/skills.py](../weyra/skills.py)<br>[weyra/skill_evolution.py](../weyra/skill_evolution.py)<br>[weyra/online_skill_evolution.py](../weyra/online_skill_evolution.py) | **SkillDiff** (`arXiv:2611.25534`, Nov 2026) | `_parse_anchors()` 解析 frontmatter（`anchors` / `protected_anchors`）与正文（`[ANCHOR]` / `<!-- anchor -->`）中的专家约束；`EvolutionStore.apply()` 默认 `allow_anchor_override=False`，拦截删除或篡改锚点的补丁并返回 `failure_kind="anchor_violation"`；`OnlineSkillEvolution.propose()` 向优化器传递 `protected_anchors`。 |
| 分类知识配额隔离 | [weyra/skill_evolution.py](../weyra/skill_evolution.py) | **WikiSkill** (`arXiv:2608.27454`, 2026) | 定义 `KNOWLEDGE_KINDS`（`boundary`、`verified_pattern`、`failure_mode`、`observation`、`rejected_update`）与独立每类配额 `KNOWLEDGE_QUOTAS`（`MAX_NOTES_PER_KIND = 10`）。瞬态 `observation` 仅在其自身配额内滚动淘汰，无法挤占 `boundary` / `verified_pattern` 等高价值约束，同时兼容历史 10 条去重契约；`knowledge_prompt()` 按优先级输出并硬限 `<= 2,000` 字符。 |
| 有界补丁与被拒编辑缓冲 | [weyra/skill_evolution.py](../weyra/skill_evolution.py)<br>[weyra/online_skill_evolution.py](../weyra/online_skill_evolution.py) | **SkillOpt** (`NeurIPS 2026 Spotlight, arXiv:2605.23904`) | 强制 `1–2` 处编辑、单处 `<= 1,000` 字符、旧文本唯一匹配、`base_hash` OCC 校验；将 `anchor_violation`、`regression` 与 `no_validation_gain` 写入 `rejected_edits.jsonl`（上限 `MAX_REJECTED_EDITS = 20`），并通过 `rejected_edits_prompt()`（`<= 1,500` 字符）回流给下一轮 `MAINTENANCE_INSTRUCTION`。 |
| 零空基线门槛 | [weyra/skill_evolution.py](../weyra/skill_evolution.py)<br>[weyra/skill_judge.py](../weyra/skill_judge.py) | **SkillGen** (`arXiv:2605.10999`)<br>**SkillsBench** (`arXiv:2602.12670`) | `ConfiguredSkillGate` 声明 `supports_null_baseline = True`；`EvolutionStore.maintain()` 在 `action="create"` 且存在支持基线对比的门禁时，以 `__null_baseline__`（不加载技能）为对照执行隔离评测，要求候选技能相对空基线取得严格收益提升且不增加硬失败后才允许发布；不支持空基线对比的自定义门禁安全弃权。 |
| 确定性校验短路门禁 | [weyra/skill_judge.py](../weyra/skill_judge.py)<br>[weyra/skill_replay_gate.py](../weyra/skill_replay_gate.py) | **SkillsBench** (`NeurIPS 2026, arXiv:2602.12670`)<br>**SOPBench** (`EMNLP 2026 Main, 2026.emnlp-main.936`) | 当所有 `validation` 用例标记 `deterministic_only=True`（或具备确定性产物/结构/写作断言且无 `rubric`）时，`_can_short_circuit_judge()` 触发 `_deterministic_judgment()` 直接合成成对裁决，完全跳过 LLM Judge 调用（`judge_mode="deterministic_short_circuit"`），仅在需要主观质量评判时走 `double_blind_llm`。 |
| 结构化 Trace 证据摘要 | [weyra/coding/lifecycle.py](../weyra/coding/lifecycle.py)<br>[weyra/online_skill_evolution.py](../weyra/online_skill_evolution.py) | **Rethinking Self-Evolving** (`arXiv:2608.02636`)<br>**AutoSaddler** (`arXiv:2608.23041`)<br>**SkillsVote** (`2026.emnlp-main.281`) | `run_feedback_turn()` 在开启演进或发生工具调用/错误/非常态结束时提取 `trace_summary`（`tool_calls`、`error_tools`、`retry_count`、`turn_state`）供 `MAINTENANCE_INSTRUCTION` 归因；未开启演进的纯文本对话保持原始最小窗口字典向后兼容。 |
| 只读过程与技能漏斗审计 | [weyra/trace_audit.py](../weyra/trace_audit.py) | **AgentLens** (`NeurIPS 2026, arXiv:2605.12925`)<br>**SkillPlumber** (`arXiv:2611.25429`)<br>**Harness Updating != Benefit** (`2026.emnlp-main.2326`)<br>**Trial by Trace** (`arXiv:2610.32285`) | `audit_session_db()` 以只读 SQLite URI (`?mode=ro`) 审计会话事件流，输出：`ProcessQualityReport`（`ideal / solid / lucky_pass / failed`，识别错误重试循环、未校验声称完成）、`SkillFunnelReport`（`exposed -> retrieved -> activated -> rule_coverage` 四段转化漏斗）与 `HarnessReliabilityReport`（未闭合意图、恢复中断、未知费用调用、清理报错与演化冲突）。 |

---

## 2. 端到端闭环流程

```text
用户任务 ──▶ SkillStore.search (C 激活条件优先 BM25) ──▶ ResourceLoader / _knowledge (暴露 when_to_use)
   │
   ▼
CodingAgent 执行工具与写入 ──▶ run_feedback_turn 提取 trace_summary (工具/报错/重试)
   │
   ├─▶ audit_session_db (?mode=ro) ──▶ ProcessQualityReport (ideal vs lucky_pass) & SkillFunnelReport
   │
   └─▶ OnlineSkillEvolution ──▶ SkillOpt (1–2 edits, <=1000 chars) + protected_anchors + rejected_edits
          │
          ├─ apply(): SkillDiff [ANCHOR] 保护 + base_hash OCC ──▶ 越界写入 rejected_edits.jsonl
          ├─ create(): ConfiguredSkillGate (__null_baseline__ 对照门禁)
          └─ evaluate(): deterministic_only -> 0 Judge 短路 (或 A/B+B/A 双盲) + SHA-256 回放缓存
```

---

## 3. 验证与评测入口

离线零 API 可靠性/长程防退化套件及多域技能评测脚本见 [benchmarks/README.md](../benchmarks/README.md)。

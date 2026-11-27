# Weyra 技能调用与自进化架构说明

本文档说明 `weyra` 在**标准技能调用协议**与**验证驱动技能自进化**两方面的架构设计与上游参考来源。Weyra 坚持精简与可核查原则：常规技能发现与调用完整对齐 `DeepSeek Harness`、`Pi (agentskills.io)` 与 `Hermes Agent`；技能演进仅保留 1 套主干优化架构（`microsoft/SkillOpt`）与 1 套正交防退化机制（`SkillDiff`），核心算法直接复用上游 MIT 开源实现。

---

## 1. 标准技能调用：开源 Agent Harness 对齐映射

| 能力维度 | 对应源码 | 对齐标准 / 框架 | 落地机制 |
| --- | --- | --- | --- |
| 多目录技能发现 | [weyra/skills.py](../weyra/skills.py) | **Pi (`agentskills.io`)**<br>**DeepSeek Harness**<br>**Hermes Agent** | 按优先级扫描工作区（`.weyra/skills`、`.agents/skills`）与用户全局目录（`~/.weyra/skills`、`~/.agents/skills`），跳过附属资源目录（`references/`、`templates/`、`scripts/`、`assets/`）与构建/依赖目录（`.git`、`.venv`、`node_modules` 等），并对同层同名冲突快速报错。 |
| 元数据与双面调用门控 | [weyra/skills.py](../weyra/skills.py)<br>[weyra/coding/agent.py](../weyra/coding/agent.py) | **DeepSeek Harness**<br>**Pi Agent**<br>**agentskills.io** | 解析 `name`、`description`、`when_to_use`、`aliases`、`disable-model-invocation`（`model_invocable`）与 `user-invocable`（`user_invocable`）。启用 `disable-model-invocation: true` 时，技能不向模型目录暴露且 `skill` 工具拒绝模型自主加载。 |
| `<available_skills>` 摘要暴露与渐进式披露 | [weyra/skills.py](../weyra/skills.py)<br>[weyra/coding/resources.py](../weyra/coding/resources.py)<br>[weyra/coding/lifecycle.py](../weyra/coding/lifecycle.py)<br>[weyra/coding/agent.py](../weyra/coding/agent.py) | **agentskills.io**<br>**Pi Agent**<br>**Hermes Agent** | 上下文仅注入精简 `<available_skills>` 摘要索引以维持静态系统提示词前缀稳定；模型按需调用 `skill(name)` 获取 `<skill_content>` 正文与附属资源清单，再通过 `skill(name, file_path=...)` 按需读取附属文件（带符号链接防护与 SHA-256 防漂移校验）。 |
| `/<skill-name>` 用户显式触发 | [weyra/skills.py](../weyra/skills.py)<br>[weyra/coding/lifecycle.py](../weyra/coding/lifecycle.py) | **DeepSeek Harness**<br>**Pi Agent** | `extract_user_invoked_skills()` 从用户输入中提取去重的 `/<skill-name>` 调用手势（自动忽略文件路径），在准备阶段直接注入 `<skill_content>`（`source="skill_invocation"`）。 |

---

## 2. 技能自进化：1 套主干 + 1 套正交防退化机制

核心算法在 [weyra/skillopt_vendor/](../weyra/skillopt_vendor/) 与 [weyra/skilldiff_vendor/](../weyra/skilldiff_vendor/) 中原样移植自 MIT 开源实现，由 `EvolutionStore`、`SkillValidationGate` 与 `OnlineSkillEvolution` 统一编排：

| 模块 | 对应源码 | 上游参考 | 核心机制与不变量 |
| --- | --- | --- | --- |
| **主干架构：有界编辑 + 步长衰减 + 性能门控 + 记忆缓冲** | [weyra/skillopt_vendor/](../weyra/skillopt_vendor/)<br>[weyra/skill_evolution.py](../weyra/skill_evolution.py)<br>[weyra/online_skill_evolution.py](../weyra/online_skill_evolution.py)<br>[weyra/skill_judge.py](../weyra/skill_judge.py) | **Microsoft SkillOpt** (`NeurIPS 2026 Spotlight`, `arXiv:2605.23904`, MIT) | 1. **标准 `Edit` / `Patch` 原语**（`types.py`, `skill.py`）：提供 `append` / `insert_after` / `replace` / `delete` 四种结构化文本操作与受保护块隔离，由 `apply_patch_with_report()` 返回逐条执行报告。<br>2. **学习率调度**（`scheduler.py`）：支持 `constant`、`linear`、`cosine` 与 `autonomous`，约束单步编辑预算 `edit_budget`。<br>3. **验证门控**（`gate.py`）：调用 `evaluate_gate(metric="hard" \| "soft" \| "mixed")` 执行严格收益对比，并叠加零硬退化检查；新建技能通过 `__null_baseline__` 对照门禁验证相对无技能基线的净胜。<br>4. **负反馈与学习区隔离**（`memory.py` & `rejected_edits.jsonl`）：`apply_edits_detailed()` 仅维护受保护 `LEARNED` 区块而不改动用户手写正文，`rejected_edits_prompt()` 将近期被拒方向回流至下一轮优化器。 |
| **正交防退化：非先验专家条款标注与护栏** | [weyra/skilldiff_vendor/](../weyra/skilldiff_vendor/)<br>[weyra/skills.py](../weyra/skills.py)<br>[weyra/skill_evolution.py](../weyra/skill_evolution.py) | **SkillDiff** (`arXiv:2611.25534`, MIT) | 使用 `split_items()` 对 Markdown 正文做结构化原子条目切分（`front matter`、`heading`、`list entry`、`paragraph`、`code block`、`table`），通过 `mark()` 对先验未覆盖的专家条目就地标注 `**[FOCUS]**`。未经外部验证门禁通过的编辑默认禁止删改 `**[FOCUS]**` 条目（返回 `failure_kind="focus_item_violation"` 并记入 `rejected_edits.jsonl`），防止多轮演化向模型先验坍缩。 |
| **独立验证门禁与只读审计** | [weyra/skill_judge.py](../weyra/skill_judge.py)<br>[weyra/trace_audit.py](../weyra/trace_audit.py) | **SkillsBench / SOPBench**（确定性校验）<br>**Zheng et al.** (`NeurIPS 2023`, 换位成对盲评) | 1. `SkillValidationGate` 在隔离临时工作区中运行留出验证集，对确定性产物/结构断言用例零裁判调用并启用 SHA-256 缓存；仅当用例显式配置主观 `rubric` 时启用 `(A, B)` / `(B, A)` 换位成对盲评，不一致时弃权以消除位置偏见。<br>2. `audit_session_db()` 以 SQLite 只读模式（`?mode=ro`）统计客观执行指标（完成数、错误重试循环、编辑后校验、未验证声明）、技能采纳漏斗与 Harness 可靠性计数。 |

---

## 3. 设计边界与精简原则

- **单一演进主干**：所有补丁原语、编辑预算调度、验证门控与学习区隔离统一收口至 `microsoft/SkillOpt`，避免多套异构启发式规则叠加带来的维护复杂度。
- **单一正交护栏**：仅叠加与优化器职责正交的 `SkillDiff` `**[FOCUS]**` 条目保护，确保受保护专家约束可由原始文档结构直接解析，不引入硬编码知识分类配额。
- **确定性优先与原子落盘**：验证门禁优先采用可执行确定性断言，并结合 `base_hash` OCC 并发检查、POSIX 原子写入与 `recover()` 崩溃恢复状态机保障技能库一致性。

---

## 4. 端到端闭环流程

```text
用户消息 ──▶ CodingLifecycle._knowledge():
               ├─ 提取 /<skill-name> 显式手势 ──▶ 注入 <skill_content source="skill_invocation">
               └─ 暴露未禁用技能 <available_skills> 摘要索引
   │
   ▼
CodingAgent 执行 ──▶ 模型按需调用 skill(name[, file_path]) 加载正文或附属资源
   │
   ├─▶ audit_session_db (?mode=ro) ──▶ 客观执行统计 & 技能采纳漏斗
   │
   └─▶ OnlineSkillEvolution ──▶ SkillOpt Edit (edit_budget + focus_items + rejected_edits)
          │
          ├─ annotate_skilldiff(): SkillDiff split_items + mark(**[FOCUS]**)
          ├─ apply() / maintain(): SkillDiff **[FOCUS]** 保护 + base_hash OCC
          └─ SkillValidationGate: 确定性断言 (0 Judge) 或换位双盲 ──▶ SkillOpt evaluate_gate()
```

---

## 5. 验证与评测入口

离线零 API 可靠性/防退化套件及多域技能评测脚本见 [benchmarks/README.md](../benchmarks/README.md)。

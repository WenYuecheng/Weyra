# Weyra 评测与验证套件

本目录包含 Weyra 的三类可复现验证与评测入口，分别覆盖真实后端编码能力、离线零 API 系统可靠性与技能演进防退化，以及多域技能路由与在线演进闭环。

## 1. Aider Polyglot 编码基准（`polyglot/`）

覆盖 **Python、Go、Rust、C++、Java** 五种后端与系统语言，对比 Weyra 与共享模型、工具及有界测试反馈的最小工具循环基线（两轮提交，Docker 隔离验收）：

- **两轮累计通过率**：**72.62%（122/168）**，较基线 **65.48%（110/168）** 相对提升 **+10.91%**（净胜 +12 题），五门语言零退步。
- **首轮失败自修复率**：**60.98%（75/123）**，较基线 **50.41%（61/121）** 相对提升 **+20.95%**；其中 **C++ 81.0%**、**Python 70.8%**、**Go 61.5%**。
- **Token 实耗**：较基线节省 **13.66%**。

| 语言 | 有效配对 | Weyra 修复率 | 基线修复率 | 修复相对提升 | 两轮累计 Weyra / 基线 | 通过率相对提升 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Python | 33 | 70.8% | 58.3% | **+21.4%** | **26 / 23** | **+13.0%** |
| Go | 37 | 61.5% | 43.5% | **+41.5%** | **27 / 24** | **+12.5%** |
| Rust | 28 | 50.0% | 44.4% | **+12.5%** | **19 / 19** | **0.0%** |
| C++ | 26 | 81.0% | 61.1% | **+32.5%** | **22 / 19** | **+15.8%** |
| Java | 44 | 46.9% | 47.4% | -1.0% | **28 / 25** | **+12.0%** |
| **合计** | **168** | **60.98%** | **50.41%** | **+20.95%** | **122 / 110** | **+10.91%** |

离线复算（仅依赖 Python 3.11+）：

```bash
python benchmarks/polyglot/report.py
```

题目来源见 [Aider Polyglot Benchmark](https://github.com/Aider-AI/polyglot-benchmark/tree/7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f)。

## 2. 离线零 API 可靠性与长程演进防退化套件（`reliability/`）

使用确定性 `ScriptedProvider` 与真实运行时组件（`SessionStore`、`ToolRuntime`、`Budget`、`EvolutionStore`、`SkillValidationGate`、`trace_audit`），零外部 API 调用：

- **故障注入回归**：6 类故障注入全部通过，覆盖演进中断恢复、工具未闭合恢复、外部写入与 `base_hash` 冲突拦截、技能附属文件 SHA-256 漂移检测、审批后权限吊销二次重检与预算防绕过。
- **30 轮连续演进防退化**：结合 `SkillDiff` `**[FOCUS]**` 非先验专家条款标记与 `SkillOpt` `evaluate_gate` 有界补丁门禁，关键专家条款留存率在长程演进压测中保持 **100%**；确定性验证短路实现零裁判模型调用并命中 SHA-256 验证缓存。
- **只读轨迹审计**：基于 SQLite 只读模式区分高洁净无错误轨迹与伴随错误重试循环/未校验声称完成的轨迹，并准确统计 `<available_skills>` 与 `skill_invocation` 采纳漏斗。

运行方式：

```bash
python benchmarks/reliability/run_suite.py
python -m pytest -q
```

## 3. 多域技能盲选路由与在线演进套件（`skills_eval/`）

面向三类企业运维与交付合规域（Audit Ledger / Incident Triage / Release Manifest），通过 `<available_skills>` 目录并发暴露业务技能与干扰技能，在不提示技能名称的条件下验证真实模型选择与在线演进闭环：

- **盲选披露与合规执行**：验证技能摘要暴露下的盲选激活准确率、零干扰误触发与无错误洁净执行能力。
- **在线演进与门禁闭环**：验证 `SkillDiff` `**[FOCUS]**` 篡改拦截、`SkillOpt` 有界补丁门禁晋升以及无技能空基线对照新建门禁（`__null_baseline__`），全程确定性校验、零裁判模型调用。

本地复现（需配置 `weyra.toml` 与 `.env` 中的有效模型密钥）：

```bash
python benchmarks/skills_eval/run_benchmark.py
```

技能调用协议与演进架构说明见 [docs/skill-evolution.md](../docs/skill-evolution.md)。

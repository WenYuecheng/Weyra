# Weyra 简历主张—证据账本

核验日期：2026-10-26。范围仅为 `/Users/wenyuecheng/code/python/Weyra`。默认面向 AI Agent / LLM 应用工程岗位，交付范围为项目经历文字。

状态说明：下表“已确认”指源码或留存结果支持该项工程事实；不据此推断作者身份、独立开发、项目负责人或全部原创。个人责任边界统一为：按本人实际负责的实现与交付使用正文中的动作词，不将参考框架或第三方组件整体认领为原创。

| ID | 原始事实与候选表述 | 证据位置 | 确认状态与使用范围 | 可展开细节与边界 |
| --- | --- | --- | --- | --- |
| W01 | 分层 Harness 与测试反馈修复；64 题累计通过 50 题，Python 28/32，本轮新增 21/35 | `weyra/core/loop.py`、`weyra/core/harness.py`、`weyra/coding/agent.py`、`docs/evidence/polyglot-64-summary.json`、`docs/evidence/polyglot-64-protocol.json` | 已确认；AI Agent 项目经历 | 多轮累计协议，保留既有 29 题；不是完整 Polyglot 或单次通过率。源码证明执行机制，成绩来自历史实验摘要，本次未重跑在线实验。 |
| W02 | 四级技能定位及歧义弃权；26/48 → 40/48，+29.1667 个百分点 | `weyra/skills.py:155`、`docs/evidence/skill-localization-summary.json`、`wiki/history_archive/FINAL-EVIDENCE.json` | 已确认；AI Agent 项目经历 | 24 任务×2 次，48 对；14 胜0负34平。仅定位质量，不扩展为端到端任务成功率或攻击防御率。 |
| W03 | 有界技能补丁、独立重执行与次序交换裁判、发布与回滚 | `weyra/online_skill_evolution.py`、`weyra/skill_evolution.py`、`weyra/skill_judge.py`、`weyra/knowledge.py` | 已确认；AI Agent 项目经历 | 最多2处、每处≤1000字符；固定验证任务上成功数严格增加、硬失败不增加；裁判冲突不批准。临时目录及工具约束不等于 OS 沙箱。 |
| W04 | 会话树、事件日志、检查点压缩、Usage 校准与工具配对 | `weyra/session.py`、`weyra/core/context.py`、`weyra/output.py`、`weyra/coding/session_bridge.py` | 已确认；AI Agent / Python 后端项目经历 | SQLite WAL、追加事件、分支派生、完整交互组；没有上下文性能配对实验，不写延迟或缓存命中率收益。 |
| W05 | 请求前预留、响应后结算、共享预算和执行权限；累计费用估算49.4882元 | `weyra/core/state.py`、`weyra/core/model_runtime.py`、`weyra/core/tools.py`、`docs/evidence/polyglot-64-summary.json` | 已确认；AI Agent / Python 后端项目经历 | Decimal 账本，未知消耗保守记账，恢复未决预留；平均49.4882226702553/64≈0.7733元。是配置费率估算，不是账单或成本节省实验。 |

## 不采用的主张

- BearCode 代码、测试结果与其引用的 DeepAgent 论文成绩：不属于本次 Weyra 简历证据。
- “全部从零原创”“工业级生产验证”“金融级安全”“彻底消除位置偏见”：现有材料不能支持相应强度。
- “自进化使任务成功率提升29.2个百分点”：定位评测不能证明技能更新的端到端能力收益。
- “全量黄金集零退化”“已接入CI阻断合并”：不把本地技能发布门禁扩展为逐题零退化或仓库合并门禁。

## 本次复核

- Weyra 完整测试：`.venv/bin/python -m pytest -q`，702 passed、3 skipped、5 subtests passed，21.20 秒。
- 数字核对：50/64=78.125%；28/32=87.5%；21/35=60%；40/48−26/48=29.1667个百分点。
- 公开摘要保留原始产物名称及 SHA-256。摘要可检查统计与协议；逐请求完整独立复核需对应原始轨迹。
- 本次未修改产品源码、既有文档或原始评测结果，仅新增本简历稿与账本。

## 2026-11-02 架构更新复核

| ID | 可采用主张 | 证据 | 边界 |
| --- | --- | --- | --- |
| W06 | 候选模型容量准入、路由持久化失败回滚、步骤工具注册身份检查 | `weyra/core/model_step.py::switch_model`、`weyra/core/tools.py::_run`、`weyra/core/tool_step.py`、`tests/test_step_admission.py` | 工程行为；不是线上收益。普通重试沿用工具快照，fallback 重新准备；程序化工具调用不自动套用模型步骤集合。 |

原有成绩与费用继续绑定原始评测协议，本轮没有重跑在线评测。此条为 P0 证据；P1 新增领域投影与可选 patch，PTC 仍未实现。

本轮最终源码验证：`pytest -q` 为 **744 passed、3 skipped、5 subtests passed（22.60 秒）**；`ruff check .` 通过；wheel 与 sdist 构建通过。安装包隔离安装、CLI、离线演示及源码报告检查通过。以上均为本地验证，未重跑在线任务。

## P1 新增主张（2026-11-03）

| ID | 工程事实 | 证据 | 边界 |
| --- | --- | --- | --- |
| W07 | 版本化领域恢复、目标执行许可分离、冷会话查询 | session.py、projections.py、service.py、test_p1_recovery.py、test_goal_lifecycle_p1.py | 本地恢复与一致性，不是通用插件隔离或常数内存保证 |
| W08 | 协议身份及来源治理、可选多文件 patch | llm/providers.py、coding/resources.py、patch.py、patch_tool.py、对应回归 | 严格原生回放；精确 patch 匹配；顺序提交无整体回滚；无新增在线成绩 |

原有 Polyglot 成绩、费用与技能定位数字继续绑定原协议，不计入新功能收益。

2026-11-03 P1 最终验证：**856 passed、3 skipped、5 subtests passed，37.04 秒**；Ruff、wheel/sdist 构建通过。源码哈希与边界见 [P1 验证记录](evidence/p1-20261103-validation.json)，原始测试输出见 [测试日志](evidence/p1-20261103-tests.txt)。本轮未运行真实模型评测。

## DSH 步骤架构新增证据

| ID | 主张 | 证据 | 边界 |
| --- | --- | --- | --- |
| W09 | 明确步骤所有权、绑定 provider、条件工具准入及注册排干 | core/step.py、core/tool_admission.py、core/tool_pipeline.py、专项回归 | 单一 Native 执行方式，不声称插件隔离 |
| W10 | 独立投影、v2 迁移、目标 CAS 与 resume/restart | projections.py、session.py、goals.py、test_projection_units.py、test_goal_cas.py | 原始历史仍在内存；旧程序拒绝 v2；无新增在线成绩 |

DSH 步骤架构最终验证：866 passed、3 skipped、5 subtests passed；Ruff、最新 wheel/sdist 与隔离安装通过。详见 [验收记录](DSH_STEP_ACCEPTANCE.md)。读取对照不作为线上性能或任务成功率收益。

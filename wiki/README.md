# Weyra 知识库与历史冷归档 (Knowledge Base & Cold Archives)

欢迎查阅 Weyra 知识库。

自 **0.1.4** 版本起，Weyra 的核心技术白皮书、架构规范与工程参考已全面升级，并统一收敛至项目根目录的 [`docs/`](../docs/) 目录中。本目录保留为历史研发冷归档与溯源索引。

---

## 官方技术文档全景导航 (Official Documentation Index)

如需深入了解 Weyra 的设计理念、架构实现、学术理论落地与量化基准表现，请直接查阅以下官方技术白皮书与工程参考：

1. 🏛️ **[系统全景架构白皮书 (docs/ARCHITECTURE.md)](../docs/ARCHITECTURE.md)**：深入剖析分层解耦体系、Pi 风格 Session Tree DAG 结构化回溯、SQLite WAL 事务事件溯源、Pi 风格双输入优先级调度与 Anthropic/Pi 4 阶段 Prompt Cache 前缀对齐。
2. 🎯 **[架构深度演进与高频技术答辩指南 (docs/INTERVIEW_DEFENSE.md)](../docs/INTERVIEW_DEFENSE.md)**：对标一线大厂深度技术答辩，推导 15 大最具挑战性的底层硬核问题，涵盖前缀缓存命脉、DSH 压缩数学模型、Fail-Closed 预算锁存与双盲仲裁。
3. 🔄 **[开源框架与理论对比白皮书 (docs/FRAMEWORK_COMPARISON.md)](../docs/FRAMEWORK_COMPARISON.md)**：详述 Weyra 如何深度汲取 Pi Agent（ReAct 微内核、Session 树状 DAG、双队列、TTY 终端协同）、DeepSeek-Harness（八节压缩、中间修剪、Fail-Closed 预算守卫、Goal 轮驱动）与 Hermes（配置解耦）的工程精髓。
4. 🧬 **[自进化技能系统技术白皮书 (docs/SKILL_EVOLUTION.md)](../docs/SKILL_EVOLUTION.md)**：详解基于 WikiSkill 与 SkillOpt 的局部有界语法补丁、隔离沙箱真实重执行、顺序交换双盲 LLM 仲裁以及验证集零退化安全发布门禁。
5. 📈 **[权威量化基准与系统效能评估白皮书 (docs/BENCHMARKS.md)](../docs/BENCHMARKS.md)**：基于真实 Polyglot 64 任务套件（78.1% 通过率、Python 87.5%、¥0.77/任务）、真实意图路由实测（83.3% 准确率、100% 歧义安全拦截）、3,740+ API 调用零穿透 Fail-Closed 预算防御以及 87 个模块顶级注释全覆盖实测战报。
6. 📘 **[开发者全功能实战指南 (docs/USER_GUIDE.md)](../docs/USER_GUIDE.md)**：环境搭建、5 级配置规范、Rich CLI 与 Textual TUI 双模终端、交互控制指令集与 MCP 客户端网桥。
7. 🔌 **[Python SDK 编程式开发与 API 参考 (docs/API_REFERENCE.md)](../docs/API_REFERENCE.md)**：`WeyraAppService`、`CodingAgent`、`AgentHarness`、`SessionStore` 核心 API 手册及完整异步示例。
8. 🖥️ **[CLI 与 Textual TUI 交互控制手册 (docs/CLI_TUI_REFERENCE.md)](../docs/CLI_TUI_REFERENCE.md)**：命令行参数、Slash 命令、TUI 快捷键与三段式 Ctrl+C 中断机制。
9. ⚙️ **[企业级配置与环境变量参考 (docs/CONFIGURATION_REFERENCE.md)](../docs/CONFIGURATION_REFERENCE.md)**：5 级覆盖优先级、`weyra.toml` 完整字段规范与高防生产级配置模板。
10. 🎓 **[学术前沿演进与理论落地报告 (paper/README.md)](../paper/README.md)**：详述 WikiSkill (arXiv:2608.27454) 与 SkillOpt (arXiv:2605.23904) 在工业级落地中的前沿推导与工程超越。
11. 🤝 **[开发者贡献指南 (CONTRIBUTING.md)](../CONTRIBUTING.md)**：顶级大厂代码与注释规范、核心设计不变性守则与提交流程。

---

## 历史研发与原始评测数据冷归档 (Historical Cold Archives)

在 Weyra 研发演进与密集验证过程中，我们建立并执行了严苛的内部自动化检验流水线。为了保证工程数据的完整性、透明性与可追溯性，所有早期版本阶段的原始评测日志、JSON 数据指纹以及工程验证记录已完整冷归档至：

📁 **[`wiki/history_archive/`](./history_archive/)**

该冷归档包含：
- 早期各研发里程碑的原始 JSON 运行事实与硬件指标记录；
- 多阶段回归测试指纹与数据包验证校验和；
- 原始基准题目的评测流水线上下文与过程记录。

> **声明**：`wiki/history_archive/` 仅供工程追溯参考，不作为最新功能验收依据。面向新用户与系统开发者，请统一以根目录 [`README.md`](../README.md) 与 [`docs/`](../docs/) 的最新官方文档为准。

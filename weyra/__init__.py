"""Weyra: 企业级轻量自治 AI Agent 框架（Enterprise-Grade Agent Harness）。

核心架构与开源血统传承:
    Weyra 是一个专为严肃研发工程打造的高性能、企业级 Python AI Agent 运行时。
    深度融合了工业界顶级大厂规范、开源智能体先驱与前沿学术论文的卓越设计：
    - Pi Agent (piagent, my-pi-agent, dg-ai-notes):
      * Session Tree DAG 会话事件图（基于 parent 与 id 建立不可变追加树，leaf 游标无损分支/回溯）；
      * ReAct 执行微内核与双队列交互调度（人类意图 Steering 抢占干预与 Follow-up 队列跟进）；
      * 提示词缓存前缀严格对齐与 Anthropic 4-breakpoint KV Cache 标准；
      * 工具消息严格配对与防 400 校验，50KB / 2000 行双维文本截断保护。
    - DeepSeek-Harness (deepseek-harness / dsh):
      * 8 段式检查点压缩协议（8-Section Compaction Protocol, commit d743267）；
      * 工具产物 8KiB 中间剪枝（prune_tool_result）与大输出磁盘卸载隔离；
      * 故障即关闭预算守卫（Fail-Closed Budget Guard: reserve/settle 两阶段结算）；
      * 目标推进轮次驱动器（Goal Round Driver，停滞熔断停机，最大自主推进轮次约束）；
      * 子智能体 Fork/Spawn 双模派生、POSIX 进程组隔离与读写协调锁（10 共享读 / 1 排他写）。
    - Hermes Agent:
      * 行为配置与机密凭据严格物理分离（weyra.toml 与 .env 解耦）；
      * 透明 Markdown 文件记忆引擎（memory.py）与技能目录发现标准（SKILL.md Frontmatter）。
    - WikiSkill (arXiv:2608.27454, 2026) & SkillOpt (arXiv:2605.23904, 2026):
      * 指令与外部领域知识解耦结构，有界自然语言补丁自演化与次序对换双盲裁判门禁。
"""

__version__ = "0.1.4"


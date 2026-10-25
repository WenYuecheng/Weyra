"""研发工程领域专用智能体装配层 (Coding Agent Assembly Layer)。

【架构定位与分层哲学】
本包对应 Pi Agent 的 `packages/coding-agent` 分层体系。
将通用的核心执行引擎（`weyra.core`）与具体的研发工作区环境（工程文件系统、规则指令 AGENTS.md / WEYRA.md、
技能目录与长期记忆）相结合，装配出专职服务于严肃代码工程（Code Exploration, Editing, Execution）的顶级智能体。

【开源框架借鉴与理论依据】
1. Pi Agent (`packages/coding-agent`):
   - 专职研发代理装配 (`CodingAgent`): 整合文件读写工具、Diff 生成与确认审批；
   - 项目指令热加载 (`ResourceLoader`): 监听工程根目录下 `AGENTS.md` / `WEYRA.md` 规则变动并热更新；
   - 上下文会话桥接 (`ContextSessionBridge`): 分离投影计算与底层持久化；
   - 双队列异步交互模型 (`steering` / `follow_up`) 与 anti-400 工具修复机制 (`_repair_tool_messages`)。
2. DeepSeek-Harness (`packages/core/`, `packages/goal/`):
   - 任务自治驱动 (`Goal`) 与多轮目标停滞检查；
   - 子智能体沙箱执行与结构化交付物回流 (`_accept_child_results`)。
"""

from __future__ import annotations

from .agent import CodingAgent, Agent

__all__ = ["CodingAgent", "Agent"]

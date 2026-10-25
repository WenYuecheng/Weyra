"""项目规则指令加载器与资源提示词快照装配机 (Project ResourceLoader & Instructions Tracker)。

【架构定位】
本模块负责在研发工程环境中装配系统提示词并追踪项目规则文件的动态变化。
对应 Pi Agent (`packages/coding-agent`) 的 ResourceLoader 架构边界，
负责发现并安全挂载工作区内的指导规范（`AGENTS.md` 与 `WEYRA.md`），
并结合记忆库索引与可用技能目录，构建确定性的系统提示词基线。

【开源框架借鉴与理论机制】
1. Pi Agent (`packages/coding-agent`):
   - 项目规则热更新与指纹追踪 (`refresh_project_rules`):
     在每个交互轮次准入前，自动计算当前工作区规则文件的 SHA-256 哈希摘要（fingerprint）；
     一旦检测到开发者修改了规则文件，在单个数据库事务内原子触发 `context_manager.refresh()`，
     使新规则即时生效，同时持久化 `project_rules` 事件记录。
   - 提示词确定性前缀缓存结构：将静态系统指示、工程规则、记忆目录与技能目录有序拼接，
     严格维护提示词前缀单调性，防止触发大模型服务端 KV 缓存失效。
"""

from __future__ import annotations

from weyra.contracts import digest


class ResourceLoader:
    """工作区资源与指令装配加载器 (Workspace Resource & Instruction Assembler)。

    代理宿主智能体并负责工作区项目规范与快照构建。
    """

    def __init__(self, agent):
        """初始化项目资源装配加载器 (Initialize ResourceLoader)。

        Args:
            agent: 绑定的宿主 CodingAgent 实例。
        """
        self.agent = agent

    def __getattr__(self, name):
        """透传代理宿主智能体属性 (Delegate Attributes to Host Agent)。

        Args:
            name: 属性名。

        Returns:
            Any: 宿主智能体对应的属性值。
        """
        return getattr(self.agent, name)

    def system(self, catalog) -> str:
        """装配完整的系统提示词文本 (Assemble Complete System Prompt Text)。

        【设计思想与开源血统】
        严格遵循 Pi Agent (`packages/coding-agent`) 的提示词拼接与缓存前缀对齐规范：
        聚合基础角色指示、工作区工程规范（`AGENTS.md` / `WEYRA.md`）、长期记忆索引快照以及技能目录快照。

        Args:
            catalog: 当前捕获的可用技能目录字典。

        Returns:
            str: 格式化后的完整系统提示词字符串。
        """
        from .agent import DEFAULT_SYSTEM

        parts = [DEFAULT_SYSTEM]
        for name in ("AGENTS.md", "WEYRA.md"):
            path = self.project / name
            if path.is_file() and path.resolve().is_relative_to(self.project):
                parts.append(f"Project instructions ({name}):\n{path.read_text(encoding='utf-8')}")
        parts.append(
            "Memory index snapshot:\n" + "\n".join(item["id"] for item in self.memory.list())
        )
        parts.append(
            "Skill catalog snapshot:\n"
            + "\n".join(
                f"{skill.name}: {skill.description} [{skill.version}]" for skill in catalog.values()
            )
        )
        return "\n\n".join(parts)

    def refresh_project_rules(self):
        """检测项目规则文件哈希变动并原子刷新系统提示词 (Detect Rules Drift & Atomic Refresh)。

        【Pi Agent 规则热加载机制】
        若检测到工作区内 `AGENTS.md` 或 `WEYRA.md` 文件的 SHA-256 指纹变动，
        在底层 SessionStore 开启单一事务，原子刷新 context_manager 的 system prompt
        并追加 `project_rules` 事件，确保无需重启智能体即可加载最新工程规范。
        """
        if self._system_override is not None:
            return
        rules = {}
        for name in ("AGENTS.md", "WEYRA.md"):
            path = self.project / name
            if path.is_file() and path.resolve().is_relative_to(self.project):
                rules[name] = path.read_text(encoding="utf-8")
        fingerprint = digest(rules)
        previous = self.session.latest("project_rules")
        if previous and previous["fingerprint"] == fingerprint:
            return
        # 在准入当前轮次前，原子化记录规则版本并刷新上下文管理器
        with self.session.transaction():
            self.context_manager.refresh(
                self.system(self.skills.capture_catalog()), reason="project_instructions_changed"
            )
            self.session.append("project_rules", {"fingerprint": fingerprint}, True)

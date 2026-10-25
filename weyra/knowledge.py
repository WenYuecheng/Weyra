"""知识层门面（Knowledge Facade）与兼容性重导出中枢。

架构定位与设计哲学:
    本模块遵循门面模式（Facade Pattern），将分散在底层各职责模块中的知识表达、持久化与检索机制统一对外导出。
    构建了连接结构化指令、半结构化长期记忆（Memory）与可执行技能（Skills）的统一知识层抽象。

开源框架借鉴与学术溯源:
    1. WikiSkill (arXiv:2608.27454, 2026):
       - 整合 frontmatter 模块的元数据解析器（parse_markdown）、安全路径校验（reject_symlinks）
         与原子写入保障（atomic），构成具备自洽性（Self-containment）的单文件技能载体。
       - 导出 BM25 词法检索核心（tokens, rank），支持低延迟离线语义匹配。
    2. Hermes Agent (agent/memory/ & skills/):
       - 导出基于 Markdown 平面文件系统的长期记忆存储器（MemoryStore）与基于类 Git 语义版本控制的
         技能存储器（SkillStore, SkillSnapshot），保证跨会话状态的轻量化持久性。
"""

from .frontmatter import atomic, parse_markdown, rank, reject_symlinks, sha, tokens
from .memory import MemoryStore
from .skills import SkillSnapshot, SkillStore

__all__ = [
    "atomic",
    "parse_markdown",
    "rank",
    "reject_symlinks",
    "sha",
    "tokens",
    "MemoryStore",
    "SkillSnapshot",
    "SkillStore",
]


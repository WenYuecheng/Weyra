"""TUI 专用斜杠命令元数据目录与前缀匹配补全器（TUI Slash Commands Catalog）。

架构定位与设计哲学:
    本模块为 Textual TUI 的编辑器输入框提供实时命令联想与补全匹配。
    严格映射自底层 `weyra.session_commands` 统一命令规范，保证 UI 与核心服务层的命令参数完全对齐。
"""

from __future__ import annotations


from dataclasses import dataclass, field


@dataclass(frozen=True)
class TUICommand:
    """Metadata for a TUI-supported slash command."""

    name: str
    args_usage: str
    description: str
    aliases: tuple[str, ...] = field(default_factory=tuple)


TUI_COMMANDS: tuple[TUICommand, ...] = (
    TUICommand("/help", "", "帮助"),
    TUICommand("/session", "", "会话管理；别名 /sessions", aliases=("/sessions",)),
    TUICommand("/new", "", "新建会话"),
    TUICommand("/skills", "", "查看技能目录"),
    TUICommand("/skill-history", "<name>", "查看技能历史"),
    TUICommand("/skill-rollback", "<name> <version>", "从历史内容发布回滚版本"),
    TUICommand("/memory", "[query]", '查看或搜索记忆；保存：/memory save name "content"'),
    TUICommand("/models", "", "查看模型配置；别名 /routes", aliases=("/routes",)),
    TUICommand("/usage", "", "查看用量"),
    TUICommand("/cost", "", "查看费用与预留"),
    TUICommand("/plan", "[on|off]", "切换计划模式"),
    TUICommand("/compact", "", "压缩上下文（可能调用模型）"),
    TUICommand("/clear", "", "清空当前上下文，保留记录与费用"),
    TUICommand("/goal", "[text]", "查看或设置目标"),
    TUICommand("/tree", "", "查看会话记录树"),
    TUICommand("/branch", "<record>", "切换到指定记录分支"),
    TUICommand("/fork", "<name>", "分叉当前会话"),
    TUICommand("/steer", "<text>", "提交引导消息"),
    TUICommand("/refresh", "", "刷新项目指令、记忆与技能"),
    TUICommand("/mcp", "", "查看 MCP 配置"),
    TUICommand("/mcp-trust", "<name>", "信任指定 MCP 服务"),
    TUICommand("/followup", "<text>", "追加跟进要求"),
    TUICommand("/cancel", "", "取消当前任务"),
    TUICommand("/exit", "", "安全退出；别名 /quit", aliases=("/quit",)),
)


def match_commands(query: str) -> list[TUICommand]:
    """Match commands by canonical name or alias prefix."""
    if not query.startswith("/"):
        return []
    prefix = query.strip()
    if prefix == "/":
        return list(TUI_COMMANDS)
    matched: list[TUICommand] = []
    for cmd in TUI_COMMANDS:
        if cmd.name.startswith(prefix) or any(alias.startswith(prefix) for alias in cmd.aliases):
            if cmd not in matched:
                matched.append(cmd)
    return matched

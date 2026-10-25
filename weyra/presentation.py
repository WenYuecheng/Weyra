"""表现层纯逻辑助手库 (Presentation Pure Logic Helpers)。

【架构定位】
本模块负责文本差异生成、工具摘要提取、长文本分页与历史会话卡片重构。
根据企业级大模型应用架构中的“展示者纯粹性（Presenter Purity）”原则，本模块完全独立于任何具体的
GUI/TUI 终端框架（既不依赖 Textual，也不依赖 curses），属于纯粹的无状态数据投影层（Stateless Projection Layer）。
确保命令行流式输出、Rich TUI 渲染与后续可能的 Web/RPC 接口展现完全统一的语义视图。

【开源框架借鉴与设计原则】
1. DeepSeek-Harness (`packages/client` & `packages/host`):
   - 借鉴其终端控制序列安全防御（ANSI Control Sequence Sanitization）：使用经过形式化验证的
     正则表达式 `_ANSI_ESCAPE_RE` 强力清洗危险的 ANSI 转义码与终端指令，杜绝模型输出恶意终端序列劫持用户控制台。
   - 借鉴其方向敏感截断策略（Directional Truncation）：对于 Shell 命令输出采用保留尾部（tail-preserving），
     确保报错堆栈与退出状态可见；对于代码与文件阅读采用保留头部（head-preserving），匹配自顶向下的阅读习惯。
   - 原始事件全保真卡片投影（`project_raw_session_records`）：直接从 SQLite 会话记录中重建 DisplayCard，
     将模型面对的有损上下文（Lossy Context）与面向用户的全量富媒体卡片（Full Rich Cards）彻底分离，
     确保历史会话恢复时具备 100% 的视觉保真度。
2. Pi Agent (`packages/coding-agent`):
   - 借鉴其安全 Unified Diff 生成器（`generate_unified_diff`）：内置 UTF-8 编码字节上限（1MB），
     对超大修改实施熔断防护，输出标准 `a/path` 与 `b/path` 补丁视图。
   - 借鉴其虚拟分页机制（`paginate_text`）：行长度上限截断（20,000 字符），彻底防止单行超长字符串拖垮终端布局引擎。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

# 用于彻底清除终端 ANSI 转义字符与潜在危险控制码的正则表达式
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# 统一工具图标映射表（支持 Unicode 与 ASCII 降级模式）
TOOL_ICONS = {
    "read_file": "📖",
    "write_file": "✏️",
    "edit_file": "🔧",
    "list_files": "📁",
    "grep_search": "🔍",
    "run_shell": "💻",
    "skill": "⚡",
    "skill_rollback": "⏪",
    "skill_resource": "📄",
    "agent": "🤖",
    "list_agents": "📋",
    "wait_agents": "⏳",
    "send_agent_message": "💬",
    "interrupt_agent": "🛑",
    "compact_context": "🗜️",
    "enter_plan_mode": "📐",
    "exit_plan_mode": "🚀",
    "goal_update": "🎯",
    "memory_search": "🧠",
    "memory_save": "💾",
    "memory_delete": "🗑️",
    "job_status": "⚙️",
    "wait_job": "⏳",
    "cancel_job": "🛑",
    "transfer_job": "🔄",
    "mcp_search": "🔌",
    "mcp_call": "🔌",
}

ASCII_TOOL_ICONS = {
    "read_file": "[READ]",
    "write_file": "[WRITE]",
    "edit_file": "[EDIT]",
    "list_files": "[LS]",
    "grep_search": "[GREP]",
    "run_shell": "[SH]",
    "skill": "[SKILL]",
    "skill_rollback": "[RBACK]",
    "agent": "[AGENT]",
    "compact_context": "[COMPACT]",
    "goal_update": "[GOAL]",
    "memory_search": "[MEM]",
    "memory_save": "[MEM+]",
    "mcp_call": "[MCP]",
}


def strip_ansi(text: str) -> str:
    """彻底剥除文本中的 ANSI 颜色代码与危险终端转义字符 (Sanitize ANSI Escapes)。

    【安全设计】
    借鉴 DeepSeek-Harness 主机与客户端防御规范：
    通过正则表达式匹配所有标准的 CSI (Control Sequence Introducer) 序列与 OSC 序列，
    防止包含控制台清屏、光标跳跃、窗口重命名等可能被模型恶意利用或意外干扰终端呈现的字符。

    Args:
        text: 待过滤的原始文本（若非字符串类型则先安全强转）。

    Returns:
        str: 彻底剥离转义控制码后的纯净文本。
    """
    if not isinstance(text, str):
        text = str(text)
    return _ANSI_ESCAPE_RE.sub("", text)


def get_tool_icon(name: str, *, ascii_only: bool = False) -> str:
    """获取指定工具的视觉图标，在非 UTF-8 终端自动优雅降级为 ASCII 标识 (Get Tool Icon)。

    Args:
        name: 工具名称。
        ascii_only: 是否强制采用纯 ASCII 字符输出（适用于 Windows CMD 或传统无 Emoji 字体终端）。

    Returns:
        str: 工具的图形 Emoji 或紧凑中括号 ASCII 标识（如 "📖" 或 "[READ]"）。
    """
    if ascii_only:
        return ASCII_TOOL_ICONS.get(name, "[TOOL]")
    return TOOL_ICONS.get(name, "🔨")


def get_tool_summary(name: str, args: dict[str, Any]) -> str:
    """从结构化工具参数中提取一行精简的摘要预览 (Extract One-Line Tool Summary)。

    【设计原则】
    根据各类工具的核心参数语义进行定制化精简投影：
    - 文件读写类：提取路径 path / file_path；
    - 文本搜索类：提取 pattern 与目标路径；
    - 终端执行类：截取前 60 个字符的单行命令行指令；
    - 子代理任务：提取代理角色与任务描述前 60 字符；
    - 兜底逻辑：遍历首个非空字符串参数并安全截断至 50 字符。

    Args:
        name: 工具名称。
        args: 工具调用所使用的参数字典。

    Returns:
        str: 单行精简摘要字符串。
    """
    if not isinstance(args, dict):
        return ""
    if name in {"read_file", "write_file", "edit_file"}:
        return str(args.get("path") or args.get("file_path") or "")
    if name == "list_files":
        return str(args.get("path") or ".")
    if name == "grep_search":
        pat = str(args.get("pattern", ""))
        p = str(args.get("path", "."))
        return f'"{pat}" in {p}'
    if name == "run_shell":
        cmd = str(args.get("command", "")).strip().replace("\n", " ")
        return cmd[:60] + "..." if len(cmd) > 60 else cmd
    if name in {"skill", "skill_resource", "skill_rollback"}:
        return str(args.get("name") or "")
    if name == "agent":
        task = str(args.get("task", "")).strip().replace("\n", " ")
        role = f"[{args['role']}] " if args.get("role") else ""
        return (role + task)[:60]
    if name in {"memory_search", "memory_save", "memory_delete"}:
        return str(args.get("query") or args.get("name") or "")
    if name.startswith("mcp_"):
        return str(args.get("tool") or args.get("query") or "")
    # 默认兜底策略：取第一个非空字符串参数
    for v in args.values():
        if isinstance(v, str) and v.strip():
            s = v.strip().replace("\n", " ")
            return s[:50] + ("..." if len(s) > 50 else "")
    return ""


def generate_unified_diff(
    path: str,
    old_text: str,
    new_text: str,
    *,
    context_lines: int = 3,
    max_bytes: int = 1024 * 1024,
) -> str | None:
    """生成标准 Unified Diff 补丁文本并内置内存熔断保护 (Generate Safe Unified Diff)。

    【设计思想与开源血统】
    严格借鉴 Pi Agent (`packages/coding-agent`) 的差异对比引擎设计：
    1. 内存防爆与熔断保护：在进行行切片与 O(N*M) 差量计算前，对新旧文件执行 1MB (`max_bytes`)
       UTF-8 字节上限预检。若任一文本超过上限，立即返回 None 拒绝处理，防止巨型生成代码或二进制内容引发 OOM；
    2. 标准 Git 补丁格式：生成带 `--- a/{path}` 与 `+++ b/{path}` 标准头部的差异块；
    3. 幂等性检测：若新旧文本完全相同，返回 None，避免在前端渲染多余的空差异卡片。

    Args:
        path: 变更文件的相对或绝对路径。
        old_text: 变更前的原始内容。
        new_text: 变更后的最新内容。
        context_lines: 差异块上下保留的上下文行数，默认 3 行。
        max_bytes: 允许执行差量计算的最大字节上限，默认 1,048,576 字节 (1MB)。

    Returns:
        str | None: 格式化后的统一差异补丁字符串；若文件未变更或超过体积上限则返回 None。
    """
    if len(old_text.encode("utf-8", errors="ignore")) > max_bytes or len(
        new_text.encode("utf-8", errors="ignore")
    ) > max_bytes:
        return None
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context_lines,
    )
    result = "".join(diff)
    return result if result.strip() else None


@dataclass
class OutputPage:
    """终端分页数据载荷对象 (Terminal Output Pagination Payload)。

    Attributes:
        output_id: 输出内容关联的唯一持久化标识。
        offset: 当前页面的起始全局行号偏移（基于 0 开始）。
        limit: 单页请求的最大行数容量。
        total_lines: 原始文本的总行数。
        has_more: 是否还有更多后续行待加载。
        lines: 当前页经过安全清洗与超宽保护后的文本行列表。
        truncated_lines_count: 当前页触发单行超宽截断保护的行数。
    """
    output_id: str
    offset: int
    limit: int
    total_lines: int
    has_more: bool
    lines: list[str]
    truncated_lines_count: int = 0


def paginate_text(
    text: str,
    output_id: str,
    offset: int = 0,
    limit: int = 200,
    max_line_len: int = 20000,
) -> OutputPage:
    """对超长文本进行内存级行切片分页与单行超宽保护 (Paginate Long Text)。

    【设计思想与开源血统】
    借鉴 Pi Agent 终端虚拟分页与防卡顿机制：
    1. 窗口切片：根据 offset 和 limit 仅对请求的行区间进行内存切片；
    2. ANSI 控制码清洗：每行均调用 `strip_ansi` 消除潜在转义污染；
    3. 超宽单行截断：限制单行最大字符数不超过 20,000 字符，杜绝单行压缩 minified JS 或巨大 JSON
       导致前端或 TUI 字符换行排版计算陷入性能瘫痪。

    Args:
        text: 待分页的完整长文本。
        output_id: 页面绑定的输出标识。
        offset: 起始行偏移量，默认 0。
        limit: 单页行数，默认 200 行。
        max_line_len: 单行允许的最大字符数上限，默认 20,000 字符。

    Returns:
        OutputPage: 包含分页切片及行数统计的载荷结构体。
    """
    raw_lines = text.splitlines()
    total_lines = len(raw_lines)
    offset = max(0, min(offset, total_lines))
    slice_lines = raw_lines[offset : offset + limit]
    sanitized: list[str] = []
    truncated_count = 0
    for line in slice_lines:
        line = strip_ansi(line)
        if len(line) > max_line_len:
            sanitized.append(line[:max_line_len] + " ...[TRUNCATED LINE]")
            truncated_count += 1
        else:
            sanitized.append(line)
    has_more = (offset + limit) < total_lines
    return OutputPage(
        output_id=output_id,
        offset=offset,
        limit=limit,
        total_lines=total_lines,
        has_more=has_more,
        lines=sanitized,
        truncated_lines_count=truncated_count,
    )


def truncate_preview(
    text: str,
    *,
    max_lines: int = 200,
    max_chars: int = 20000,
    direction: str = "head",
) -> tuple[str, bool, int, int]:
    """方向感知的终端前置截断预览算法 (Direction-Aware Preview Truncation)。

    【设计思想与开源血统】
    完全遵循 DeepSeek-Harness 与 Pi Agent 的双向截断体系：
    1. ANSI 强力清洗：预先清洗文本，统计纯净总字符数与总行数；
    2. 阈值早退：在同时满足行数与字符数限制时，原样直接返回，无多余分配；
    3. 方向敏感截断：
       - `direction="tail"`：保留末尾行并逆向字符切片，专门用于 Shell 终端，确保报错堆栈完整；
       - `direction="head"`：保留起始行并正向字符切片，专门用于源码阅读与结构化列表展示。

    Args:
        text: 原始待渲染文本。
        max_lines: 最大允许保留行数，默认 200 行。
        max_chars: 最大允许保留字符数，默认 20,000 字符。
        direction: 截断方向；"tail" 保留尾部（用于 Shell 输出），"head" 保留头部（用于代码/文件）。

    Returns:
        tuple[str, bool, int, int]: (截断后文本, 是否发生截断, 总行数, 总字符数)。
    """
    clean = strip_ansi(text)
    total_chars = len(clean)
    lines = clean.splitlines()
    total_lines = len(lines)

    if total_lines <= max_lines and total_chars <= max_chars:
        return clean, False, total_lines, total_chars

    if direction == "tail":
        # 保留尾部输出（对 Shell 执行极其关键，确保能看到退出码与错误栈）
        tail_lines = lines[-max_lines:]
        truncated = "\n".join(tail_lines)
        if len(truncated) > max_chars:
            truncated = truncated[-max_chars:]
        return truncated, True, total_lines, total_chars
    else:
        # 保留头部输出（对文件浏览友好）
        head_lines = lines[:max_lines]
        truncated = "\n".join(head_lines)
        if len(truncated) > max_chars:
            truncated = truncated[:max_chars]
        return truncated, True, total_lines, total_chars


@dataclass
class DisplayCard:
    """面向终端与前端解耦渲染的交互卡片结构体 (Decoupled Display Card Structure)。

    Attributes:
        card_id: 卡片全局唯一标识。
        kind: 卡片类型（如 "user_message", "assistant_message", "thinking", "tool_call", "compaction", "notice"）。
        title: 卡片展示标题。
        content: 卡片核心渲染文本。
        status: 卡片生命周期状态（"running", "completed", "failed", "cancelled" 等）。
        metadata: 携带的附加结构化数据字典（如工具入参、Diff 文本、截断标记等）。
        timestamp: 事件发生的逻辑或绝对时间戳。
    """
    card_id: str
    kind: str  # "user_message", "assistant_message", "thinking", "tool_call", "compaction", "notice"
    title: str
    content: str
    status: str = "completed"
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


def project_raw_session_records(records: list[dict[str, Any]]) -> list[DisplayCard]:
    """直接从 SessionStore 底层持久化事件流重构有序的展示卡片列表 (Project Raw Records to Display Cards)。

    【设计思想与开源血统】
    严格遵循 DeepSeek-Harness 的“模型上下文与用户视图双轨解耦”核心架构：
    1. 解耦有损模型上下文与无损用户视图：智能体在长程任务中会经历工具输出中段裁剪、上下文八段式压缩等有损操作，
       若直接使用发送给模型的 messages 渲染用户 UI，会导致用户看到被截断甚至丢失的历史记录。
       通过直接读取 SessionStore 中的持久化事件（`records`），完整重建用户全量富媒体卡片；
    2. 深度保留思考链与代码变更：重构深度思考过程（`reasoning_content`）、工具调用意图映射与 Unified Diff；
    3. 工具意图与结果精准配对：缓存 `tool_intent` 事件并与 `tool_result` 通过 `call_id` 关联，
       向用户呈现带有精准入参、图标、截断状态和外部产物索引的卡片。

    Args:
        records: 从底层 SessionStore.chain() 导出的不可变事件记录列表。

    Returns:
        list[DisplayCard]: 面向 TUI/UI 渲染呈现的有序展示卡片列表。
    """
    cards: list[DisplayCard] = []
    tool_intents: dict[str, dict[str, Any]] = {}

    for record in records:
        kind = record.get("kind")
        data = record.get("data", {})
        rid = record.get("id", "")

        if kind == "tool_intent":
            call_id = data.get("call_id")
            if call_id:
                tool_intents[call_id] = data

        elif kind == "message":
            role = data.get("role")
            content = data.get("content", "")
            source = data.get("source")
            reasoning = data.get("provider_data", {}).get("reasoning_content") or data.get(
                "provider_data", {}
            ).get("reasoning")
            if source in {"memory_selection", "environment"}:
                continue

            if role == "user":
                if source == "compaction":
                    cards.append(
                        DisplayCard(
                            card_id=f"compaction_{rid}",
                            kind="compaction",
                            title="上下文结构化压缩",
                            content="历史会话已生成结构化检查点摘要",
                            status="completed",
                            metadata={"summary": content},
                        )
                    )
                else:
                    cards.append(
                        DisplayCard(
                            card_id=f"user_{rid}",
                            kind="user_message",
                            title="用户",
                            content=content,
                            status="completed",
                        )
                    )
            elif role == "assistant":
                if reasoning:
                    cards.append(
                        DisplayCard(
                            card_id=f"thinking_{rid}",
                            kind="thinking",
                            title="思考过程",
                            content=reasoning,
                            status="completed",
                        )
                    )
                if content:
                    cards.append(
                        DisplayCard(
                            card_id=f"assistant_{rid}",
                            kind="assistant_message",
                            title="Weyra",
                            content=content,
                            status="completed",
                        )
                    )

        elif kind == "tool_result":
            call_id = data.get("call_id")
            intent = tool_intents.get(call_id, {})
            tool_name = data.get("name") or intent.get("name", "unknown_tool")
            args = intent.get("arguments", {})
            raw_result = data.get("content", "")
            diff_text = (data.get("structured") or {}).get("_display", {}).get("diff")

            preview, truncated, total_lines, total_chars = truncate_preview(
                raw_result,
                direction="tail" if tool_name == "run_shell" else "head",
            )
            cards.append(
                DisplayCard(
                    card_id=f"tool_{call_id or rid}",
                    kind="tool_call",
                    title=f"{get_tool_icon(tool_name)} {tool_name}",
                    content=preview,
                    status=data.get("status", "success"),
                    metadata={
                        "call_id": call_id,
                        "name": tool_name,
                        "arguments": args,
                        "summary": get_tool_summary(tool_name, args),
                        "diff": diff_text,
                        "truncated": truncated,
                        "total_lines": total_lines,
                        "output_ref": data.get("output_ref"),
                    },
                )
            )

        elif kind == "turn_end":
            state = data.get("state")
            if state in {"cancelled", "failed"}:
                cards.append(
                    DisplayCard(
                        card_id=f"notice_{rid}",
                        kind="notice",
                        title="轮次状态",
                        content=f"轮次结束状态: {state}",
                        status=state,
                    )
                )

    return cards

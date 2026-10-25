"""工具输出有向截断、全量产物溢出落盘与前置预览格式化引擎 (Directional Output Truncation & Preview Formatting)。

【架构定位】
本模块负责对工具执行返回的大容量原始文本执行安全截断与格式化。
在将全量执行输出完整存入 `.weyra/artifacts/` 产物目录的前提下，生成符合上下文窗口限制的紧凑预览，
防止单个工具返回海量日志（如无限循环输出、巨型日志文件）导致上下文溢出或大模型处理瘫痪。

【开源与工程设计借鉴】
1. 借鉴 Pi Agent (`packages/coding-agent/src/truncate.ts`) 的双向容量截断标准 (Directional Truncation)：
   - `truncate_head`（头部保留截断）：用于 `read_file`、`grep_search` 等按顺序读取的工具，优先保留起始前缀行；
   - `truncate_tail`（尾部保留截断）：用于 `run_shell`、`wait_job` 等终端工具，优先保留末尾的执行摘要与异常堆栈（Traceback）；
   - 容量硬上界：默认行数上限 2,000 行，默认字节上限 51,200 字节 (50KiB)；
2. 借鉴 DeepSeek-Harness (`packages/compaction/`) 的 8KiB 边界中段修剪 (Middle Pruning)：
   - `prune_tool_result`：当工具内容超出 8,192 字符时，截取前 4,096 字符与后 1,024 字符并插入修剪标记，兼顾输入对齐与输出结果。
"""

from dataclasses import dataclass

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 51200
GREP_MAX_LINE_LENGTH = 500
PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"


@dataclass(frozen=True)
class TruncationResult:
    """截断处理结果元数据对象 (Truncation Result Metadata)。

    Attributes:
        content: 经过截断处理后的文本内容。
        truncated: 是否触发了截断动作。
        output_lines: 截断保留并实际输出的有效行数。
        total_lines: 原始未截断文本的总行数。
        truncated_by: 触发截断的限制维度，可选 "lines"（行数超限）或 "bytes"（字节超限）。
        first_line_exceeds_limit: 首行自身是否已经超过字节上限。
        last_line_partial: 保留的最后一行是否遭遇了字符级字节切片截断。
    """
    content: str
    truncated: bool = False
    output_lines: int = 0
    total_lines: int = 0
    truncated_by: str | None = None
    first_line_exceeds_limit: bool = False
    last_line_partial: bool = False


def _lines(content: str) -> list[str]:
    """按换行符切分文本为行列表并安全剔除尾部空行 (Split Lines Safe Helper)。

    【设计原则】
    标准 Python `split('\\n')` 在末尾存在换行符时会产生多余的空字符串末尾行。
    本函数在保留所有中间空行的同时，统一剥除末尾由末尾换行符带来的纯占位空行，
    保证截断计数器与行号映射精确吻合。

    Args:
        content: 待切分的原始字符串内容。

    Returns:
        list[str]: 切分后的行字符串列表。若输入为空字符串则返回空列表。
    """
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def truncate_head(
    content: str, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = DEFAULT_MAX_BYTES
) -> TruncationResult:
    """从开头保留指定行数与字节上限的头部截断算法 (Head-Preserving Directional Truncation)。

    【设计思想与开源血统】
    严格遵循 Pi Agent (`packages/coding-agent/src/truncate.ts`) 的文件与代码阅读截断标准：
    1. 适用场景：针对 `read_file`、`grep_search`、`list_files` 等从前向后阅读的工具输出；
    2. 边界控制：累计保留不超过 max_lines（默认 2,000 行），累计 UTF-8 字节不超过 max_bytes（默认 50KiB）；
    3. 防御特例：若第一行文本本身已超过 max_bytes，则直接丢弃并标记 `first_line_exceeds_limit=True`，
       引导模型通过范围切片命令（如 `read_file` 的 offset 参数）进行细粒度按需读取。

    Args:
        content: 待截断的原始文本内容。
        max_lines: 最大允许保留的行数上限，默认 2000 行。
        max_bytes: 最大允许保留的 UTF-8 编码字节上限，默认 51200 字节 (50KiB)。

    Returns:
        TruncationResult: 封装了截断后内容与详细统计元数据的不可变结果对象。
    """
    lines = _lines(content)
    if len(lines) <= max_lines and len(content.encode("utf-8")) <= max_bytes:
        return TruncationResult(content, output_lines=len(lines), total_lines=len(lines))
    if lines and len(lines[0].encode("utf-8")) > max_bytes:
        return TruncationResult("", True, 0, len(lines), "bytes", True)
    selected, size, reason = [], 0, "lines"
    for line in lines[:max_lines]:
        cost = len(line.encode("utf-8")) + bool(selected)
        if size + cost > max_bytes:
            reason = "bytes"
            break
        selected.append(line)
        size += cost
    return TruncationResult("\n".join(selected), True, len(selected), len(lines), reason)


def truncate_tail(
    content: str, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = DEFAULT_MAX_BYTES
) -> TruncationResult:
    """从末尾向前保留指定行数与字节上限的尾部截断算法 (Tail-Preserving Directional Truncation)。

    【设计思想与开源血统】
    严格遵循 Pi Agent (`packages/coding-agent/src/truncate.ts`) 与 DeepSeek-Harness 的终端输出截断规范：
    1. 适用场景：针对 `run_shell`、`wait_job` 等命令行子进程与终端命令输出；
    2. 报错保全：当构建编译、自动化测试或部署脚本发生崩溃退出时，关键错误信息（如 Python Traceback、
       编译器 SyntaxError、Go Panic、Jest Failure）几乎 100% 集中在输出的最末尾；
    3. 逆向累计：从尾行开始向前扫描累计字节，确保退出状态与崩溃根因优先留在模型上下文中；
    4. 局部兜底：若最后一行自身超过 max_bytes 且尚未收集到任何行，对其末尾进行局部切片并标记 `last_line_partial=True`。

    Args:
        content: 待截断的原始终端输出文本。
        max_lines: 最大允许保留的行数上限，默认 2000 行。
        max_bytes: 最大允许保留的 UTF-8 编码字节上限，默认 51200 字节 (50KiB)。

    Returns:
        TruncationResult: 封装了截断后内容与详细统计元数据的不可变结果对象。
    """
    lines = _lines(content)
    if len(lines) <= max_lines and len(content.encode("utf-8")) <= max_bytes:
        return TruncationResult(content, output_lines=len(lines), total_lines=len(lines))
    selected, size, reason, partial = [], 0, "lines", False
    for line in reversed(lines[-max_lines:]):
        cost = len(line.encode("utf-8")) + bool(selected)
        if size + cost > max_bytes:
            reason = "bytes"
            if not selected:
                selected.append(line.encode("utf-8")[-max_bytes:].decode("utf-8", "ignore"))
                partial = True
            break
        selected.append(line)
        size += cost
    return TruncationResult(
        "\n".join(reversed(selected)),
        True,
        len(selected),
        len(lines),
        reason,
        last_line_partial=partial,
    )


def prune_tool_result(content: str) -> str:
    """工具大文本输出的 8KiB 边界中段修剪算法 (DeepSeek-Harness Middle Pruning)。

    【设计思想与开源血统】
    完全遵循 DeepSeek-Harness (`packages/compaction/`) 核心压缩流水线的中段修剪规范：
    1. 触发水位：文本长度严格小于等于 8,192 字符时保持原样，不发生任何信息损失；
    2. 头部保留 4,096 字符：保留命令启动、初始参数确认、前置文件结构等关键上下文事实；
    3. 尾部保留 1,024 字符：保留最终完成状态、汇总统计、退出提示或报错概要；
    4. 插入显式修剪标记 `\\n\\n[... tool result middle pruned ...]\\n\\n`：明确告知下游 LLM
       中间部分因篇幅过大已被截除，杜绝模型产生幻觉。

    Args:
        content: 待修剪的工具输出文本。

    Returns:
        str: 修剪后的文本（若未超限则返回原始文本）。
    """
    return content if len(content) <= 8192 else content[:4096] + PRUNE_MARKER + content[-1024:]


def preview_output(result) -> str:
    """根据工具语义定制前置交互预览并附加产物引用提示 (Format Tool Preview & Artifact Reference)。

    【设计思想与开源血统】
    结合 Pi Agent 的增量提示机制与 DeepSeek-Harness 产物外置落盘架构：
    1. 面向 `read_file`：提取结构化 offset/limit 元数据，若首行超 50KiB 则提示使用 `run_shell` 字节范围读取；
       若发生行截断则明确提示后续继续阅读的命令参数（如 `Use offset=... to continue.`）；
    2. 面向 `grep_search`：单行超长（>500 字符）自动截断，超过匹配条数上限时提示使用 `limit` 翻倍或优化正则；
    3. 面向 `run_shell` / `wait_job`：强制采用尾部截断（`truncate_tail`），附加非零返回码提示；
    4. 全量落盘索引：所有发生截断的工具输出，均追加 `Full output: {output_ref}` 标记，指示磁盘持久化路径。

    Args:
        result: 工具执行结果对象（携带 name, content, structured, output_ref 等属性）。

    Returns:
        str: 经过定制化格式与截断处理后的交互预览文本。
    """
    metadata = result.structured if isinstance(result.structured, dict) else {}
    if result.name == "read_file" and metadata.get("_preview") == "read":
        start = metadata["offset"]
        full_lines = result.content.split("\n")
        selected = (
            "\n".join(full_lines[: metadata["limit"]]) if metadata.get("limit") else result.content
        )
        truncation = truncate_head(selected)
        if truncation.first_line_exceeds_limit:
            return (
                f"[Line {start} exceeds 50.0KB limit. Use run_shell to read a byte range. "
                f"Full output: {result.output_ref}]"
            )
        if truncation.truncated:
            end = start + truncation.output_lines - 1
            return (
                truncation.content + f"\n\n[Showing lines {start}-{end} of "
                f"{metadata['total_lines']}. Use offset={end + 1} to continue.]"
            )
        if metadata.get("limit") is not None and metadata["limit"] < len(full_lines):
            remaining = len(full_lines) - metadata["limit"]
            return (
                truncation.content + f"\n\n[{remaining} more lines in file. "
                f"Use offset={start + metadata['limit']} to continue.]"
            )
        return truncation.content
    if result.name == "grep_search" and metadata.get("_preview") == "grep":
        matches = metadata["matches"]
        limit = metadata.get("limit", 100)
        lines = [
            f"{m['path']}:{m['line']}: "
            + (m["text"][:500] + "... [truncated]" if len(m["text"]) > 500 else m["text"])
            for m in matches[:limit]
        ]
        truncation = truncate_head("\n".join(lines), max_lines=max(len(lines), 1))
        notices = []
        if len(matches) >= limit:
            notices.append(
                f"{limit} matches limit reached. Use limit={limit * 2} for more, or refine pattern"
            )
        if truncation.truncated:
            notices.append("50.0KB limit reached")
        if any(len(m["text"]) > 500 for m in matches[:limit]):
            notices.append("Some lines truncated to 500 chars. Use read_file to see full lines")
        return (truncation.content or "No matches found") + (
            "\n\n[" + ". ".join(notices) + "]" if notices else ""
        )
    tail = result.name in {"run_shell", "wait_job"}
    truncation = truncate_tail(result.content) if tail else truncate_head(result.content)
    content = truncation.content
    if truncation.truncated:
        content += (
            f"\n\n[Output truncated ({truncation.truncated_by}). Full output: {result.output_ref}]"
        )
    if tail and metadata.get("returncode") not in {0, None}:
        content += f"\n\nCommand exited with code {metadata['returncode']}"
    return content

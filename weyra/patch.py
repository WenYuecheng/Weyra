"""基于 Codex 规范的有界补丁纯文本解析器与正向精确匹配引擎 (Patch Parser & Line Matcher)。

【架构定位】(Architectural Role)
    位于 `weyra` 核心代码操作与编辑基础设施层，负责将大模型生成的 Codex 风格
    Begin/End Patch 文本解析为结构化的 `PatchOperation` 与 `Hunk` 序列，
    并提供纯内存的确定性正向行匹配应用引擎。本模块为纯函数实现，不包含任何文件系统 I/O 与权限判断。

【开源设计思想与架构血统】(Design Pedigree)
    1. 遵循 SkillOpt (arXiv:2605.23904) 的局部有界补丁哲学（Bounded Patch Representation）：
       废弃全量重写，支持 Add、Delete、Update 与 Move 文件级原子算子，限定精准正向上下文匹配；
    2. 兼容 Codex 补丁协议规范（`*** Begin Patch` / `*** End Patch` / `@@` / `*** End of File`）；
    3. 纯函数安全隔离：语法解析与内存仿真绝对不接触磁盘，所有文件写入与权限边界统一委托给 `patch_tool`。

【核心不变性与安全约束】(Invariants & Safety)
    - 精确正向行匹配：拒绝模糊推测匹配，除 CRLF 标准化外保持字符与换行完全确定；
    - 路径安全防注入：补丁目标路径必须非空且严禁包含空字符（NUL `\\x00`）；
    - 严格帧边界：必须具备 `*** Begin Patch` 首行与 `*** End Patch` 末行，拒绝格式残缺补丁。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Hunk:
    """单个局部代码修改块 (Code Change Hunk)。

    【架构定位】
    补丁操作的细粒度差异块，记录前序基准行（old）、替换目标行（new）及上下文锚点。

    Attributes:
        old: 匹配目标原始文本行元组（被删除或作为上下文的行）。
        new: 拟替换生成的目标文本行元组（新增或保留的行）。
        anchor: 可选的上下文起始搜索锚点行文本。
        eof: 是否匹配至文件末尾（*** End of File 标记）。
    """

    old: tuple[str, ...]
    new: tuple[str, ...]
    anchor: str | None = None
    eof: bool = False


@dataclass(frozen=True)
class PatchOperation:
    """文件级原子补丁操作规范 (File-level Patch Operation)。

    【架构定位】
    定义针对单一文件的增、删、改或重命名移动原子操作。

    Attributes:
        kind: 操作类型枚举（'add', 'delete', 'update'）。
        path: 操作的目标相对路径。
        content: 新增文件时的完整内容（仅当 kind='add' 时有效）。
        hunks: 局部编辑块元组（仅当 kind='update' 时有效）。
        move_to: 可选的目标重命名目标路径（文件移动操作）。
    """

    kind: str
    path: str
    content: str = ""
    hunks: tuple[Hunk, ...] = ()
    move_to: str | None = None


def _path(value: str) -> str:
    """校验补丁目标路径字符串的合法性与安全性。

    Args:
        value: 待校验的原始路径字符串。

    Returns:
        str: 校验合规的路径字符串。

    Raises:
        ValueError: 当路径为空或包含 NUL 字节时抛出。
    """
    if not value.strip() or "\x00" in value:
        raise ValueError("Patch paths must be nonempty and contain no NUL")
    return value


def parse_patch(text: str) -> tuple[PatchOperation, ...]:
    """解析完整的 Codex 风格 Begin/End 补丁文本并生成操作序列。

    【架构定位】
    补丁文本解析的核心入口，负责格式拆解、语法校验与分块提取。

    【核心不变性与安全约束】
    - 必须以 `*** Begin Patch` 起始并以 `*** End Patch` 终结；
    - 指令仅限 `Add File: `, `Delete File: `, `Update File: ` 与可选的 `*** Move to: `；
    - 补丁内部必须包含至少一项有效的文件操作，严禁空补丁穿透。

    Args:
        text: 包含完整 Begin/End 帧的补丁文本字符串。

    Returns:
        tuple[PatchOperation, ...]: 有序解析出的补丁原子操作元组。

    Raises:
        ValueError: 当格式不合规、指令未知、缺少行块或缺失必要操作时抛出。
    """
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("Expected Begin Patch and End Patch framing")
    operations = []
    i = 1
    while i < len(lines) - 1:
        header = lines[i]
        kind = next(
            (
                kind
                for kind in ("Add", "Delete", "Update")
                if header.startswith(f"*** {kind} File: ")
            ),
            None,
        )
        if kind is None:
            raise ValueError("Expected Add, Delete, or Update File directive")
        path = _path(header[len(f"*** {kind} File: ") :])
        i += 1
        if kind == "Delete":
            operations.append(PatchOperation("delete", path))
            continue
        if kind == "Add":
            content = []
            while i < len(lines) - 1 and lines[i].startswith("+"):
                content.append(lines[i][1:])
                i += 1
            operations.append(
                PatchOperation("add", path, "\n".join(content) + ("\n" if content else ""))
            )
            continue
        move_to = None
        if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
            move_to = _path(lines[i][len("*** Move to: ") :])
            i += 1
        hunks = []
        while i < len(lines) - 1 and (lines[i] == "@@" or lines[i].startswith("@@ ")):
            anchor = lines[i][3:] if lines[i].startswith("@@ ") else None
            i += 1
            old, new = [], []
            count = 0
            while i < len(lines) - 1 and lines[i][:1] in {" ", "+", "-"}:
                prefix, body = lines[i][0], lines[i][1:]
                if prefix != "+":
                    old.append(body)
                if prefix != "-":
                    new.append(body)
                count += 1
                i += 1
            if not count:
                raise ValueError("An update hunk must contain lines")
            eof = i < len(lines) - 1 and lines[i] == "*** End of File"
            if eof:
                i += 1
            hunks.append(Hunk(tuple(old), tuple(new), anchor, eof))
        if not hunks and move_to is None:
            raise ValueError("An update needs hunks or a move destination")
        operations.append(PatchOperation("update", path, hunks=tuple(hunks), move_to=move_to))
    if not operations:
        raise ValueError("A patch must contain at least one operation")
    return tuple(operations)


def apply_hunks(text: str, hunks: tuple[Hunk, ...]) -> str:
    """按正向顺序精确应用代码修改块，完整保留原始文本换行风格。

    【架构定位】
    纯文本差异行合并核心引擎，在源文本中定位匹配上下文并生成修改后文本。

    【核心不变性与安全约束】
    - 正向确定性扫描：严格依序向后匹配锚点与上下文，杜绝回溯歧义；
    - 上下文严格一致：若目标文件中未找到指定的上下文匹配行或锚点，立即抛出明确异常。

    Args:
        text: 待修改的原始源文件文本。
        hunks: 拟应用的局部修改块元组。

    Returns:
        str: 应用修改后的完整新文本。

    Raises:
        ValueError: 当指定的锚点或上下文行在源文本中无法唯一定位时抛出。
    """
    if not hunks:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n")
    final_newline = normalized.endswith("\n") or not normalized
    lines = normalized.split("\n") if normalized else []
    if normalized.endswith("\n"):
        lines.pop()
    cursor = 0
    output = []
    for hunk in hunks:
        start = cursor
        if hunk.anchor is not None:
            try:
                start = lines.index(hunk.anchor, start) + 1
            except ValueError:
                raise ValueError("Patch anchor was not found") from None
        if not hunk.old:
            position = len(lines)
        else:
            positions = (
                (len(lines) - len(hunk.old),)
                if hunk.eof
                else range(start, len(lines) - len(hunk.old) + 1)
            )
            position = next(
                (
                    pos
                    for pos in positions
                    if pos >= start and tuple(lines[pos : pos + len(hunk.old)]) == hunk.old
                ),
                None,
            )
            if position is None:
                raise ValueError("Patch context was not found")
        output.extend(lines[cursor:position])
        output.extend(hunk.new)
        cursor = position + len(hunk.old)
    output.extend(lines[cursor:])
    return newline.join(output) + (newline if output and final_newline else "")

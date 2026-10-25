"""Markdown 元数据解析、原子文件落盘与双语 BM25 词法检索核心。

架构定位与设计哲学:
    本模块为技能（Skills）与长期记忆（Memory）提供基础的文件解析、磁盘写入可靠性与离线检索能力。
    在企业级 Agent 本地持久化中，外部数据文件可能遭遇进程崩溃导致的半写损毁（Torn Write）、
    符号链接跨目录提权（Symlink Traversal）以及因依赖外部向量数据库或嵌入模型导致的冷启动慢与网络脆弱性。
    本模块通过操作系统级原子替换、形式化安全路径校验与纯 Python 双语分词 BM25 算法，
    提供了自包含、高可靠、零外部依赖的检索与存储底层支撑。

开源框架借鉴与学术溯源:
    1. WikiSkill (arXiv:2608.27454, 2026):
       - 借鉴其统一 Frontmatter 规范（parse_markdown）：从 Markdown 首部提取 YAML/JSON 形式化元数据
         （name, description, when_to_use 等）并与主体指令（Body）完全解耦，提供技能自省能力。
       - 符号链接攻击免疫（reject_symlinks）：递归校验路径及其所有父目录，严格禁止软链接跨根逃逸。
    2. Hermes Agent (hermes_state.py & memory/):
       - POSIX 原子写屏障（atomic）：采用 `tempfile.mkstemp` 在同目录下创建临时文件，完成内容写入后
         强制执行 `os.fsync` 落盘，再利用操作系统内核的 `os.replace` 进行原子原子替换，彻底避免半写损坏。
    3. BM25 Okapi 词法检索算法 (Lucene / Information Retrieval SOTA):
       - 中英混合滑动窗口分词（tokens）：结合英文单词字符、中文字符一元（Unigram）以及相邻中文字符
         二元组（Bigram），大幅提升短文本与技术专有名词的召回率。
       - 工业级 BM25 评分（rank）：标准参数设置（k1=1.2, b=0.75）与逆文档频率（IDF）平滑计算，
         实现纯离线、毫秒级的零依赖精准记忆检索。
"""

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


def sha(text: str) -> str:
    """计算 UTF-8 文本的标准 SHA-256 哈希十六进制摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic(path, text: str):
    """原子化安全落盘文件写入。

    工程防御机制:
        1. 在目标文件的同级父目录中创建临时文件（确保在同一个物理文件系统分区，避免跨设备移动失败）。
        2. 写入文本并显式执行 os.fsync(fd)，强制刷写操作系统脏页到物理磁盘。
        3. 利用 POSIX os.replace 实现原子级替换，即使系统断电也不会留下损坏的半写文件。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".weyra-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def reject_symlinks(path):
    """形式化符号链接校验（防目录穿越与提权）。

    遍历 path 自身及其所有父路径，若发现任意一层为符号链接立即抛出 ValueError。
    """
    for component in [Path(path), *Path(path).parents]:
        if component.is_symlink():
            raise ValueError("Symlink paths are not allowed")


def tokens(text: str) -> list[str]:
    """中英文混合词法分词器。

    分词策略:
        - 英文及数字：提取连续字母数字单词。
        - CJK 汉字：提取单字一元（Unigram），同时构建相邻双字滑动窗口（Bigram），
          极大增强无分词库环境下的中文专业术语倒排检索命中率。
    """
    text = text.casefold()
    return (
        re.findall(r"[^\W_]+", text)
        + re.findall(r"[\u3400-\u9fff]", text)
        + [
            text[i : i + 2]
            for i in range(len(text) - 1)
            if all("\u3400" <= c <= "\u9fff" for c in text[i : i + 2])
        ]
    )


def rank(query: str, items: list[tuple[Any, str]], limit: int) -> list[Any]:
    """基于 BM25 Okapi 算法的纯 Python 离线轻量检索排序。

    Args:
        query: 用户检索查询语句。
        items: 待排序候选列表，元素格式为 (item_payload, text_corpus)。
        limit: 最大返回候选数量。

    Returns:
        list[Any]: 按 BM25 评分降序排列的原始 item 载荷列表。
    """
    q = set(tokens(query))
    docs = [Counter(tokens(text)) for _, text in items]
    n = len(docs)
    avg = sum(sum(d.values()) for d in docs) / max(n, 1) or 1
    scores = []
    for (item, _), d in zip(items, docs):
        score = 0
        for term in q:
            freq = d[term]
            df = sum(term in x for x in docs)
            score += (
                math.log(1 + (n - df + 0.5) / (df + 0.5))
                * freq
                * 2.2
                / (freq + 1.2 * (0.25 + 0.75 * sum(d.values()) / avg))
                if freq
                else 0
            )
        if score:
            scores.append((score, item))
    return [item for _, item in sorted(scores, key=lambda x: (-x[0], str(x[1])))[:limit]]


def parse_markdown(text: str) -> tuple[dict[str, Any], str]:
    """解析附带 Frontmatter 头的 Markdown 文本（借鉴 WikiSkill 设计）。

    提取开头的 `---` 元数据块，解析其中的键值对，并返回 (metadata_dict, markdown_body)。
    """
    metadata = {}
    body = text
    if text.startswith("---\n"):
        parts = text.split("\n---", 1)
        if len(parts) != 2:
            raise ValueError("Unclosed frontmatter")
        for line in parts[0][4:].splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                key, value = line.split(":", 1)
                value = value.strip()
                if value.startswith(chr(34)):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = value.strip(chr(34))
                else:
                    value = value.strip(chr(39))
                metadata[key.strip()] = value
        body = parts[1].lstrip("\r\n")
    return metadata, body


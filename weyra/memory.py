"""基于 Markdown 的持久化语义记忆库与有界检索回忆引擎 (Markdown Memory Store & Bounded Recall Engine)。

【架构定位】
本模块负责 Weyra 智能体在多会话跨周期的长期事实记忆存储与动态回忆。
不同于上下文窗口内的易失性对话，记忆以离散的 Markdown 文件形式持久化于项目 `.weyra/memory/` 目录中，
支持开发者与智能体双向直接查看与修改。

【开源与工程设计借鉴】
1. 借鉴 Hermes Agent (`agent/memory.py`) 的透明 Markdown 记忆架构：
   - 保持极简人类可读性：每个记忆条目是一个标准 `.md` 文件，配合原子写 (`atomic`) 与符号链接拦截防提权；
   - 变更检测与单调版本号：通过 `manifest.json` 自动跟踪文件的 SHA-256 内容哈希，检测外部编辑并单调递增 `version`；
2. 词法筛选 + 模型语义精选双级召回模型 (`recall_entries`)：
   - 初始阶段：利用 `rank` 快速做轻量词法匹配召回候选；
   - 精排阶段：由 `selector` 回调调度专用 memory 路由模型对候选摘要做最终意图匹配；
3. 上下文容量保护与单次注入预算上限：
   - 会话中累计可见记忆字节数严格限制在 60KiB 以内；
   - 注入前与当前模型上下文剩余 Token 做动态比对，超出预算直接阻断注入，防止记忆挤占推理空间。
"""

import asyncio
import json
from pathlib import Path
from .frontmatter import atomic, rank, reject_symlinks, sha


class MemoryStore:
    """项目级 Markdown 语义事实记忆仓库 (Project Memory Store)。"""

    def __init__(self, project_path, state_dir=None):
        """初始化项目级 Markdown 记忆仓库。

        【架构定位】
        负责多轮交互中通用知识与经验事实的磁盘文件持久化。

        【设计思想与开源血统】
        遵循 Hermes Agent (`agent/memory.py`) 透明 Markdown 架构：
        使用标准文件系统，严格禁止符号链接防范越权，并以 SHA-256 维护 manifest.json 索引。

        Args:
            project_path: 项目工程根目录。
            state_dir: 可选的自定义存储目录路径（默认 `.weyra/memory/`）。
        """
        self.root = (
            Path(state_dir) if state_dir else Path(project_path).resolve() / ".weyra" / "memory"
        )
        reject_symlinks(self.root)
        self.root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._refresh()
        self._background = set()
        self._closed = False

    def _path(self, name):
        """安全解析指定记忆名称对应的物理 Markdown 文件路径，并防范路径穿越与软链接提权。

        Args:
            name: 记忆名称标识。

        Returns:
            Path: 物理文件路径。

        Raises:
            ValueError: 名称包含斜杠或相对路径保留字。
        """
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError("Invalid memory name")
        path = self.root / (name + ".md")
        reject_symlinks(path)
        return path

    def _refresh(self):
        """扫描物理目录，依据 SHA-256 哈希计算增量变更，原子化同步 manifest.json 元数据索引。"""
        self.records = {}
        reject_symlinks(self.root / "manifest.json")
        try:
            old = (
                json.loads((self.root / "manifest.json").read_text())
                if (self.root / "manifest.json").exists()
                else {}
            )
        except (ValueError, OSError):
            old = {}
        if not isinstance(old, dict):
            old = {}
        for p in sorted(self.root.glob("*.md")):
            reject_symlinks(p)
            content = p.read_text(encoding="utf-8")
            h = sha(content)
            prior = old.get(p.stem, {})
            if not isinstance(prior, dict):
                prior = {}
            previous_version = prior.get("version", 0)
            if type(previous_version) is not int or previous_version < 0:
                previous_version = 0
            self.records[p.stem] = {
                "id": p.stem,
                "content": content,
                "content_hash": h,
                "version": previous_version + (h != prior.get("content_hash")),
            }
        atomic(
            self.root / "manifest.json",
            json.dumps(self.records, ensure_ascii=False, sort_keys=True),
        )

    def create(self, name, content):
        """新建一条持久化 Markdown 记忆条目。

        Args:
            name: 记忆名称。
            content: 记忆内容。

        Returns:
            dict: 记忆条目元数据字典。

        Raises:
            ValueError: 同名记忆已存在。
        """
        if self._path(name).exists():
            raise ValueError("Memory already exists")
        return self.update(name, content)

    def update(self, name, content):
        """原子化写入更新指定记忆内容，并递增版本号。

        Args:
            name: 记忆名称。
            content: 新内容。

        Returns:
            dict: 更新后的记忆条目。
        """
        atomic(self._path(name), content)
        self._refresh()
        return self.records[name].copy()

    def delete(self, name):
        """物理删除指定记忆文件并刷新清单。

        Args:
            name: 记忆名称。
        """
        self._path(name).unlink()
        self._refresh()

    def get(self, name):
        """获取指定名称的记忆条目数据。

        Args:
            name: 记忆名称。

        Returns:
            dict: 记忆数据字典。
        """
        self._ensure_fresh()
        return self.records[name].copy()

    def list(self):
        """列出当前所有持久化记忆条目列表。

        Returns:
            list[dict]: 记忆字典列表。
        """
        self._ensure_fresh()
        return [v.copy() for v in self.records.values()]

    def _ensure_fresh(self):
        """核对物理文件与内存缓存的哈希一致性，若检测到外部篡改或编辑则自动刷新。"""
        current = {}
        for path in sorted(self.root.glob("*.md")):
            reject_symlinks(path)
            current[path.stem] = sha(path.read_text(encoding="utf-8"))
        if current != {key: value["content_hash"] for key, value in self.records.items()}:
            self._refresh()

    def search(self, query, limit=5):
        """轻量词法匹配检索候选记忆列表。

        Args:
            query: 搜索关键词。
            limit: 最大候选条数。

        Returns:
            list[dict]: 排名排序后的候选记忆列表。
        """
        self._ensure_fresh()
        return [
            self.get(k)
            for k in rank(
                query, [(k, k + " " + v["content"]) for k, v in self.records.items()], limit
            )
        ]

    def _selector_done(self, task):
        """模型语义精排异步任务完成回调，安全清理后台引用并提取异常。

        Args:
            task: 已终结的异步任务。
        """
        self._background.discard(task)
        if not task.cancelled():
            # Timed-out work has no waiting caller to retrieve its exception.
            task.exception()

    @staticmethod
    async def _drain(tasks):
        """Let paid request settlement finish even if the owning waiter is cancelled again."""
        pending = asyncio.gather(*tasks, return_exceptions=True)
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return pending.result()

    def cancel_pending(self):
        """Immediately request cancellation; close() also waits for durable settlement."""
        tasks = tuple(task for task in self._background if not task.done())
        for task in tasks:
            task.cancel()
        return tasks

    async def close(self):
        """Drain every selector before the owning session writer or provider closes.

        Normal recall deadlines never wait for an uncooperative backend. Such
        tasks remain owned here until their adapter's bounded request finishes,
        or shutdown cancels and drains them. Their late result is never injected.
        """
        self._closed = True
        await self._drain(self.cancel_pending())

    async def recall(self, query, selector=None, timeout=8):
        """两阶段动态回忆：执行词法匹配与模型精选，返回拼装后的 XML 记忆块文本。

        Args:
            query: 检索查询词。
            selector: 可选的模型精选异步回调。
            timeout: 精选超时秒数上限（默认 8 秒）。

        Returns:
            str: 拼装好的 `<memory id="..." version="...">...</memory>` 纯文本字符串。
        """
        return "\n".join(
            item["wrapped"] for item in await self.recall_entries(query, selector, timeout)
        )

    async def recall_entries(self, query, selector=None, timeout=8):
        """Pin selection to the versions offered to the selector, with complete wrappers."""
        if self._closed:
            raise RuntimeError("MemoryStore is closed")
        selected = self.search(query)
        snapshot = {record["id"]: record for record in self.list()}
        if selector:
            task = asyncio.create_task(selector(query, list(snapshot.values())))
            self._background.add(task)
            task.add_done_callback(self._selector_done)
            try:
                done, _ = await asyncio.wait({task}, timeout=min(max(timeout, 0), 8))
            except asyncio.CancelledError:
                task.cancel()
                await self._drain((task,))
                raise
            if not done:
                task.cancel()
                return []
            ids = task.result()
            if self._closed:
                return []
            ids = ids if isinstance(ids, (list, tuple)) else []
            selected = [
                snapshot[k].copy()
                for k in dict.fromkeys(k for k in ids if isinstance(k, str) and k in snapshot)
            ]
        result = []
        total = 0
        for record in selected[:5]:
            # JSON encoding prevents identifiers from breaking the wrapper.
            opening = (
                "<memory id="
                + json.dumps(record["id"], ensure_ascii=False)
                + " version="
                + json.dumps(record["content_hash"])
                + ">"
            )
            closing = "</memory>"
            budget = 4096 - len((opening + closing).encode())
            if budget < 0:
                continue
            text = opening + record["content"].encode()[:budget].decode("utf-8", "ignore") + closing
            if total + len(text.encode()) + 1 > 60 * 1024:
                break
            result.append({**record, "wrapped": text})
            total += len(text.encode()) + 1
        return result

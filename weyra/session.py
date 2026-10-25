"""持久化会话事实存储、ACID 状态机与分支投影引擎 (Persistent Session Store & Event Sourcing Engine)。

【架构定位】
本模块是 Weyra 智能体架构的最底层持久化基础设施。作为整个系统的“单一事实来源（Single Source of Truth）”，
所有用户交互输入、模型请求、工具意图、工具执行观测、结构化压缩摘要及子智能体通信记录，
均作为全局唯一的不可变因果事件（Immutable Causal Events）持久化至项目级 SQLite 数据库中。

【设计思想与开源血统】
1. 深度继承 Pi Agent (packages/coding-agent & dg-ai-notes 第 10 章 会话管理 —— 对话的存储、恢复与分叉) 的 Session Tree 树形日志架构：
   - 存储介质与数据结构正交解耦：Pi Agent 在工程上清晰拆分了“存哪里”（介质）与“长什么样”（数据结构）。
     Weyra 继承其核心数据结构——基于唯一 `id` 与父引用 `parent` 拓扑链接的单调只追加树（Append-Only DAG），
     并将默认存储介质升级为兼具本地零运维与 ACID 强一致性的 SQLite WAL 模式。
   - 指针移动式分支与分叉 (Branching & Forking via Pointer Movement)：在真实会话中，交互并非线性排列。
     用户需要尝试多种方案、调试回退或让子智能体探索支线。Pi Agent 的破局点在于：回退与分叉绝不物理修改或删除历史节点，
     而是仅仅移动活跃叶子指针（`self.leaf = branch_from_id`）。旧分支的数据 100% 完好留存，历史永不丢失。
   - 沿父指针回溯重构 (Path Backtracking to Root)：当需要为大模型准备上下文时，系统通过 `chain()` / `project()` 从当前
     叶子节点 `self.leaf` 溯源向上追溯至根节点，动态线性化重构当前有效上下文链条，天然免疫多分支交叉污染。
2. 借鉴 DeepSeek-Harness (packages/typert/ & packages/storage/) 的大对象内容寻址与崩溃恢复规范：
   - 内容寻址 Blob 机制 (Content-Addressed Blobs)：针对系统提示词（System Prompt）、工具声明集（Tools Schema）等大对象，
     自动计算确定性 SHA-256 紧凑哈希（`digest`）并抽取至独立的 `blobs` 表中，事件日志中仅保留哈希引用键（如 `system_ref`），
     彻底根除主事件表与索引树因长文本膨胀导致的 I/O 劣化；
   - 崩溃自治恢复状态机 (Crash Recovery State Machine)：借鉴 DSH 的受控进程监督哲学，在系统重启或进程异常中断后，
     自动调用 `recover_interrupted` 扫描未闭合的工具调用意图并合成 unknown outcome 结果，防止下一轮推理陷入死锁或重复执行破坏性工具。
3. 生产级单写入者互斥锁与事务一致性隔离 (Single Writer Process Lock & ACID Transaction)：
   - 基于 POSIX 内核级 `fcntl.flock(LOCK_EX | LOCK_NB)` 对每个会话绑定独占 `.lock` 文件，严格保障同一会话单进程独占写入；
   - 独占事务一致性：`with session.transaction()` 开启 SQLite `BEGIN IMMEDIATE` 独占事务，嵌套故障自动标记 `_rollback_only`，
     保障内存状态缓存与磁盘数据绝对强一致。

【核心不变性与安全约束】
- 单调只追加约束 (Append-Only Invariant)：已落盘的会话事件绝对物理只读，系统底层严禁暴露任何针对历史事件的 UPDATE/DELETE 接口。
- 分支原子性保障：禁止在未闭合的工具调用批次中间开辟分支（`branch`）或派生分叉（`fork`），彻底防御孤立工具调用引发的 API 400 异常。
- 符号链接防御 (Symlink Attack Defense)：状态数据库文件、锁目录及 Blob 存储路径均严密校验真实物理路径，拦截路径逃逸攻击。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .contracts import Message, canonical, digest, new_id


class SessionError(RuntimeError):
    """会话事实存储、并发进程锁争用、事务回滚或数据完整性异常。"""
    pass


class SessionStore:
    """会话持久化与事件溯源存储引擎 (Session Store & Event Sourcing Engine)。

    【职责定位与设计模式】
    本类是领域驱动设计中的“事件溯源仓库（Event Sourcing Repository）”。
    负责 Weyra 智能体会话生命周期内的事件落盘、树形分支管理、大对象内容寻址以及跨进程独占锁协调。

    【并发模型与事务安全】
    - 进程级互斥：基于 `locks/<sha256>.lock` 的内核级排他文件锁 `fcntl.flock(LOCK_EX | LOCK_NB)`，确保全局单写入者；
    - 线程级安全：内部配备可重入锁 `threading.RLock()`，对外公开的方法均线程安全；
    - 事务一致性：`with session.transaction()` 开启 `BEGIN IMMEDIATE` 独占事务，嵌套异常自动触发回滚并恢复内存快照。

    Attributes:
        project_path (Path): 项目根目录绝对路径。
        path (Path): SQLite 数据库物理文件路径（位于 `.weyra/state.db`）。
        session_id (str): 当前活跃会话全局唯一标识符（最长 256 字符）。
        leaf (str | None): 当前会话在 Session Tree 中的活动叶子节点事件 ID。
        closed (bool): 数据库连接与锁句柄是否已安全关闭。
        failed (bool): 底层存储是否遭遇磁盘满或 I/O 损坏等不可恢复故障。
    """

    def __init__(self, path=None, project=".", *, session_id=None, _seed_events=None):
        """初始化会话事实存储引擎并建立排他性文件锁与 SQLite 数据库连接。

        【执行流程与防护机制】
        1. 路径沙箱校验：解析并规范化项目根目录，确保状态目录 `.weyra/` 严格隶属于项目，拦截符号链接提权攻击；
        2. 数据库连接与 PRAGMA 调优：开启 SQLite WAL 模式（Write-Ahead Logging），配置外键约束强校验，
           并将同步模式置为 FULL，确保断电或崩溃时数据绝对不丢失；
        3. 数据表拓扑自愈：调用 `_schema()` 初始化或校验 sessions, events, blobs, aliases, imports, child_inbox 结构；
        4. 单一写入者内核锁 (Single Writer Lock)：基于会话 ID 的 SHA-256 哈希创建独立 `.lock` 文件，
           使用 `fcntl.flock(LOCK_EX | LOCK_NB)` 获取非阻塞排他锁，杜绝多进程并发写入同一会话导致脑裂；
        5. 历史加载与平滑迁移：若存在遗留 JSONL 文件，安全解析其历史事件并落盘入库；否则新建根会话记录；
        6. 若传入 `_seed_events`，在同一事务中批量提交（常用于 Subagent fork 派生场景）。

        Args:
            path: 可选的会话物理路径（支持 `.jsonl` 遗留导入文件路径或 SQLite 库路径）。
            project: 项目根目录绝对路径或相对路径，默认为当前目录 "."。
            session_id: 可选的显式指定会话唯一标识符（最长 256 字符），若缺省则自动生成或沿用遗留标识。
            _seed_events: 内部种子事件序列，用于从父会话派生 fork 时的上下文初始化。

        Raises:
            SessionError: 当状态目录逃逸项目边界、锁文件为符号链接、会话已被其他写入者独占加锁、
                         或者遗留会话文件损坏时抛出。
        """
        self.project_path = Path(project).resolve()
        state = self.project_path / ".weyra"
        if not state.resolve().is_relative_to(self.project_path):
            raise SessionError("State directory escapes project")
        state.mkdir(parents=True, exist_ok=True)
        self.path = state / "state.db"
        if self.path.is_symlink():
            raise SessionError("State database must not be a symlink")
        self.failed = False
        self.closed = False
        self._lock = threading.RLock()
        self._depth = 0
        self._records = []
        self._by_id = {}
        self.leaf = None
        self._writer = None
        self._db = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=30
        )
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=FULL")
            self._schema()
            source = (
                Path(path).resolve() if path is not None and Path(path).suffix == ".jsonl" else None
            )
            alias = str(source) if source else None
            previous = (
                self._db.execute("SELECT session_id FROM aliases WHERE path=?", (alias,)).fetchone()
                if alias
                else None
            )
            imported = None
            if previous:
                if session_id is not None and session_id != previous[0]:
                    raise SessionError("Legacy path already belongs to another session")
                session_id = previous[0]
            elif source and source.exists():
                imported = self._read_legacy(source)
                ledger = self._db.execute(
                    "SELECT session_id FROM imports WHERE source_hash=?", (imported[2],)
                ).fetchone()
                if ledger:
                    if session_id is not None and session_id != ledger[0]:
                        raise SessionError("Legacy content already belongs to another session")
                    session_id = ledger[0]
                    imported = None
            if session_id is None:
                if path is not None and source is None and Path(path).resolve() == self.path:
                    rows = self._db.execute(
                        "SELECT session_id FROM sessions ORDER BY updated DESC"
                    ).fetchall()
                    if len(rows) != 1:
                        raise SessionError(
                            "Choose an explicit session_id when reopening a database"
                        )
                    session_id = rows[0][0]
                else:
                    session_id = (
                        source.stem if source else str(path) if path is not None else new_id()
                    )
            if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
                raise SessionError("Invalid session ID")
            self.session_id = session_id
            locks = state / "locks"
            if locks.is_symlink():
                raise SessionError("Session lock directory must not be a symlink")
            locks.mkdir(exist_ok=True)
            lock_path = locks / (hashlib.sha256(session_id.encode()).hexdigest() + ".lock")
            if lock_path.is_symlink():
                raise SessionError("Session lock must not be a symlink")
            self._writer = lock_path.open("a+b")
            try:
                fcntl.flock(self._writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SessionError("Session already has a writer") from None
            with self.transaction():
                existing = self._db.execute(
                    "SELECT project,leaf FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if existing:
                    if _seed_events is not None:
                        raise SessionError("Fork destination already exists")
                    if existing[0] != str(self.project_path):
                        raise SessionError("Session belongs to another project")
                    if imported:
                        raise SessionError("Import destination already exists")
                    self._load()
                else:
                    self._db.execute(
                        "INSERT INTO sessions(session_id,project) VALUES(?,?)",
                        (session_id, str(self.project_path)),
                    )
                    if imported:
                        records, blobs, source_hash = imported
                        if records[0]["data"].get("project") != str(self.project_path):
                            raise SessionError("Session belongs to another project")
                        for key, value in blobs.items():
                            self._db.execute(
                                "INSERT OR IGNORE INTO blobs(hash,data) VALUES(?,?)",
                                (key, canonical(value)),
                            )
                        for record in records:
                            self._insert(record)
                        self._db.execute(
                            "INSERT INTO imports(source_hash,session_id,source_path) VALUES(?,?,?)",
                            (source_hash, session_id, alias),
                        )
                        self._restore_inbox()
                    else:
                        self.append(
                            "session",
                            {
                                "format": 2,
                                "project": str(self.project_path),
                                "session_id": session_id,
                            },
                        )
                if _seed_events is not None:
                    self.append_many(_seed_events)
                if alias:
                    self._db.execute(
                        "INSERT OR IGNORE INTO aliases(path,session_id) VALUES(?,?)",
                        (alias, session_id),
                    )
        except BaseException:
            self._db.close()
            if self._writer:
                self._writer.close()
            self.closed = True
            raise

    def _schema(self):
        """初始化持久化存储的 6 张核心数据表与高性能因果查询索引。

        【数据表架构设计】
        1. sessions: 存储会话元信息（session_id, project, leaf 当前活动叶子节点, 创建与更新时间戳）；
        2. events: 核心不可变事件表（seq 自增物理序号, id 业务 UUID, parent 树形父节点指针, kind 类型, data 负载,
           以及 request_id, task_id, turn_id 因果追踪外键）；
        3. blobs: 内容寻址大对象表（hash SHA-256 主键, data 序列化文本），分离大对象防止主表膨胀；
        4. aliases: 文件系统路径与 session_id 映射别名表；
        5. imports: 遗留历史会话导入审计账本，记录导入源 SHA-256，防止同一历史被重复导入；
        6. child_inbox: 子智能体异步通信收件箱，支持两阶段确认（Two-Phase ACK）保证消息至少投递一次。
        """
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, project TEXT NOT NULL, leaf TEXT,
                created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                id TEXT NOT NULL, parent TEXT, kind TEXT NOT NULL, data TEXT NOT NULL,
                request_id TEXT, task_id TEXT, turn_id TEXT,
                UNIQUE(session_id,id),
                FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                FOREIGN KEY(session_id,parent) REFERENCES events(session_id,id)
            );
            CREATE INDEX IF NOT EXISTS events_kind ON events(session_id,kind,seq);
            CREATE INDEX IF NOT EXISTS events_request ON events(session_id,request_id);
            CREATE INDEX IF NOT EXISTS events_task ON events(session_id,task_id);
            CREATE INDEX IF NOT EXISTS events_turn ON events(session_id,turn_id);
            CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aliases (path TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id));
            CREATE TABLE IF NOT EXISTS imports (source_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), source_path TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS child_inbox (
                session_id TEXT NOT NULL REFERENCES sessions(session_id), task_id TEXT NOT NULL,
                result TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id,task_id)
            );
        """
        )

    @classmethod
    def list_sessions(cls, project):
        """在只读连接中安全扫描并列举指定项目下的所有历史会话元数据。

        【大厂并发安全规范】
        采用 SQLite URI 只读模式 (`?mode=ro`) 打开数据库，完全绕过写锁与排他锁争用，
        支持在高并发推理或后台轮询时无阻塞提取会话列表。

        Args:
            project: 项目根目录绝对路径或相对路径。

        Returns:
            list[dict]: 包含 session_id, project, leaf, created, updated, message_count 的会话字典列表，
                        按最后更新时间倒序排序。
        """
        path = Path(project).resolve() / ".weyra" / "state.db"
        if not path.exists():
            return []
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    """SELECT session_id, project, leaf, created, updated,
                              (SELECT count(1) FROM events WHERE events.session_id = sessions.session_id AND events.kind = 'message') AS message_count
                       FROM sessions ORDER BY updated DESC, session_id"""
                )
            ]

    @staticmethod
    def _read_legacy(source):
        """安全解析并校验遗留的纯文本 JSONL 会话历史文件。

        【血统连续性校验】
        逐行反序列化 JSONL 记录，严格校验每一行必须具备 `id`, `parent`, `kind`, `data` 四大核心字段，
        并执行 DAG 祖先合法性校验（第一条必须为根节点 parent=None，后续节点的 parent 必须在前序节点集中出现），
        防止损坏或恶意伪造的树形日志破坏数据完整性。

        Args:
            source: 遗留 `.jsonl` 文件的 Path 路径对象。

        Returns:
            tuple[list[dict], dict[str, Any], str]:
                (标准化事件列表, 关联的大对象 Blob 映射表, 原始文件的 SHA-256 哈希值)。

        Raises:
            SessionError: 当文件记录损坏、字段缺失、父节点断裂或 Blob 哈希篡改时抛出。
        """
        raw = source.read_bytes()
        records, seen, blobs = [], set(), {}
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if index == len(lines) - 1 and not line.endswith(b"\n"):
                    break
                raise SessionError(f"Corrupt session record {index + 1}") from None
            if (
                not isinstance(record, dict)
                or not {"id", "parent", "kind", "data"} <= record.keys()
            ):
                raise SessionError(f"Invalid session record {index + 1}")
            rid, parent = record["id"], record["parent"]
            if (
                not isinstance(rid, str)
                or not rid
                or not isinstance(record["kind"], str)
                or not isinstance(record["data"], dict)
            ):
                raise SessionError("Invalid session record")
            if (
                rid in seen
                or (not records and (parent is not None or record["kind"] != "session"))
                or (records and (not isinstance(parent, str) or parent not in seen))
            ):
                raise SessionError("Invalid session ancestry")
            seen.add(rid)
            records.append(record)
            if record["kind"] == "model_request":
                for key in ("system_ref", "tools_ref"):
                    ref = record["data"].get(key)
                    if ref is not None:
                        SessionStore._validate_ref(ref)
                        blob = source.parent / "blobs" / (ref + ".json")
                        if not blob.resolve().is_relative_to(source.parent):
                            raise SessionError("Request blob escapes session storage")
                        try:
                            value = json.loads(blob.read_text(encoding="utf-8"))
                        except (OSError, ValueError) as error:
                            raise SessionError("Missing or invalid request blob") from error
                        if digest(value) != ref:
                            raise SessionError("Request blob content mismatch")
                        blobs[ref] = value
        if not records:
            raise SessionError("Legacy session has no valid root")
        return records, blobs, hashlib.sha256(raw).hexdigest()

    def _load(self):
        """从 SQLite 数据库中全量装载当前会话的所有历史事件，重建内存高效检索索引。"""
        self._records = [
            {"id": rid, "parent": parent, "kind": kind, "data": json.loads(data)}
            for rid, parent, kind, data in self._db.execute(
                "SELECT id,parent,kind,data FROM events WHERE session_id=? ORDER BY seq",
                (self.session_id,),
            )
        ]
        self._by_id = {r["id"]: r for r in self._records}
        self.leaf = self._db.execute(
            "SELECT leaf FROM sessions WHERE session_id=?", (self.session_id,)
        ).fetchone()[0]

    @contextmanager
    def transaction(self):
        """开启单一写入者 ACID 事务上下文 (Single-Writer ACID Transaction)。

        【事务一致性与错误遏制保证】
        1. 采用 SQLite `BEGIN IMMEDIATE` 模式，在进入事务块之初立即获取数据库保留锁（Reserved Lock），
           防止并发冲突或写入时遭遇死锁；
        2. 内存状态与磁盘对齐：事务开启前对内存记录缓存 (`_records`, `_by_id`, `leaf`) 建立快照；
        3. 嵌套事务支持：支持同一线程内重入调用，内部任何嵌套抛错将原子标记 `_rollback_only = True`，
           确保无论外部调用方是否捕获内部异常，最外层事务均强制执行 ROLLBACK 并恢复内存快照；
        4. 硬件级故障标记：当捕获到磁盘满 (`SQLITE_FULL`)、I/O 损坏 (`SQLITE_IOERR` / `SQLITE_CORRUPT`)
           等严重底层错误时，立即标记 `self.failed = True`，永久拒绝后续写入以保护存储完整性。
        """
        with self._lock:
            if self.closed or self.failed:
                raise SessionError("Session is closed or its persistence has failed")
            outer = self._depth == 0
            if outer:
                self._db.execute("BEGIN IMMEDIATE")
                snapshot = (list(self._records), dict(self._by_id), self.leaf)
                self._rollback_only = False
            self._depth += 1
            try:
                yield self
                if outer:
                    if self._rollback_only:
                        raise SessionError("Nested transaction failed")
                    self._db.execute("COMMIT")
            except BaseException as error:
                self._rollback_only = True
                code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                if code in {
                    sqlite3.SQLITE_FULL,
                    sqlite3.SQLITE_IOERR,
                    sqlite3.SQLITE_CORRUPT,
                    sqlite3.SQLITE_NOTADB,
                    sqlite3.SQLITE_CANTOPEN,
                    sqlite3.SQLITE_READONLY,
                }:
                    self.failed = True
                if outer:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                    self._records, self._by_id, self.leaf = snapshot
                raise
            finally:
                self._depth -= 1

    def _insert(self, record):
        """执行底层 SQL 单事件持久化，推进 session 表的 leaf 指针与更新时间戳。"""
        data = record["data"]
        self._db.execute(
            "INSERT INTO events(session_id,id,parent,kind,data,request_id,task_id,turn_id) VALUES(?,?,?,?,?,?,?,?)",
            (
                self.session_id,
                record["id"],
                record["parent"],
                record["kind"],
                canonical(data),
                data.get(
                    "request_id",
                    data.get("id") if record["kind"] == "model_request" else None,
                ),
                data.get("task_id"),
                data.get(
                    "turn_id",
                    (data.get("id") if record["kind"] in {"turn_start", "turn_end"} else None),
                ),
            ),
        )
        self._db.execute(
            "UPDATE sessions SET leaf=?,updated=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE session_id=?",
            (record["id"], self.session_id),
        )
        self._records.append(record)
        self._by_id[record["id"]] = record
        self.leaf = record["id"]

    def append(self, kind: str, data: dict, checkpoint: bool = False) -> dict:
        """向会话提交追加一条全新的不可变持久化事件记录 (Append Immutable Event)。

        【Pi Agent 会话树生长与 DSH 大对象抽取机制】
        本方法是 Session Tree 节点递增的唯一合法入口（严格对齐 dg-ai-notes 第 10 章）。
        1. 每一个新生成的事件节点被分配唯一的 32 位十六进制 UUID，并自动绑定当前会话活动叶节点 `self.leaf` 作为其 `parent`，
           实现 Append-Only 树形拓扑的单调伸展；
        2. 结合 DeepSeek-Harness 内容寻址规范：对大对象（如 system prompt、tools 声明集）自动抽取为 SHA-256
           内容寻址 Blob 存储（`put_blob`），事件负载中仅保留不可变的哈希引用键（如 `system_ref`, `tools_ref`），
           彻底防止主事件表因模型大提示词发生物理行碎片与膨胀；
        3. 落盘成功后自动原子推进 `self.leaf = record['id']`，维持单调递增的因果时间序。

        Args:
            kind: 事件类型（如 'message', 'model_request', 'tool_intent', 'tool_result', 'turn_start', 'turn_end' 等）。
            data: 事件负载字典，包含业务数据与调用参数。
            checkpoint: 是否为关键检查点事件（如轮次结束、压缩完成）。

        Returns:
            dict: 完成持久化并赋予全局唯一 ID 与父节点引用的标准化事件字典。

        Raises:
            SessionError: 当数据库连接已关闭、事务被标记为仅回滚或写入冲突时抛出。
        """
        with self.transaction():
            data = json.loads(canonical(data))
            if kind == "model_request":
                for key in ("system", "tools"):
                    if key in data:
                        data[key + "_ref"] = self.put_blob(data.pop(key))
            record = {"id": new_id(), "parent": self.leaf, "kind": kind, "data": data}
            self._insert(record)
            return json.loads(canonical(record))

    def append_many(self, entries):
        """在单一原子事务中批量提交追加多条事件记录。

        【执行流程】
        在事务锁保护下，按序调用 `append` 方法追加每个条目。若任何一条失败，
        整个批次全部原子回滚，防止中间半提交状态破坏因果树形结构。

        Args:
            entries: 事件条目列表，元素可为字典（必须含 kind 和 data 键）或 (kind, data) 元组。

        Returns:
            list[dict]: 完成持久化落盘的标准化事件字典列表。
        """
        with self.transaction():
            return [
                (
                    self.append(item["kind"], item["data"])
                    if isinstance(item, dict)
                    else self.append(*item)
                )
                for item in entries
            ]

    def add_message(self, message: Message) -> dict:
        """记录并持久化一条标准化消息（角色包含 user, assistant, tool 等）。

        Args:
            message: 结构化 Message 领域实体对象。

        Returns:
            dict: 完成持久化的 'message' 类型事件字典。
        """
        return self.append("message", message.to_dict(), checkpoint=True)

    @property
    def records(self):
        """获取当前会话全量历史事件列表的深拷贝副本（只读防御）。

        Returns:
            list[dict]: 包含从创建以来产生的所有事件字典列表。
        """
        with self._lock:
            return json.loads(canonical(self._records))

    def export_jsonl(self, path=None):
        """将当前会话历史导出为符合 Pi 规范的诊断性标准 JSONL 文本。

        【设计说明】
        纯文本 JSONL 仅作为诊断分析、备份归档与外部调试工具使用，
        运行时的在线持久化严格由 SQLite WAL 数据库负责。

        Args:
            path: 可选的导出物理目标路径。若指定，则必须为一个不存在的新文件（禁止覆盖当前在线数据库）。

        Returns:
            str: 规范化的每行一个事件 JSON 对象的完整字符串。

        Raises:
            SessionError: 当目标导出路径试图覆盖正在运行的在线数据库时抛出。
        """
        text = "".join(canonical(record) + "\n" for record in self.records)
        if path is not None:
            target = Path(path).resolve()
            if target == self.path.resolve():
                raise SessionError("Cannot export over the live database")
            with target.open("x", encoding="utf-8") as output:
                output.write(text)
        return text

    def put_blob(self, value):
        """将大对象持久化至内容寻址 Blob 存储表，并返回其 SHA-256 紧凑哈希。

        【DeepSeek-Harness 内容寻址设计】
        通过 `digest(value)` 确定性计算按键升序排序的紧凑 JSON SHA-256 哈希值。
        采用 `INSERT OR IGNORE` 语义，天然支持跨轮次、跨事件的相同大对象全局去重。

        Args:
            value: 任意可序列化为 JSON 的大对象（字典、列表或字符串）。

        Returns:
            str: 64 位十六进制 SHA-256 内容寻址哈希摘要。
        """
        with self.transaction():
            key = digest(value)
            self._db.execute(
                "INSERT OR IGNORE INTO blobs(hash,data) VALUES(?,?)",
                (key, canonical(value)),
            )
            return key

    @staticmethod
    def _validate_ref(ref):
        """严格校验 Blob 哈希引用的合法性，防御 SQL 注入与路径穿越攻击。"""
        if (
            not isinstance(ref, str)
            or len(ref) != 64
            or any(c not in "0123456789abcdef" for c in ref)
        ):
            raise SessionError("Invalid request blob reference")

    def expand_request(self, record):
        """展开含有 Blob 引用的模型调用请求事件，反向内联恢复完整的大对象内容。

        【执行流程】
        若传入的 `model_request` 记录中包含 `system_ref` 或 `tools_ref`，
        根据哈希值从 `blobs` 表中检索原始 JSON 文本并比对校验内容哈希，
        重构包含完整 `system` 和 `tools` 的模型请求字典。

        Args:
            record: 含有 Blob 引用的事件记录字典。

        Returns:
            dict: 完成内联展开的完整请求字典。

        Raises:
            SessionError: 当引用哈希格式非法、Blob 缺失或内容指纹校验不匹配时抛出。
        """
        with self._lock:
            data = json.loads(canonical(record.get("data", record)))
            for key in ("system", "tools"):
                ref = data.pop(key + "_ref", None)
                if ref is not None:
                    self._validate_ref(ref)
                    row = self._db.execute("SELECT data FROM blobs WHERE hash=?", (ref,)).fetchone()
                    if not row:
                        raise SessionError("Missing request blob")
                    value = json.loads(row[0])
                    if digest(value) != ref:
                        raise SessionError("Request blob content mismatch")
                    data[key] = value
            return data

    def consume_input(self, item):
        """原子准入并消费一条用户输入队列项，建立 user 消息并标记已消费状态。

        【幂等消费保证】
        先在当前活跃因果链中扫描是否已存在该输入项 ID 对应的 `input_consumed` 记录；
        若已存在则直接幂等返回 None，防止网络抖动或界面重试导致重复提交同一输入。

        Args:
            item: 包含 id, text/content, source/kind 的输入项字典。

        Returns:
            dict | None: 生成并持久化的 'message' 记录字典，若已消费则返回 None。
        """
        with self.transaction():
            if any(
                r["kind"] == "input_consumed" and r["data"]["id"] == item["id"]
                for r in self.chain()
            ):
                return None
            message = Message(
                "user",
                item.get("text", item.get("content", "")),
                id=item["id"],
                source=item.get("source", item.get("kind", "user")),
            )
            record = next(
                (
                    r
                    for r in self.chain()
                    if r["kind"] == "message" and r["data"].get("id") == item["id"]
                ),
                None,
            )
            if record is None:
                record = self.add_message(message)
            self.append("input_consumed", {"id": item["id"]})
            return record

    def record_child_result(self, result):
        """将子智能体的异步执行交付物写入当前会话的收件箱（child_inbox）。

        【两阶段投递与幂等保障】
        基于 `(session_id, task_id)` 联合主键校验，若同一任务结果已存在则返回 False；
        否则在同一事务中写入 `child_result` 事件并插入 `child_inbox` 待确认表。

        Args:
            result: 子智能体交付的 ChildResult 实体对象或字典。

        Returns:
            bool: 是否成功新登记子任务结果（True 表示新写入，False 表示已存在）。
        """
        data = asdict(result) if is_dataclass(result) else json.loads(canonical(result))
        with self.transaction():
            if self._db.execute(
                "SELECT 1 FROM child_inbox WHERE session_id=? AND task_id=?",
                (self.session_id, data["task_id"]),
            ).fetchone():
                return False
            self.append("child_result", data)
            self._db.execute(
                "INSERT INTO child_inbox(session_id,task_id,result) VALUES(?,?,?)",
                (self.session_id, data["task_id"], canonical(data)),
            )
            return True

    def pending_child_results(self):
        """获取当前会话收件箱中所有未被主轮次确认（acknowledged=0）的子任务交付物列表。

        Returns:
            list[dict]: 待确认的子任务交付物字典列表，按入队顺序升序排列。
        """
        with self._lock:
            return [
                json.loads(row[0])
                for row in self._db.execute(
                    "SELECT result FROM child_inbox WHERE session_id=? AND acknowledged=0 ORDER BY rowid",
                    (self.session_id,),
                )
            ]

    def accept_child_result(self, task_id, message):
        """主智能体正式采纳子智能体结果：持久化为会话消息并原子标记两阶段确认。

        Args:
            task_id: 子任务唯一标识符。
            message: 由子任务产物格式化生成的标准化 Message 实体。

        Returns:
            bool: 采纳是否成功（若该任务不存在或已被确认过则返回 False）。
        """
        with self.transaction():
            row = self._db.execute(
                "SELECT acknowledged FROM child_inbox WHERE session_id=? AND task_id=?",
                (self.session_id, task_id),
            ).fetchone()
            if not row or row[0]:
                return False
            self.add_message(
                message if isinstance(message, Message) else Message.from_dict(message)
            )
            self.acknowledge_child_result(task_id)
            return True

    def acknowledge_child_result(self, task_id):
        """显式确认关闭指定子任务消息，将其标记为已确认（acknowledged=1）并追加审计事件。

        Args:
            task_id: 子任务唯一标识符。

        Returns:
            bool: 是否成功变更状态。
        """
        with self.transaction():
            changed = self._db.execute(
                "UPDATE child_inbox SET acknowledged=1 WHERE session_id=? AND task_id=? AND acknowledged=0",
                (self.session_id, task_id),
            ).rowcount
            if changed:
                self.append("child_result_ack", {"task_id": task_id})
            return bool(changed)

    def _restore_inbox(self):
        """在系统从外部导入历史时，从事件链条回放恢复 child_inbox 事实表的状态。"""
        for record in self.chain():
            data = record["data"]
            if record["kind"] == "child_result":
                self._db.execute(
                    "INSERT OR IGNORE INTO child_inbox(session_id,task_id,result) VALUES(?,?,?)",
                    (self.session_id, data["task_id"], canonical(data)),
                )
            elif record["kind"] == "child_result_ack":
                self._db.execute(
                    "UPDATE child_inbox SET acknowledged=1 WHERE session_id=? AND task_id=?",
                    (self.session_id, data["task_id"]),
                )

    def chain(self, leaf: str | None = None) -> list[dict]:
        """沿 DAG 父指针反向追溯至根节点，生成因果线性化事件链条 (Path Backtracking)。

        【Pi Agent 会话溯源算法】
        从指定或默认的活动叶节点 `cursor = leaf` 开始，反复读取 `record['parent']` 指针，
        直到追溯至根节点（parent=None）。最终将逆向采集的列表翻转为因果发生先后升序序列。
        这正是 Pi Agent 树形日志多分支并存时，为单一分支提取完整历史的标准拓扑重构算法。

        Args:
            leaf: 可选的目标叶节点 ID，默认使用当前活跃的最新 `self.leaf`。

        Returns:
            list[dict]: 按因果发生先后升序排列的完整事件字典列表。

        Raises:
            SessionError: 当父指针指向不存在的未知节点（祖先链路损坏）时抛出。
        """
        with self._lock:
            cursor = self.leaf if leaf is None else leaf
            records = []
            while cursor:
                if cursor not in self._by_id:
                    raise SessionError("Unknown session ancestry")
                record = self._by_id[cursor]
                records.append(record)
                cursor = record["parent"]
            return json.loads(canonical(list(reversed(records))))

    def project(self, leaf: str | None = None) -> list[Message]:
        """将不可变事件流动态投影为模型可见的线性 Message 消息序列 (State Projection)。

        【压缩与种子投影感知】
        1. 沿当前叶节点回溯提取事件链 `self.chain(leaf)`；
        2. 顺序投影其中的 'message' 记录；
        3. 若链条中途出现 `compaction`（结构化压缩摘要）或 `seed`（Fork 会话种子）事件，
           之前的所有历史消息被原子替换为压缩后或种子快照中的保留消息，实现上下文确定性收缩，
           保证提供给大模型的上下文紧凑且语法严格闭合。

        Args:
            leaf: 可选的目标叶节点 ID，默认使用当前最新 `self.leaf`。

        Returns:
            list[Message]: 可直接传递给下游大模型接口的结构化 Message 实体列表。
        """
        messages = []
        for record in self.chain(leaf):
            if record["kind"] == "message":
                messages.append(Message.from_dict(record["data"]))
            elif record["kind"] in {"compaction", "seed"}:
                messages = [Message.from_dict(item) for item in record["data"]["messages"]]
        return messages

    def completed_view(self) -> list[Message]:
        """获取最近一次已成功完成（turn_end）状态的上下文消息视图。

        Returns:
            list[Message]: 最近一个已完整闭合轮次的消息列表；若已被用户清空则返回空列表。
        """
        for record in reversed(self.chain()):
            if record["kind"] in {"seed", "compaction"} and record["data"].get("reason") == "user_clear":
                return []
            if record["kind"] == "turn_end":
                return self.project(record["id"])
        return []

    def latest(self, kind: str) -> dict | None:
        """从当前链条最新叶节点向前逆序查找指定类型的最新一条事件负载数据。

        Args:
            kind: 要查找的事件类型（如 'baseline', 'goal', 'context_measurement' 等）。

        Returns:
            dict | None: 找到的最新事件 payload 字典深拷贝，若不存在则返回 None。
        """
        value = next((r["data"] for r in reversed(self.chain()) if r["kind"] == kind), None)
        return json.loads(canonical(value)) if value is not None else None

    def branch(self, record_id: str) -> str:
        """在会话树的指定历史节点上开辟新分支 (Branch in Session Tree DAG)。

        【Pi Agent 会话树分支哲学与防 400 校验】
        1. 核心操作：移动活跃指针！将当前会话的 `self.leaf` 指向历史给定的 `record_id`，随后追加一条 'branch' 审计事件；
        2. 不变性保护：旧分支上的节点绝不被删除，历史数据 100% 完好留存，支持随时切换回旧思路；
        3. 原子批次保护 (Anti-400 Incomplete Tool Batch Protection)：
           检查从根节点到该分支点的消息投影，若存在尚未返回对应 ToolResult 的 Assistant ToolCalls，
           严禁在批次中间断头开辟分支，杜绝 API 400 校验异常。

        Args:
            record_id: 作为新分支起点的历史事件唯一标识符。

        Returns:
            str: 新生成并落盘的 'branch' 事件唯一 ID。

        Raises:
            SessionError: 当指定起点不存在或位于未闭合的工具调用批次中间时抛出。
        """
        with self.transaction():
            if record_id not in self._by_id:
                raise SessionError("Unknown branch point")
            pending = set()
            for message in self.project(record_id):
                pending.update(call.id for call in message.tool_calls)
                if message.tool_call_id:
                    pending.discard(message.tool_call_id)
            if pending:
                raise SessionError("Cannot branch inside an incomplete tool batch")
            self.leaf = record_id
            return self.append("branch", {"from": record_id}, checkpoint=True)["id"]

    def fork(self, path=None, *, session_id=None) -> SessionStore:
        """从当前会话投影深度复制并派生出全新的子会话存储 (Fork Session for Subagent)。

        【Pi Agent 派生思想与 DeepSeek-Harness 子任务隔离】
        派生子会话继承当前会话的消息序列快照（作为子会话首个 'seed' 事件写入）、提示词基线 baseline
        与目标 goal，但获得全新的独立会话 ID 与独立的内核排他文件锁，彻底实现子任务的上下文物理沙箱隔离。

        Args:
            path: 可选的派生会话目标路径。
            session_id: 可选的显式子会话 ID，若缺省则自动生成全新 UUID。

        Returns:
            SessionStore: 初始化就绪、具备独立写入锁的全新派生会话存储实例。

        Raises:
            SessionError: 当存在未完成的工具批次或目标会话 ID 已存在时抛出。
        """
        with self._lock:
            view = self.project()
            pending = {call.id for m in view for call in m.tool_calls}
            pending -= {m.tool_call_id for m in view if m.tool_call_id}
            if pending:
                raise SessionError("Cannot fork an incomplete tool batch")
            target_id = session_id or (
                Path(path).stem
                if path and Path(path).suffix == ".jsonl"
                else str(path)
                if path
                else new_id()
            )
            if self._db.execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (target_id,)
            ).fetchone():
                raise SessionError("Fork destination already exists")
            entries = [
                (
                    "seed",
                    {
                        "messages": [m.to_dict() for m in view],
                        "source_session": self.session_id,
                        "source_leaf": self.leaf,
                    },
                )
            ]
            for kind in ("baseline", "goal"):
                value = self.latest(kind)
                if value:
                    entries.append((kind, value))
            return SessionStore(path, self.project_path, session_id=target_id, _seed_events=entries)

    def recover_interrupted(self):
        """崩溃与突发中断自治恢复状态机 (Crash Recovery State Machine)。

        【DeepSeek-Harness 工业级防崩溃自愈规则】
        在进程意外终止、SIGKILL 强杀或断电重启后，自动调用本方法完成状态修复：
        1. 针对进程在工具执行期间被杀的情况：为所有已发出 tool_intent 但缺失 tool_result 的调用，
           合成带未知结果标记的工具消息（'Outcome unknown after process interruption; do not automatically retry'），
           防止模型在下一轮推理中陷入断头调用引发的 400 异常或盲目重试非幂等操作；
        2. 针对未正常闭合的活跃轮次：自动追加 `turn_end(state='cancelled', reason='process_interrupted')`；
        3. 针对进行中的子智能体任务：标记其为 cancelled 状态，释放父级会话的等待栅栏。
        """
        active = {}
        intents = {}
        results = {}
        child_tasks = {}
        views = {}
        for record in self.chain():
            data = record["data"]
            if record["kind"] == "turn_start":
                turn_id = data.get("turn_id", data.get("id"))
                if turn_id:
                    active[turn_id] = data
            elif record["kind"] == "turn_end":
                active.pop(data.get("turn_id", data.get("id")), None)
            elif record["kind"] == "tool_intent":
                intents[data.get("call_id", data.get("id"))] = data
            elif record["kind"] == "tool_result":
                intents.pop(data.get("call_id"), None)
                results[data.get("call_id")] = data
            elif record["kind"] == "tool_result_view":
                views[data["call_id"]] = data["content"]
            elif record["kind"] == "child_task":
                child_tasks[data["task_id"]] = data
            elif record["kind"] == "child_result":
                child_tasks.pop(data["task_id"], None)
        existing = {m.tool_call_id for m in self.project() if m.tool_call_id}
        for message in list(self.project()):
            for call in message.tool_calls:
                if call.id not in existing:
                    result = results.get(call.id)
                    self.add_message(
                        Message(
                            "tool",
                            (
                                views.get(call.id, result.get("content", "Outcome unknown"))
                                if result
                                else "Outcome unknown after process interruption; do not automatically retry."
                            ),
                            source="recovery",
                            tool_call_id=call.id,
                            name=call.name,
                            provider_data={
                                "tool_status": (
                                    result.get("status", "unknown") if result else "unknown"
                                )
                            },
                        )
                    )
                    if result is None:
                        self.append(
                            "tool_result",
                            {
                                "call_id": call.id,
                                "name": call.name,
                                "status": "unknown",
                                "started": call.id in intents,
                                "effect": "unknown",
                            },
                            True,
                        )
        for turn_id in active:
            self.append(
                "turn_end",
                {"turn_id": turn_id, "state": "cancelled", "reason": "process_interrupted"},
                True,
            )
        for task_id in child_tasks:
            self.record_child_result(
                {
                    "task_id": task_id,
                    "state": "cancelled",
                    "summary": "",
                    "evidence": [],
                    "artifacts": [],
                    "unresolved": ["process_interrupted"],
                    "usage": 0,
                }
            )

    def checkpoint(self):
        """触发 SQLite WAL 被动检查点（PASSIVE Checkpoint），将 WAL 预写日志刷入主数据库文件。"""
        with self._lock:
            if self.closed:
                raise SessionError("Session is closed")
            if not self._depth:
                self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def close(self):
        """安全释放会话存储：执行最终 WAL 检查点落盘、关闭数据库连接并释放内核文件排他锁。

        Raises:
            SessionError: 当在活动未决事务中间尝试关闭会话时抛出。
        """
        with self._lock:
            if self.closed:
                return
            if self._depth:
                raise SessionError("Cannot close during a transaction")
            try:
                if not self.failed:
                    self.checkpoint()
            finally:
                self._db.close()
                if self._writer:
                    fcntl.flock(self._writer.fileno(), fcntl.LOCK_UN)
                    self._writer.close()
                self.closed = True

"""持久化物理存储模式版本定义 (Durable Storage Schema Version)。

【架构定位】(Architectural Role)
    定义由读写器（Reader/Writer）统一共享的物理数据库模式版本号，
    位于底层存储基础设施层，作为 SQLite 表结构与数据一致性演进的静态门禁。

【开源设计思想与架构血统】(Design Pedigree)
    借鉴 DeepSeek-Harness 的单调递增模式版本控制思想（Monotonic Schema Versioning）：
    当持久化表结构、索引或核心列布局发生破坏性变更时单调递增，杜绝跨版本读写冲突。

【核心不变性与安全约束】(Invariants & Safety)
    - 单调递增：版本号只能单调向上演进，严禁向后回退；
    - 全局唯一真实源：所有 SessionStore、迁移工具与投影读取器均以本模块的 SCHEMA_VERSION 为准。
"""

SCHEMA_VERSION = 2

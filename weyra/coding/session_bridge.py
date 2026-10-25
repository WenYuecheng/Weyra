"""会话存储与上下文投影解耦适配器 (Context-Session Bridge Adapter)。

【架构定位与设计哲学】
本模块遵循适配器模式（Adapter Pattern），在底层持久化会话存储（SessionStore）与
高层上下文管理器（ContextManager）之间建立轻量隔离桥梁。
使上下文投影逻辑（`project`）在未来演进支持视图过滤、动态脱敏或虚拟分支重放时，
无需直接修改底层的 SQLite WAL 事件存储实现。
"""

from __future__ import annotations


class ContextSessionBridge:
    """会话存储适配桥接器 (Context-Session Bridge Adapter)。

    代理底层存储操作并隔离上下文投影边界。
    """

    def __init__(self, store):
        """初始化会话桥接器 (Initialize ContextSessionBridge)。

        Args:
            store: 底层持久化会话存储实例 (SessionStore)。
        """
        self.store = store

    def project(self, *args, **kwargs):
        """执行会话历史消息线性投影 (Project Session Message History)。

        Args:
            *args: 传递给底层 SessionStore.project 的位置参数。
            **kwargs: 传递给底层 SessionStore.project 的关键字参数。

        Returns:
            list[Message]: 线性化投影后的消息列表。
        """
        return self.store.project(*args, **kwargs)

    def __getattr__(self, name):
        """透明转发底层 SessionStore 的其他属性与方法 (Delegate to SessionStore)。

        Args:
            name: 属性名。

        Returns:
            Any: 底层 SessionStore 对应的属性或方法。
        """
        return getattr(self.store, name)

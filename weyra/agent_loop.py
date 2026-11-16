"""历史生命周期模块兼容性导入别名 (Legacy AgentLoop Alias)。

【架构定位】
保持旧版模块路径 `weyra.agent_loop` 的导入兼容性，导出 `CodingLifecycle as AgentLoop`。
"""
from weyra.coding.lifecycle import CodingLifecycle as AgentLoop

__all__ = ["AgentLoop"]

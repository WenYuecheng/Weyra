"""历史兼容性模块别名 (Compatibility Alias for Agent)。

【架构定位】
保持旧版模块路径 `weyra.agent` 的导入兼容性，完整重定向至 `weyra.coding.agent`。
"""
import sys
from weyra.coding import agent as _implementation
sys.modules[__name__] = _implementation

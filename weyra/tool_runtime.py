"""历史工具运行时模块兼容性别名 (Legacy Tool Runtime Alias)。

【架构定位】
保持旧版模块路径 `weyra.tool_runtime` 的导入兼容性，完整重定向至 `weyra.core.tools`。
"""
import sys
from weyra.core import tools as _implementation
from weyra.tools import install_builtin_tools
_implementation.install_builtin_tools = install_builtin_tools
sys.modules[__name__] = _implementation

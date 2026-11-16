"""历史上下文模块兼容性别名 (Legacy Context Module Alias)。

【架构定位】
保持旧版模块路径 `weyra.context` 的导入兼容性，完整重定向至 `weyra.core.context`。
"""
import sys
from weyra.core import context as _implementation
sys.modules[__name__] = _implementation

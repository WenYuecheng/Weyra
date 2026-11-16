"""历史运行时状态模块兼容性别名 (Legacy Runtime State Alias)。

【架构定位】
保持旧版模块路径 `weyra.runtime_state` 的导入兼容性，完整重定向至 `weyra.core.state`。
"""
import sys
from weyra.core import state as _implementation
sys.modules[__name__] = _implementation

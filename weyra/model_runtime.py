"""历史模型执行运行时协调器兼容性别名 (Legacy Model Runtime Alias)。

【架构定位】
保持旧版模块路径 `weyra.model_runtime` 的导入兼容性，完整重定向至 `weyra.core.model_runtime`。
"""
import sys
from .core import model_runtime as _implementation
sys.modules[__name__] = _implementation

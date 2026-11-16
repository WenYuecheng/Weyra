"""历史厂商中立模型目录模块兼容性别名 (Legacy Model Catalog Alias)。

【架构定位】
保持旧版模块路径 `weyra.model_catalog` 的导入兼容性，完整重定向至 `weyra.llm.catalog`。
"""
import sys
from .llm import catalog as _implementation
sys.modules[__name__] = _implementation

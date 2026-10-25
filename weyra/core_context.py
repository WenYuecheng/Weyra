"""基准评测上下文治理模块的源码环境兼容转发入口（benchmarks.core_context 代理）。"""
import sys
from ._benchmark_compat import load, run


if __name__ == "__main__":
    run("core_context")
else:
    sys.modules[__name__] = load("core_context")

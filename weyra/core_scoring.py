"""综合评分统计与指标计算模块的源码环境兼容转发入口（benchmarks.core_scoring 代理）。"""
import sys
from ._benchmark_compat import load, run


if __name__ == "__main__":
    run("core_scoring")
else:
    sys.modules[__name__] = load("core_scoring")

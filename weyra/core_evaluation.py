"""系统核心能力基准评测运行器的源码环境兼容转发入口（benchmarks.core_evaluation 代理）。"""
import sys
from ._benchmark_compat import load, run


if __name__ == "__main__":
    run("core_evaluation")
else:
    sys.modules[__name__] = load("core_evaluation")

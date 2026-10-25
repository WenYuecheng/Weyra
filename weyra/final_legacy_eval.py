"""历史基准用例兼容回归评测模块的源码环境兼容转发入口（benchmarks.final_legacy_eval 代理）。"""
import sys
from ._benchmark_compat import load, run


if __name__ == "__main__":
    run("final_legacy_eval")
else:
    sys.modules[__name__] = load("final_legacy_eval")

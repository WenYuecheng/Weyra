"""终验全量套件综合评测模块的源码环境兼容转发入口（benchmarks.finish_evaluation 代理）。"""
import sys
from ._benchmark_compat import load, run


if __name__ == "__main__":
    run("finish_evaluation")
else:
    sys.modules[__name__] = load("finish_evaluation")

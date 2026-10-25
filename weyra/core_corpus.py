"""基准评测核心语料集模块的源码环境兼容转发入口（benchmarks.core_corpus 代理）。"""
import sys
from ._benchmark_compat import load, run


if __name__ == "__main__":
    run("core_corpus")
else:
    sys.modules[__name__] = load("core_corpus")

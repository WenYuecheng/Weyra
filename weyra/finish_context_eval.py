"""终验上下文压缩与长文本治理评测模块的源码环境兼容转发入口（benchmarks.finish_context_eval 代理）。"""
import sys
from ._benchmark_compat import load, run


if __name__ == "__main__":
    run("finish_context_eval")
else:
    sys.modules[__name__] = load("finish_context_eval")

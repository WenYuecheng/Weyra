"""交付标准端到端综合评估模块的源码环境兼容转发入口（benchmarks.final_delivery_eval 代理）。"""
import sys
from ._benchmark_compat import load, run


if __name__ == "__main__":
    run("final_delivery_eval")
else:
    sys.modules[__name__] = load("final_delivery_eval")

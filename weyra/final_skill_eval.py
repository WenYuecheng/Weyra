"""技能自进化与无退化门禁评测模块的源码环境兼容转发入口（benchmarks.final_skill_eval 代理）。"""
import sys
from ._benchmark_compat import load, run


if __name__ == "__main__":
    run("final_skill_eval")
else:
    sys.modules[__name__] = load("final_skill_eval")

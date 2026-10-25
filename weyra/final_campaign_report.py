"""战役级评测报告汇总结算模块的源码环境兼容转发入口（benchmarks.final_campaign_report 代理）。"""
import sys
from ._benchmark_compat import load, run


if __name__ == "__main__":
    run("final_campaign_report")
else:
    sys.modules[__name__] = load("final_campaign_report")

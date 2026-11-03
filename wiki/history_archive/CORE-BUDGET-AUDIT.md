# 最终独立预算审计

主报告：[CORE-EVALUATION.md](CORE-EVALUATION.md)。本审计在 worker 66013 已退出、`finalization-complete.json` 确认完成后，只读复算共享根账本及全部请求证据，不修改活账本。

最终根账本调用数2468，settlement数2456；12个差额是恢复时已保守吸收、没有伪造settlement的失联请求。真正未知用量settlement为86个，全部找到对应原始请求，无重复、无遗漏。仅 `cost_known=false` 而 input/output 已知的请求不算未知用量。根账本unknown_calls为98，即86个未知settlement加12个已吸收请求。

账面费用48.1510343288931元。历史未知用量按完整输入上界计算的总补差1.6728605678182元，其中1.3599645732131元已在恢复时应用；独立审计只再加未应用的0.3128959946051元。最终保守费用 **48.4639303234982元**，低于50元上限，余 **1.5360696765018元**。这不是提供方发票或精确实际扣费声明。

最终pending为0。三批请求数据库中，排除恢复时已吸收的12个request ID后，没有新的未写model_usage请求；最终收尾没有再吸收新的orphan pending，因此无需额外遗漏补差。12个原失联请求的完整输入上界1.2222874277202元，已经计入账面：原held1.1022501263360元由Budget.attach吸收，再通过明确accounting_adjustment追加正差0.1200373013842元。其原checkpoint、SQLite备份、原始请求及独立计算保存在 `.weyra/core-budget-recovery-20261018-v1`。恢复幂等，不重放请求、不改调用数、不伪造settlement，原始事件轨迹保留。

本轮恢复后实际使用的是动态49元保守总额guard，worker PID66013；早期45元guard没有重新启动。动态总额包括账面、尚未应用的历史未知补差和pending完整输入上界。恢复期间的旧45元说明只属于早期运行，不代表最终实际停线。

计算方法与当前运行时一致：完整system/messages/tools的canonical UTF-8字节数，加256基础封装余量、每条消息/工具32，与模型配置容量取大；输入采用input/cache最高费率，输出采用请求的输出预留。只累计新上界高于原settlement的正差；已应用的历史补差扣除，已吸收的pending request ID不再加价。三批协议的对应费率一致。

证据：`CORE-BUDGET-AUDIT.json` 包含逐项未知请求和补差；`CORE-PENDING-BOUND.json` 包含12个已吸收request ID及最终零pending核对。最终账本查询没有发现新的未匹配未知请求。

可复跑入口（只读账本，覆盖独立审计JSON）：

```text
python .weyra/core-budget-audit.py
python .weyra/core-pending-bound.py
```

脚本API分别为 `audit(project)` 与 `audit_pending(project)`，可通过 `runpy.run_path` 取得。本次最终审计未启动guard、未发送信号、未调用模型，也未修改预算checkpoint。

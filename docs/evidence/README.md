# 公开评测证据

这些文件从现有实验产物中提取，随仓库公开，便于检查 README 中的主要成绩。

| 文件 | 内容 |
| --- | --- |
| [polyglot-64-summary.json](polyglot-64-summary.json) | 64 题任务状态、累计通过曲线、分语言成绩与费用估算 |
| [polyglot-64-protocol.json](polyglot-64-protocol.json) | 协议版本、任务 ID 和输入哈希、输出与资源限制、主备模型配置 |
| [skill-localization-summary.json](skill-localization-summary.json) | 48 对技能定位结果与按独立任务聚类的统计摘要 |

每份摘要保留原始产物相对名称及 SHA-256，用于标识本次提取来源。导出时去除了本机绝对路径、服务端地址和逐请求文本；未修改任务结果或把估算账本改成实扣账单。完整请求轨迹仍保存在本地，公开摘要支持核对统计与协议，逐请求独立复核需要对应原始轨迹。

Polyglot 累计实验包含多个阶段：保留已有 29 题通过结果，再对 35 题继续执行，新增通过 21 题。具体运行与结果口径见[评测说明](../BENCHMARKS.md)。

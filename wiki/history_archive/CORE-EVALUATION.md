# Weyra 核心评测报告 v1

生成时间：2026-10-18T19:36:08+08:00
 
## 状态与证据边界

正式计划822单元，已记录552单元；另有0个开发试跑单元。
本批不混入此前试跑、预检和停止批次的成绩；它们另行留档，费用仍包含在同一根账本。
根账本保守费用：48.1510343288931元；物理请求：2468；未知用量：98。新增上限50元，实际平台账单未知。

包含尚未入账的未知用量全文上界补差后，独立保守复核合计 **48.4639303234982元**，无待返回请求。 [费用复核](CORE-BUDGET-AUDIT.json)。

本轮尚不能证明最初的长期自演化与长任务收益目标已经达成。工程可靠性改进已有回归证据；核心实验是否覆盖完整应以下列固定分母和未启动清单为准。

本轮为受控机制实验与公开题能力检查。维护单元成功只表示执行完成；定位为解析器探针；结构化任务检查可观察规则和事实。未把这些数字当通用写作质量、论文复现成绩或多厂商稳定性。

## 固定分母结果

| 实验 | 变体 | 固定计划数 | 记录数 | 确认成功 | 失败 | 错误/超时 | 待复核 | 中断/预算停止 | 未启动 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| compression | dsh_checkpoint | 36 | 11 | 5 | 4 | 0 | 0 | 2 | 25 |
| compression | dsh_no_prune | 36 | 10 | 4 | 4 | 1 | 0 | 1 | 26 |
| compression | none | 36 | 11 | 7 | 4 | 0 | 0 | 0 | 25 |
| compression | single_json | 36 | 11 | 1 | 3 | 5 | 0 | 2 | 25 |
| compression | three_parallel | 36 | 11 | 0 | 0 | 7 | 0 | 4 | 25 |
| localization | new | 60 | 60 | 54 | 6 | 0 | 0 | 0 | 0 |
| localization | old | 60 | 60 | 34 | 26 | 0 | 0 | 0 | 0 |
| maintenance | experience_bounded | 18 | 16 | 13 | 0 | 1 | 0 | 2 | 2 |
| maintenance | experience_full | 18 | 14 | 10 | 0 | 0 | 0 | 4 | 4 |
| maintenance | no_experience_bounded | 18 | 15 | 14 | 0 | 0 | 0 | 1 | 3 |
| maintenance | no_experience_full | 18 | 15 | 13 | 0 | 0 | 0 | 2 | 3 |
| progressive | new_new | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 18 |
| progressive | old_new | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 18 |
| progressive | old_old | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 18 |
| public | dsh_configured | 36 | 36 | 0 | 0 | 10 | 26 | 0 | 0 |
| public | none | 36 | 36 | 0 | 0 | 10 | 25 | 1 | 0 |
| skill_test | experience_bounded | 54 | 48 | 37 | 10 | 0 | 0 | 1 | 6 |
| skill_test | experience_full | 54 | 36 | 28 | 7 | 0 | 0 | 1 | 18 |
| skill_test | frozen_old | 54 | 36 | 27 | 9 | 0 | 0 | 0 | 18 |
| skill_test | no_experience_bounded | 54 | 45 | 35 | 10 | 0 | 0 | 0 | 9 |
| skill_test | no_experience_full | 54 | 45 | 29 | 15 | 0 | 0 | 1 | 9 |
| skill_test | no_skill | 54 | 36 | 28 | 8 | 0 | 0 | 0 | 18 |

以上为冻结评分器原始结果，未覆盖或追改。另有7条原始fail因题面没有约定字符串词汇而被独立有效性审计标记待复核；它们不能作为已确认语义错误。审计只降格争议失败，不自动提升成功。
[评分有效性审计](core-scoring-validity-audit-ee91eaa63f9281a2.json)。原始失败率和涉及这些题目的差值只供排查，不能证明记忆丢失或行为退化。

## 费用、用量与时间

| 实验/变体 | 记录单元费用上界/元 | 结算请求 | 有记录输入token | 有记录输出token | 未知用量记录 | 单元耗时合计/秒 |
|---|---:|---:|---:|---:|---:|---:|
| compression/dsh_checkpoint | 2.723719 | 112 | 364893 | 134816 | 4 | 1900.6 |
| compression/dsh_no_prune | 3.697739 | 141 | 461239 | 205714 | 1 | 2622.7 |
| compression/none | 0.842218 | 11 | 235089 | 271 | 0 | 153.2 |
| compression/single_json | 5.624806 | 130 | 859124 | 373271 | 5 | 2655.8 |
| compression/three_parallel | 15.224821 | 363 | 2758095 | 834723 | 42 | 2447.4 |
| localization/new | 0.000000 | 0 | 0 | 0 | 0 | 0.0 |
| localization/old | 0.000000 | 0 | 0 | 0 | 0 | 0.0 |
| maintenance/experience_bounded | 0.492217 | 167 | 78324 | 8719 | 3 | 2216.7 |
| maintenance/experience_full | 0.284663 | 160 | 71893 | 7815 | 2 | 2055.0 |
| maintenance/no_experience_bounded | 0.446004 | 170 | 61679 | 7985 | 2 | 2432.8 |
| maintenance/no_experience_full | 0.504309 | 185 | 61879 | 8000 | 5 | 2518.8 |
| progressive/new_new | 0.000000 | 0 | 0 | 0 | 0 | 0.0 |
| progressive/old_new | 0.000000 | 0 | 0 | 0 | 0 | 0.0 |
| progressive/old_old | 0.000000 | 0 | 0 | 0 | 0 | 0.0 |
| public/dsh_configured | 0.547784 | 36 | 5487 | 49315 | 0 | 650.0 |
| public/none | 0.509718 | 35 | 5067 | 45901 | 1 | 636.1 |
| skill_test/experience_bounded | 0.043025 | 47 | 8568 | 1161 | 1 | 669.4 |
| skill_test/experience_full | 0.034442 | 35 | 7136 | 837 | 1 | 402.7 |
| skill_test/frozen_old | 0.032682 | 36 | 6292 | 954 | 0 | 533.6 |
| skill_test/no_experience_bounded | 0.040989 | 45 | 8172 | 1103 | 0 | 646.9 |
| skill_test/no_experience_full | 0.044160 | 44 | 9225 | 1048 | 1 | 572.0 |
| skill_test/no_skill | 0.098427 | 37 | 5614 | 833 | 1 | 550.5 |

单元费用不含被中断后未形成结果的调用，不能相加替代根账本总额。token仅汇总实际轨迹，缺失用量不当零费用；耗时包含排队且并发重叠，不当总墙钟时间或公平延迟排名。
[逐单元资源审计](core-resource-usage.json)。各批次试跑与失败费用均包含在根账本，旧版本预留偏低的未知用量补差另见最终预算审计。

## 技能定位

- old：正确 34/60；错误目标 18；正确弃权 6。
- new：正确 54/60；错误目标 6；正确弃权 14。

独立事后补充：另冻结60个不同输入，旧版23/60、新版59/60。全部错误保留，未据结果改题；该结果与v1分开，仍只是小型声明别名库中的功能路径对比。
[补充逐题结果](core-localization-supplement-v2.json) · [范围说明](CORE-LOCALIZATION-SUPPLEMENT-V2.md)

重复创建未在解析器探针中实际执行，因此不填一个推断的“重复创建率”；生产路径的拒绝创建行为由回归测试单独验证。

## 技能维护与实际加载

- experience_bounded：更新/拒绝动作 {'rejected': 13, 'updated': 3, 'discarded': 10}。
- experience_full：更新/拒绝动作 {'updated': 3, 'rejected': 17}。
- no_experience_bounded：更新/拒绝动作 {'rejected': 16, 'discarded': 7, 'updated': 5}。
- no_experience_full：更新/拒绝动作 {'rejected': 22, 'updated': 4}。
- 按需加载 old_old：实际skill工具成功加载 0/0。
- 按需加载 old_new：实际skill工具成功加载 0/0。
- 按需加载 new_new：实际skill工具成功加载 0/0。

门禁逐次重新生成旧版与候选答案；严格增加成功数且硬失败不增加才接纳。拒绝保持旧版，最终测试不回流维护。维护发生错误的实验保留实际生效技能哈希和版本，不能解释成“新技能已发布”。

## 上下文机制覆盖

| 变体 | 固定/压力 | fit/overflow | 记录数 | 成功折叠总次数 | 原始记录保持 | 容量/压缩错误 |
|---|---|---|---:|---:|---:|---:|
| dsh_checkpoint | fixed | fit | 3 | 3 | 3 | 0 |
| dsh_checkpoint | fixed | overflow | 3 | 2 | 2 | 0 |
| dsh_checkpoint | pressure | fit | 2 | 5 | 2 | 0 |
| dsh_checkpoint | pressure | overflow | 3 | 19 | 2 | 0 |
| dsh_no_prune | fixed | fit | 3 | 3 | 3 | 0 |
| dsh_no_prune | fixed | overflow | 2 | 1 | 1 | 0 |
| dsh_no_prune | pressure | fit | 2 | 5 | 2 | 0 |
| dsh_no_prune | pressure | overflow | 3 | 19 | 2 | 0 |
| none | fixed | fit | 3 | 0 | 3 | 0 |
| none | fixed | overflow | 3 | 0 | 3 | 0 |
| none | pressure | fit | 2 | 0 | 2 | 0 |
| none | pressure | overflow | 3 | 0 | 3 | 0 |
| single_json | fixed | fit | 3 | 2 | 3 | 1 |
| single_json | fixed | overflow | 3 | 1 | 3 | 3 |
| single_json | pressure | fit | 2 | 3 | 1 | 0 |
| single_json | pressure | overflow | 3 | 12 | 1 | 0 |
| three_parallel | fixed | fit | 3 | 0 | 3 | 3 |
| three_parallel | fixed | overflow | 3 | 0 | 2 | 2 |
| three_parallel | pressure | fit | 2 | 0 | 0 | 0 |
| three_parallel | pressure | overflow | 3 | 0 | 0 | 0 |

fit/overflow是按源历史构造的压力层次；实际是否溢出以服务端结果为准。固定触发与生产压力触发分列。工具历史为预先构造的合法调用/结果记录，非付费现场执行的完整长任务；相关结论仅适用于状态保持。

## 成对比较与不确定性

下表差值为新组减旧组的原始确认成功率；按任务族聚类bootstrap，2000次重采样。重复运行未当作独立题目。待复核按未确认成功计，不能解释为语义错误；中断及未覆盖重复造成选择性覆盖，本表不构成完整消融结论。小样本区间仅描述本任务集。

| 实验 | 旧→新 | 触发/层次 | 成对单元 | 聚类数 | 差值 | 95%区间 |
|---|---|---|---:|---:|---:|---|
| skill_test | frozen_old→experience_bounded | all/all | 36 | 6 | 0.02777777777777778 | [-0.13888888888888887, 0.13888888888888887] |
| skill_test | no_experience_bounded→experience_bounded | all/all | 45 | 6 | 0.0 | [-0.05555555555555555, 0.05555555555555555] |
| skill_test | no_experience_full→experience_full | all/all | 36 | 6 | 0.11111111111111112 | [-0.027777777777777776, 0.2222222222222222] |
| compression | none→dsh_checkpoint | all/all | 11 | 6 | -0.25 | [-0.5833333333333334, 0.0] |
| compression | dsh_checkpoint→three_parallel | all/all | 11 | 6 | -0.4166666666666667 | [-0.75, -0.08333333333333333] |
| localization | old→new | all/all | 34 | 6 | 0.32777777777777783 | [0.1722222222222222, 0.49444444444444446] |
| compression | none→dsh_checkpoint | fixed/fit | 3 | 3 | 0.0 | [0.0, 0.0] |
| compression | none→dsh_checkpoint | fixed/overflow | 3 | 3 | -0.3333333333333333 | [-1.0, 0.0] |
| compression | none→dsh_checkpoint | pressure/fit | 2 | 2 | 0.0 | [0.0, 0.0] |
| compression | none→dsh_checkpoint | pressure/overflow | 3 | 3 | -0.3333333333333333 | [-1.0, 0.0] |

[逐题、逐重复成对差异及未执行侧](core-paired-details.json)保留原状态、硬约束差、费用、用量与耗时；尚未启动的一侧不计算差值。

经验＋有界修改对冻结旧技能：现有36对的任务族平均差值为2.78个百分点，描述性95%区间[-13.89, 13.89]个百分点。覆盖不齐且样本少，现有证据不足以证明稳定改善。

当前压缩结果主要暴露执行可靠性问题：单JSON有摘要截断和最低折叠次数错误，三并行有格式契约冲突、重试放大与中断；原始任务失败还包含未约定词汇的评分争议。应先修复并用新开发集验证，不能把这些数字直接解释为三层记忆原理无效或DSH已带来收益。

## 公开题与待复核

题库：BearCode已有GAIA165题与HLE500题。按稳定ID审核题面和当前能力，选择12道自包含HLE数学题；全部审核决策及源记录哈希见公开题审核表。无需图片的题目可能在原库标为MM，此处按完整题面可解性审核，不声称覆盖多模态。
公开题没有真实先前交互，两组都仅作外部能力检查，**不作为压缩收益证据**。精确规范化相等为确认成功；非空不相等进入待复核，等价公式可能被保守暂挂。没有人工复核的答案不称人工评估完成。

[公开答案的确定性格式等价补充检查](core-public-answer-review.json)仅作事后附录，保留原成绩，不代替开放式或人工质量评估。

## 失败与未运行项

- compression / Started unit lacks durable completion; not automatically replayed：9。
- compression / Unit exceeded 900 seconds：4。
- compression / fail：15。
- compression / insufficient_folds：9。
- localization / fail：32。
- maintenance / ModelError：1。
- maintenance / Started unit lacks durable completion; not automatically replayed：9。
- public / Started unit lacks durable completion; not automatically replayed：1。
- public / incomplete_or_tool_response：20。
- public / pending_review：51。
- skill_test / Started unit lacks durable completion; not automatically replayed：3。
- skill_test / fail：59。
- 尚未启动：270，完整清单保留在summary.json。

## 可复现产物

- [冻结协议](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/protocol.json)
- [逐次结果](/Users/wenyuecheng/code/python/Weyra/wiki/core-evaluation-results.json)
- [汇总与未运行清单](/Users/wenyuecheng/code/python/Weyra/wiki/core-evaluation-summary.json)
- [冻结源码清单](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3/source-snapshot.json)
- [本地完整运行目录](/Users/wenyuecheng/code/python/Weyra/.weyra/core-eval-20261018-v3)
- [评测方法与矩阵](CORE-EVALUATION-PLAN.md)
- [当前生产代码工程回归](CORE-ENGINEERING-VERIFICATION.json)
- [隔离安装验证](CORE-PACKAGE-VERIFICATION.json)
- [公开题审核](../data/core-public-audit-v1.json)

## 结论与下一轮优先级

1. 技能定位、隔离候选与严格发布门禁已有实现；定位夹具仅34种不同输入，不能把60条都当独立语言理解证据。验证题只有两道且存在高起点能力，尚不足以证明长期演化收益。
2. 优先修复摘要输出契约与压力触发的重试放大，再用新的开发数据验证。当前题的自然语言评分有契约缺口，保留本轮结果；新一轮另行冻结，不在已见测试上调参。
3. 补充真实文件操作、开放文本盲评和实际按需加载覆盖；再判断改进是否进入正常Agent流程。当前JSON计划题不等于真实文件工作流。
4. DSH、三层记忆是机制对照；未完成DeepAgent训练方法、WikiSkill完整wiki编译或SkillOpt完整优化器复现。UI、多厂商和子任务效果按本轮范围未做专项评测。
5. 未执行项需要下一轮独立预算与冻结协议；不削减重复次数后称本轮已完整完成。

- [逐项验收与缺口](CORE-ACCEPTANCE.md)
- [技能案例分析](CORE-SKILL-FINDINGS.md)
- [上下文失败分析](CORE-CONTEXT-FAILURES.md)

运行：使用冻结副本的agents包，在仓库外启动 `python -m agents.core_evaluation --project <配置目录> --output <本轮目录> --ledger-root <原共享账本目录> --stage all`。重用本轮目录会恢复账本、跳过已有结果；冻结v3不含端点摘要，恢复时必须人工保持原CUN端点；新版v4另增加端点摘要和持久账本绑定。配置/源码指纹不同会拒绝恢复，已开始但未完成的单元不自动重放。

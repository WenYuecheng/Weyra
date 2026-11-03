# DSH 步骤架构改造验收

## 五个里程碑

1. PreparedStep 拥有模型与准入工具集合、绑定 provider、请求序列和所有出口释放；旧 LoopHost 使用显式适配器。
2. 不可变工具注册、条件准入、公共执行管道、单次调用走批次、MCP 派生范围及注册排干；旧评测入口改用显式重新注册。
3. 六个独立投影与窄查询，SQLite v2 迁移和分领域检查点；原始事件与旧缓存保留，旧程序安全拒绝 v2。
4. GoalService 事务 CAS、连续 revision、模型更新引用校验；resume 保留进度，restart 显式重置；恢复及分支切换 disarmed。
5. 完整回归、构建、隔离安装、集成冒烟及读取对照；同步架构/API/CLI/配置/简历和证据账本。

## 验证结果

最终源码完整 pytest：**866 passed，3 skipped，5 subtests passed，37.92 秒**。全仓 Ruff 与改动空白检查通过。最新 wheel/sdist 构建通过；逐文件核对 wheel 内所有运行时 Python 源码与工作区一致。隔离安装、依赖、CLI、离线演示、离线评测及旧源码入口检查全部通过。

集成冒烟验证了服务层过期 GoalRef 拒绝、Textual 中实际 patch 工具执行及成功卡片、v1 数据库迁移到 v2 后事件正文不变。专项测试覆盖步骤关闭、provider 注册变化后的绑定、条件失败/已关闭集合拒绝、注册关闭等待已启动任务、持久化失败保留原始执行结果、目标 CAS 和窄投影读取。

构建与安装期间曾遇到依赖服务 TLS 中断，重试后的最终构建和安装成功；此处记录的是最新产物的检查结果。

## 读取对照

固定 1000 条、每条约 1 KiB 消息，同版本缓存预热后重复 20 次：目标窄读取 P50 约 **0.010 ms**，兼容聚合状态读取约 **15.809 ms**，消息投影约 **35.838 ms**；冷打开并读目标约 **12.180 ms**。计时含 Python 分配追踪开销，受机器环境影响。

这是读取范围对照，不是旧版/新版端到端提速；不推导线上成功率或成本收益。原始历史仍加载在内存，消息投影仍随历史规模增长。原有 Polyglot 成绩与费用口径保持不变。

## 复核材料

- [接口、迁移与目标状态表](DSH_STEP_ARCHITECTURE.md)
- [源码及产物指纹和验证统计](evidence/dsh-step-refactor-validation.json)
- [完整测试输出](evidence/dsh-step-refactor-tests.txt)
- [隔离安装输出](evidence/dsh-step-refactor-package.txt)
- [固定规模读取测量](evidence/domain-read-comparison.json)

未提交、未发布，未调用付费模型。多文件 patch 继续精确匹配、顺序提交，不承诺整体回滚；来源分类不构成宿主插件隔离；领域投影改造不包含磁盘历史分页。

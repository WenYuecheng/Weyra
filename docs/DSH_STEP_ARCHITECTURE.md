# DSH 步骤架构吸收：当前实现与升级说明

参考 DSH ce7073d5，在既有 P0/P1 基础上实现步骤所有权、工具治理、独立领域投影和目标 CAS。保持原生 Python 分层，仅有 Native 工具执行器，不包含 PTC、外部代理或付费评测。

## 步骤与请求

`PreparedStep` 由循环拥有，捕获模型和工具集合，维护 step_id 与 request_series_id，并通过异步上下文在所有出口释放。普通网络/坏参数重试保留绑定；上下文压缩后更新请求序列；fallback 先执行目标模型容量准入，再重新解析工具集合，共享本逻辑步骤的物理尝试上限和预算。

`ModelRuntime.prepare_call(request)` 返回 `PreparedModelCall`，发送及重试使用绑定的 provider 实例和请求参数。日志只新增 JSON 标识，不保存 provider、任务或工具句柄。响应校验使用本步骤模型。旧 LoopHost 回调通过显式 LegacyStepAdapter 保留。

工具注册或模型选择不是取消已经完成副作用的机制。步骤关闭使准入集合失效，不把实际执行状态回滚。

## 工具 API

- `register(spec, when=None)` 返回 ToolRegistration；旧调用可忽略返回值。
- 注册复制 schema 与 handler，运行时定义不可变；修改原 spec 不改变已注册能力。需要修改时使用 `register(replace(spec, ...))`。
- `await resolve_tools(ToolActivationContext(model, ctx, scope))` 原子解析条件，返回 ResolvedToolSet。
- 条件支持同步/异步，必须返回 bool；异常或取消不发布部分集合。
- `execute_batch(..., tool_set=...)` 和 `execute(..., tool_set=...)` 使用公共 ToolExecutionPipeline；单调用经过单元素批次。
- 无集合的程序化调用只允许无条件工具；条件工具不能按名称绕过。已关闭集合、替换/撤销注册、外来 runtime 的集合均拒绝执行。
- MCP 内部目标沿已准入 mcp_call 的任务局部上下文派生，仍需目标注册、父工具与实际范围有效；隐藏目标不进入模型声明。
- `dispose()` 阻止新调用，`await aclose()` 另外等待已开始工具排干。审批后重检权限与注册；已启动工具不自动重放。
- 调度与持久化失败先排干已启动调用，再抛出携带原始 outcomes 的 ToolBatchExecutionError；这不表示副作用被撤销。

patch 继续默认关闭，其路径、版本、逐操作权限与部分提交规则保持 P1 行为。

## 独立投影及数据库 v2

六个投影分别为 messages、model、queues、goal、children、latest。每个投影拥有 key、state_version、init/apply/validate/checkpoint；无关事件返回原状态，缓存分别记录分支叶及水位。

窄读取：`messages_state()`、`model_state()`、`input_queue_state()`、`goal_state()`、`child_delivery_state()`。`project()`、`latest()` 保留接口；`domain_state()` 仅在显式调用时组装兼容聚合视图。目标读取不加载或深拷贝消息投影。预算恢复继续使用根账本。

SQLite user_version 升级为 2，共享常量定义于 storage_version.py；新增 projection_units 表，按会话及投影 key 保存版本、叶、水位、JSON 与校验。旧 projection_checkpoints 保留为历史缓存，新投影从原始事件重建。迁移验证相关表结构，事务失败不升级；旧程序拒绝 v2。升级不改写历史事件正文。

检查点缺失/版本不匹配/校验失败时重建对应领域。分支、事务回滚与取消不共享错误状态。消息和原始日志仍占用与历史规模相关的内存，本轮不声称分页持久化或常数内存。

## 目标变更

GoalService 是目标持久变更入口，GoalRef 为 goal_id + revision。CAS 比较与 goal_change v1 完整快照提交在同一会话事务内；连续 revision 必须合法。旧 goal 事件读取兼容，第一次新变更确定稳定身份，不改写原记录。

| 操作 | 行为 |
| --- | --- |
| create | 新目标身份、初始计数、显式 armed |
| pause | 保留进度与计数，关闭 armed |
| resume | 保留进度与计数；completed 或轮次耗尽时拒绝 |
| restart | 同一目标身份，增加 revision，显式清零并 armed |
| revise | 修改目标，增加 revision，重置执行修订 |
| complete/block | 关闭 armed；block 保存稳定原因码和说明 |

恢复、普通 fork 与切换分支均不继承进程执行许可。`/goal restart` 新增；pause/resume/complete 和 `/goal <文字>` 保留。resume 的语义从原来的清零改为继续，达到上限须 restart 或 revise。

服务入口 `mutate_goal(operation, goal_id=..., revision=...)`；模型 goal_update 必须提供 goal_id/revision，只能更新已有目标 todo/完成状态，不能重启或开启 armed。步骤前的来源事实和工具结果提供当前目标身份及版本。

错误码：goal_not_found、stale_goal_revision、invalid_goal_transition、goal_limit_reached。自动阻断原因包括 budget-exhausted、round-limit、no-progress；显式 block 可报告 execution-failed 或需要用户输入的原因。异步等待结束后重新检查引用，不允许旧结果覆盖新目标。

## 复核

专项测试：test_prepared_step.py、test_tool_sets.py、test_projection_units.py、test_goal_cas.py；兼容检查继续覆盖 P1、MCP、patch、CLI/TUI 与评测入口。固定长历史读对照脚本为 `python -m scripts.benchmark_domain_reads --output <路径>`，无模型调用。

性能记录比较同版本窄读取和兼容聚合读取；它不是旧版/新版端到端速度比较，也不证明在线任务成功率或费用收益。原 Polyglot 成绩保持历史口径。

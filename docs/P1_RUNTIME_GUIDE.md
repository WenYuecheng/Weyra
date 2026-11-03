# P1 执行与恢复机制

后续升级说明：[DSH 步骤架构](DSH_STEP_ARCHITECTURE.md)。本文保留 P1 时点的 schema v1 和目标行为记录。


本说明对应 2026-11-03 的实现。保留 Python 的 llm → core → coding → service → UI 分层；不引入 Node、PTC、外部代理、桌面自动化或长期调度。

## 存储和恢复

SQLite `user_version=1` 标记当前物理结构。旧无版本数据库先校验已有表和列，再事务升级；未来版本在打开时拒绝。原始事件正文不改写。新增 `event_metadata` 声明事件版本及可忽略性，`projection_checkpoints` 保存可丢弃的领域状态缓存。

模型、输入队列、目标和子任务由 `weyra/projections.py` 的确定性 fold 恢复，模型消息及 latest 查询使用同一状态。恢复不调用模型或工具。预算继续使用原有根账本恢复，不沿分支投影重复扣款。

缓存绑定定义版本、会话、分支叶和水位；正常追加增量更新，切换分支重新恢复。检查点关闭时保存，可删除并从事件重建。未知必需事件或未来必需事件版本拒绝恢复；显式可忽略的未来事件不进入业务投影，原文保留供历史查询。旧扩展事件维持可读取性。

`SessionStore.fork(..., delegated=False)` 保留来源审计并标记用途；普通 fork 不取得委托执行许可。检查点和事件树包含消息历史，空间随有效历史增长；本轮不声称常数内存或完整历史已无需加载。

## 目标与观察

目标持久状态为 active、paused、blocked、completed。旧 stopped/complete 读入时映射为 blocked/completed，不修改旧事件。`armed` 只在进程内，恢复和普通 fork 后关闭。新建目标及显式 resume 才开启。

- `/goal <文字>`：新建或修订目标。
- `/goal pause`：暂停且关闭自动推进。
- `/goal resume`：显式恢复执行许可。
- `/goal complete`：完成并关闭许可。

目标等待活跃子任务期间不消耗自动续轮；等待结束复查目标版本、排队用户输入、取消和预算。进展指纹排除随机 id 与时间信息，仅作为停滞检测依据，不是任务验收证明。

`WeyraAppService.get_snapshot(session_id=...)`、`get_history(session_id=..., before_record_id=..., limit=...)` 可只读查询冷会话，不创建 Agent、不恢复任务。冷状态不携带旧进程的执行许可。历史页及状态中的 `persistent_through_seq` 为持久数据库水位；原 `through_seq` 是当前服务事件总线序号，两者不能比较。

订阅继续先安装监听再获取快照，客户端丢弃事件总线序号不大于快照 through_seq 的增量。背压溢出需要重新同步。解除订阅不取消运行中的任务。

## 协议和来源

原生 reasoning 仅在 provider、协议、模型与端点身份完全匹配时回放。端点身份不保留密钥、查询参数或 URL 用户凭据；旧记录没有足够身份时回退为普通文本与工具事实。原生元数据不删除，支持切回原模型。

`Message.source` 维持字符串接口，分类由统一函数提供。真实用户输入、steering、follow-up 为指令来源；记忆、技能、摘要、工具与子任务结果为事实来源，不能扩大操作权限。事实通过明确的来源信封发送给模型，容量估算和预算上界包含信封开销；权限仍由工具运行时决定，来源分类不是插件或宿主代码的安全隔离。

项目规则、记忆索引、技能目录通过带版本和指纹的上下文事实更新，不再刷新静态系统前缀。旧会话保留已记录 baseline；上下文快照留在日志中，不显示为用户聊天气泡。普通用户输入不受此过滤。

## 配置

在现有 weyra.toml 中可选增加：

```toml
[tools]
apply_patch = false

[goal_policy]
max_continuations = 3
max_stagnant_rounds = 2

[retry_policy]
argument_retries = 1
```

默认关闭 patch，保持原工具声明；坏参数修复默认最多一次，每次物理调用仍计预算。配置拒绝未知字段、错误类型及负数，目标策略要求正整数。

## Patch 使用及失败语义

打开开关后获得 `apply_patch(input)`。支持 Codex 风格 Begin/End Patch、Add/Update/Delete File、Move to、@@ 锚点和 EOF 标记。匹配采用确定性的向前精确上下文；只有新增行、没有旧行的 hunk 按参考语义追加到文件尾，@@ 锚点仍须存在；支持中文及 CRLF，不承诺全部 Codex 模糊匹配语义。

既有文件必须先通过 read_file 取得当前版本。预检模拟整份 patch，包括重复路径、移动、目录冲突与等效 write/edit 权限。预检拒绝不修改目标文件。审批等待后重新检查全部版本及策略，提交每个操作时再次检查。

提交顺序执行，不跨文件回滚。结果逐项报告 completed、failed、unknown、skipped；移动细分目标发布与源删除。失败或取消后停止，保留已完成 diff。unknown 要求重新读取，不能自动重放整份 patch。文件版本检查属于乐观并发控制，不能排除操作系统外部写入者在最终校验与文件操作之间的竞态。

技能修改仍需原独立验证和发布门禁，patch 成功不等于技能改善。

## 验证与简历引用

恢复测试：`tests/test_p1_recovery.py`；目标与冷观察：`tests/test_goal_lifecycle_p1.py`；patch：`tests/test_patch.py`、`tests/test_patch_tool.py`；协议与资源来源见 `tests/test_protocol_source_identity.py`、`tests/test_sourced_resources.py` 及相关新增测试。

固定本地双文件编辑对照验证两次 edit 与一次 patch 产生相同文件结果，并验证失败 patch 零预检写入及恢复。该结果只说明确定性工具调用次数，不代表真实模型任务成功率或线上节费。

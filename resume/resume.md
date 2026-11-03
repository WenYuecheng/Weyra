# Weyra｜基于 Pi Agent / DeepSeek Harness / Hermes 的持续任务与技能演进 Python Agent Harness

**步骤与状态治理**：参考 DSH ce7073d5，将模型请求与工具集合绑定到有明确释放生命周期的步骤；统一单次/批次工具治理，增加条件准入与不可变注册快照。将会话聚合状态拆为独立领域投影，支持数据库 v2 迁移；目标变更采用身份与 revision 的事务 CAS，区分保留进度的 resume 与显式 restart。源码及专项回归可核验，性能结果按实际读取对照说明，不推导线上任务收益。


## P1 恢复与工具治理（2026-11）

实现基于事件的领域状态恢复、SQLite 单调版本迁移和可丢弃检查点；将持久目标与进程内执行许可分离，支持冷会话只读查询及子任务等待。补充 provider/协议/model/端点身份隔离的原生 reasoning 回放、版本化来源上下文及可选多文件 patch，在预检和逐操作提交时检查权限与文件版本，并保留部分完成状态。技术证据见 P1 运行说明与对应离线回归；不将此能力表述为线上成功率或性能提升。


## 2026-11 架构更新补充

**执行一致性治理**：参考 DeepSeek Harness `ce7073d5` 的步骤准入设计，在 Python 运行时实现候选模型容量检查及提交失败回滚，避免先切换路由再发现目标窗口不可用；将模型步骤工具声明与注册实例绑定，阻断工具重载、移除及审批等待期间替换后的旧调用，保留执行时参数与权限重检。使用离线回归验证切换成功、拒绝、存储失败回滚和工具替换路径；不据此推断任务成功率或性能收益。

面试可展开：候选策略与有效路由为何分离；已提交压缩为何不回滚；普通重试与 fallback 的准备边界；注册身份检查为何不能替代权限重检。PTC 和桌面扩展仍属后续规划；领域投影及可选 patch 已在 P1 实现。


**个人项目｜Agent Runtime / LLM 应用开发**

**参考来源**：Pi Agent `4270522473e677ddba7b093caa0064a3df51d459`；DeepSeek Harness `d743267388641bc76f17c45ce8b4c231aed1d32c`；Hermes Agent `590af66a58c2b3eb3c5edd1dcfefa09c625ae18a`；WikiSkill `arXiv:2608.27454v1`、SkillOpt `arXiv:2605.23904v1`、AutoSkill `arXiv:2603.01145v2` 与 SkillEvo；Anthropic MCP 2024-11-05；Aider Polyglot。

## 详细版

### 技术栈

- 运行时 / 协议：Python 3.11+、asyncio、OpenAI / Anthropic-compatible 流式 API、SSE、基于 Anthropic MCP 2024-11-05 的 stdio 网桥
- Agent / 上下文：基于 Pi Agent 的 ReAct 微内核与 Steering / Follow-up 双队列；基于 DSH 的八节检查点压缩、8KiB 中间修剪、10 路工具调度；基于 Pi / Anthropic 的 4-breakpoint Prompt Cache；Usage Anchor、SHA-256 CAS
- 状态 / 治理：基于 Pi Agent 的 Session Tree DAG；基于 DSH 的 Fail-Closed Budget、jobs / subagent、POSIX 进程组；SQLite WAL、`fcntl` 排他锁、`RLock`、`BEGIN IMMEDIATE`；基于 Hermes 的配置 / 凭据分离与透明 Markdown 记忆
- 技能演进：基于 WikiSkill 的经验 / 知识 / 技能分离、SkillOpt 的有界补丁与验证门禁、AutoSkill / SkillEvo 的经验驱动维护；四级定位、歧义弃权、A/B + B/A 双盲裁判、语义版本与回滚
- 评测 / 工程：基于 Aider Polyglot 的 Python / JavaScript 64 题子集；基于 DSH `benchmarks/` 的单因素对照；pytest、ruff、build、Docker 评测容器
- 界面 / 接入：基于 Pi 单输入源与三段式 Ctrl+C 的 CLI / TUI；Python SDK（`WeyraAppService`）

面向长程编码与工具调用任务，基于 **Pi Agent** 的依赖注入的 ReAct、Session Tree DAG、Steering / Follow-up 双队列与 4-breakpoint 缓存，基于 **DeepSeek Harness** 的八节检查点压缩、8KiB 中间修剪、Fail-Closed 预算与 POSIX 进程治理，基于 **Hermes Agent** 的配置 / 凭据分离与透明 Markdown 记忆，基于 **WikiSkill / SkillOpt / AutoSkill** 的技能演进方法，在原生 Python asyncio 上完成五层解耦与可复核评测。

**执行内核（基于 Pi Agent 的 ReAct + 双队列；基于 DSH 的 subagent / jobs）**：基于 Pi Agent `packages/agent/src/agent.ts` 的依赖注入的 ReAct 单步状态机与 `PendingMessageQueue` 的 one-at-a-time 双队列，Weyra 在 `weyra/core/loop.py` 实现 `run_agent_loop`，把模型推理、工具调用与会话落库收敛为统一契约；配置 `max_steps=40` 熔断、有限重试、协作式取消与工具消息配对修复；steering 在工具步边界插队，follow-up 在整轮终态后执行，并新增 `input_queued` / `input_consumed` 持久化与重启恢复。5 类生命周期 Hook 基于项目既有第三方 MIT 接口实现，ToolCallHook 自身异常时 Fail-Closed 阻断。基于 DSH `packages/subagent` 的 Fork / Spawn 双模，spawn 零历史污染、fork 深拷贝父会话，并发 3、递归深度 1，交付 summary / evidence / artifacts / unresolved / usage；基于 DSH `packages/jobs` / `packages/subprocess`，后台 Job 用独立日志承接 stdout/stderr，支持所有权转移、`SIGTERM → SIGKILL` 两阶段取消与 POSIX 进程组清理。控制指令对齐 DSH `/plan`、`/goal`、`/compact`；TUI 对齐 Pi 的三段式 Ctrl+C（取消任务 → 清空草稿 → 释放锁与进程组退出），同一内核复用于 CLI、TUI、SDK 与离线评测。

**上下文压缩（基于 DSH 的八节检查点 + 8KiB 修剪；基于 Pi Agent 的双向截断 + 4-breakpoint 缓存）**：基于 DSH `compaction-basic/src/config.ts` 的 0.8 / 65,536 / 0.16 压力参数与 `summarizer.ts` 的八节检查点原文（commit d743267），Weyra 在 `ContextPolicy` 中实现 `P = C − R − H`、`floor(min(C×θ, P))`、`floor((C−R)×ρ)` 与 `Retain < Threshold` 的 Fail-Loud 校验，八节固定 Primary Request、Key Technical Concepts、Files and Code、Errors and Fixes、Pending Jobs、Current Work、Next Step、Critical Context，并按完整工具交互组保留近期原文。基于 DSH `compaction-tool-result-pruner` 的 8,192 / 4,096 / 1,024 中间修剪，Weyra 增加全文 SHA-256 CAS 外置与按需回读；基于 Pi Agent `truncate.ts` 的 2,000 行 / 50 KiB 双向截断与 `grep.ts` 的 100 条 / 500 字符/条，Weyra 实现 `output.py` 的 `truncate_head/tail`。基于 Pi / Anthropic 的 4-breakpoint Prompt Cache，Weyra 最多设置 4 个缓存标记（系统块首尾 + 最近 2–3 条消息），跳过 thinking / redacted_thinking，并把动态 Git / 时间事实作为独立观察消息注入。基于 DSH 的 chars/4 Token 估算，Weyra 改用更保守的 UTF-8 bytes/2，并用 Usage Anchor 把供应商真实 Usage 与启发式估算做带符号差值校准。

**会话状态（基于 Pi Agent 的 Session Tree DAG；基于 DSH 的 CAS / 崩溃恢复；基于 Hermes 的 SQLite 状态）**：基于 Pi Agent 的 Session Tree DAG（`id` / `parent` / `leaf`），Weyra 用 SQLite WAL + append-only 事件 + parent / leaf 指针实现 branch / fork / 时间旅行：branch 只移动活动叶、fork 生成独立会话并写入 seed，支持非破坏性平行推演；基于 DSH `packages/typert` / `packages/storage` 的大对象内容寻址与崩溃恢复，Weyra 把 system / tools 等大对象按 SHA-256 存入 CAS，启动时 `recover_interrupted` 合成未闭合工具的 unknown outcome；基于 Hermes `hermes_state_common.py` 的 SQLite 结构化状态，Weyra 采用 WAL、`synchronous=FULL`、单写者与事务，不复制网关表。Weyra 自研扩展：`fcntl.flock(LOCK_EX|LOCK_NB)` 单写者锁、`threading.RLock`、`BEGIN IMMEDIATE` 事务、压缩投影与 context_version 原子提交、未闭合工具批次禁止分支。基于 Hermes `agent/memory.py` 的透明 Markdown 记忆与 `skills/` 的 SKILL.md frontmatter 发现，Weyra 用 SHA-256 manifest 跟踪外部编辑。

**预算与治理（基于 DSH 的 Fail-Closed 预算 + 10 路读写协调；基于 Hermes 的配置 / 凭据分离）**：基于 DSH `packages/guard` 的 Fail-Closed 两阶段预算，Weyra 在 `Budget.reserve/settle` 中按 Token、调用次数、人民币三个维度预扣，`total + pending + amount <= limit` 才放行，响应后按 Usage 结算、差额退还；金额用 Decimal，未知用量按预扣额保守入账，崩溃重启时 `attach()` 重放未核销 pending，父子任务共享根账本，维护类调用受独立 maintenance_limit 约束，子任务可设独立 token scope。基于 DSH `DEFAULT_MAX_PARALLEL_TOOL_CALLS=10`，Weyra 实现 10 路只读并发 + 写独占（写优先防饥饿）；基于 DSH `packages/shell` / `packages/fs` 的读写协调与 `packages/api` / `packages/host` 的两阶段审批，Weyra 在异步审批完成后重新检查本实例与父级策略；基于 Anthropic MCP 2024-11-05，Weyra 的 MCP stdio 服务必须显式 `trusted=True` 才允许启动。基于 Hermes 的 `config.yaml` + `.env` 分离，Weyra 实现 `weyra.toml` 行为配置与 `.env` 凭据分离、5 级覆盖、严格白名单与模型容量契约。

**技能演进（基于 WikiSkill 的三权分立 + SkillOpt 的有界优化 + AutoSkill / SkillEvo 的经验维护）**：基于 WikiSkill 的 instruction / knowledge separation，Weyra 把执行经验与可执行技能分开，`knowledge_note` ≤300 字符、最多 10 条，拒绝候选后保留知识；基于 SkillOpt 算法 1 的有界补丁与验证严格改善，Weyra 限制 1–2 处编辑、单处 ≤1,000 字符、旧文本唯一匹配、`base_hash` 匹配当前快照，候选与旧版在独立临时目录重执行固定用例，硬断言检查输入完整性 / 必需产物 / 无额外文件 / completed；基于 AutoSkill / SkillEvo 的近期用户经验边界、冻结回放、开发与晋升验证分离、固定候选预算与证据不足孵化，Weyra 在 `online_skill_evolution.py` / `skill_replay.py` 中接入交互窗口、维护预算与 `incubating` 状态。Weyra 自研扩展：四级定位（显式 ID → 成功使用记录 → 全称 / 别名 → 词法召回）、歧义弃权、A/B + B/A 双盲裁判、成功数严格增加且硬失败不增加才发布、版本快照与回滚（1.0.0 → 1.0.1 → 1.0.2）；默认关闭，开启必须显式配置 `skill_judge` 路由与固定验证清单。

**评测体系（基于 Aider Polyglot + DSH 单因素对照 + WikiSkill / SkillOpt / AutoSkill 的验证划分）**：基于 Aider Polyglot 开源基准的 Python / JavaScript 64 题子集，Weyra 采用累计保留 + 多轮修复协议：保留既有 29 题通过，35 道待修复题继续运行新增通过 21 题（60.0%），累计 50/64（78.1%），Python 28/32（87.5%）；主模型 DeepSeek-V4.1-Flash、备用 gpt-5.6-luna，主输出 4,096 token，每题最多 80 个模型步、120 次物理请求、720 秒，累计费用按配置费率保守估算 ¥49.4882（¥0.7733/题）。基于 DSH `benchmarks/` 的单因素对照，Weyra 构建 20 个基准场景 + 5 组 30 个专项（cache / compression / subtasks / memory / evolution），离线 20/20 + 30/30 全部通过；基于 Pi / Anthropic 的 4-breakpoint 缓存对照，缓存修正后 4/4 复测通过。基于 WikiSkill / SkillOpt / AutoSkill 的开发 / 验证 / 晋升划分，Weyra 完成 24 任务 × 2 次定位配对：26/48 → 40/48（54.2% → 83.3%，+29.2pp），14 胜 0 负 34 平，Bootstrap 95% CI [12.5, 50.0]pp，8 次歧义任务正确弃权 0/8 → 8/8；完整技能评测 294 槽位 / 220 次执行 / 11 次恢复 / 48 组双顺序盲评，100 元账本累计 ¥34.69；旧技能维护 4/8 → 8/8、表达与例外 0/8 → 8/8，渐进加载 14/18 → 18/18；真实链路基准 20/20；写作演示 1.0.0 → 1.0.1 → 1.0.2；完整测试 702 passed / 3 skipped / 5 subtests（20.88 s）。

**工程交付（基于 Anthropic MCP 2024-11-05 + OpenAI / Anthropic 协议）**：基于 Anthropic MCP 2024-11-05 的 stdio 握手、`tools/list`、`tools/call`，Weyra 实现 `mcp_client.py` 的显式信任与环境变量白名单；基于 OpenAI / Anthropic 协议，Weyra 提供流式 Provider、5 类用途路由（main / compression / memory / evolution / skill_judge）、CLI、Textual TUI 与 `WeyraAppService` SDK；仓库含 87 个 Python 模块、约 2 万行代码、88 个测试文件，ruff、build、隔离 wheel 安装通过；版本 0.1.4，MIT；提供架构、评测协议、技能演进、API、配置、CLI/TUI 与面试答辩文档，公开摘要保留协议、任务状态、费用口径与原始产物 SHA-256。

**项目地址：** [github.com/WenYuecheng/Weyra](https://github.com/WenYuecheng/Weyra)；离线演示：`weyra demo --scenario writing --offline`

## 简略版

### 技术栈

- 运行时 / 协议：Python、asyncio、OpenAI / Anthropic-compatible API、SQLite WAL、Anthropic MCP 2024-11-05、Textual、Python SDK
- Agent / 上下文：基于 Pi Agent 的 ReAct、Steering / Follow-up 双队列、Session Tree DAG、4-breakpoint 缓存；基于 DSH 的八节压缩、8KiB 中间修剪、10 路工具调度
- 治理 / 演进：基于 DSH 的 Fail-Closed Budget、jobs / subagent、POSIX 进程组；基于 Hermes 的配置 / 凭据分离与 Markdown 记忆；基于 WikiSkill / SkillOpt / AutoSkill 的技能演进
- 评测 / 工程：基于 Aider Polyglot 的 64 题子集；基于 DSH `benchmarks/` 的 20 + 30 专项；pytest（702 项）、ruff、build、Docker

基于 **Pi Agent** 的 ReAct、Session Tree、双队列与 4-breakpoint 缓存，基于 **DeepSeek Harness** 的八节压缩、8KiB 修剪、Fail-Closed 预算与 POSIX 进程治理，基于 **Hermes Agent** 的配置 / 凭据分离与 Markdown 记忆，基于 **WikiSkill / SkillOpt / AutoSkill** 的技能演进方法，在原生 Python asyncio 上构建可中断恢复、可动态干预、支持技能持续演进的 Agent Harness。

- **执行内核（基于 Pi Agent + DSH）**：基于 Pi 的 `PendingMessageQueue` 双队列与 Session Tree DAG，steering 在工具步边界插队、follow-up 在整轮终态后执行，新增 `input_queued` / `input_consumed` 持久化与重启恢复；工具消息配对修复、5 类 Hook、`max_steps=40` 熔断与协作式取消统一 CLI、TUI、SDK、评测入口。基于 DSH 的 spawn / fork 双模（并发 3、深度 1），后台 Job 用独立日志、`SIGTERM → SIGKILL` 两阶段取消与 POSIX 进程组清理。
- **上下文与状态（基于 DSH + Pi Agent + Hermes）**：基于 DSH `compaction-basic` 的 0.8 / 65,536 / 0.16 与八节检查点，按完整工具交互组保留近期原文；基于 DSH 的 8,192 / 4,096 / 1,024 中间修剪与 Pi `truncate.ts` 的 2,000 行 / 50 KiB 双向截断，全文外置 CAS。基于 Pi / Anthropic 的 4-breakpoint 缓存固定前缀，Usage Anchor 校准 Token；基于 Pi 的 Session Tree DAG 用 SQLite WAL + parent / leaf 支持 branch / fork / 时间旅行，基于 DSH 的 CAS / 崩溃恢复补记未闭合工具；基于 Hermes 的 `weyra.toml` / `.env` 分离与 Markdown 记忆。
- **预算与治理（基于 DSH + Hermes）**：基于 DSH `packages/guard` 的 Fail-Closed 两阶段预算，请求前按 Token / 调用 / 人民币预扣、响应后按 Usage 结算；Decimal 账本、未知用量保守记账、崩溃重放 pending、父子共享根账本。基于 DSH 的 10 路只读并发 / 写独占与两阶段审批，审批后重检父子策略；基于 Anthropic MCP 2024-11-05 的显式信任门禁；基于 Hermes 的配置 / 凭据分离。
- **技能演进（基于 WikiSkill + SkillOpt + AutoSkill）**：基于 WikiSkill 的经验 / 知识 / 技能分离，基于 SkillOpt 的 1–2 处 ≤1,000 字符有界补丁、唯一锚点与 `base_hash`，基于 AutoSkill / SkillEvo 的近期经验维护、冻结回放与证据不足孵化；Weyra 自研四级定位、歧义弃权、A/B + B/A 双盲裁判、成功数增加且硬失败不增加才发布、版本快照与回滚。
- **评测结果（基于 Aider Polyglot + DSH benchmarks + WikiSkill / SkillOpt / AutoSkill）**：Polyglot 64 累计 **50/64（78.1%）**，Python **28/32（87.5%）**，35 道待修复题新增通过 **21 题（60.0%）**，累计费用 **¥49.4882（¥0.7733/题）**；技能定位 **26/48 → 40/48（54.2% → 83.3%，+29.2pp）**，**14 胜 0 负 34 平**，Bootstrap 95% CI **[12.5, 50.0]pp**，8 次歧义任务正确弃权 **0/8 → 8/8**；完整技能评测 **294 槽位 / 220 次执行 / 11 次恢复 / 48 组双顺序盲评**，100 元账本累计 **¥34.69**；旧技能维护 **4/8 → 8/8**、表达与例外 **0/8 → 8/8**，渐进加载 **14/18 → 18/18**；离线 **20/20 + 30/30** 全部通过，真实链路基准 **20/20**，缓存修正 **4/4**；写作演示 **1.0.0 → 1.0.1 → 1.0.2**。
- **工程验证（Weyra 自研交付）**：完整测试 **702 passed / 3 skipped / 5 subtests passed（20.88 s）**，ruff、build、隔离 wheel 安装通过；提供 CLI、TUI、`WeyraAppService` SDK、MCP 网桥与离线复现入口。

**项目地址：** [github.com/WenYuecheng/Weyra](https://github.com/WenYuecheng/Weyra)

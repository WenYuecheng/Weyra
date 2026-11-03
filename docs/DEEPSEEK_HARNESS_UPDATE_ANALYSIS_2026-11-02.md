# DeepSeek Harness 更新对比与 Weyra 设计建议

后续实施：本次评估后，P0 模型切换准入和步骤工具注册集合已落地；具体范围见 [架构文档](ARCHITECTURE.md) 与 `tests/test_step_admission.py`。下文保留评估时的现状。2026-11-03 已继续实现 P1，当前语义见 [P1 运行说明](P1_RUNTIME_GUIDE.md)；P2 仍是建议。

分析日期：2026-11-02（Asia/Shanghai）。本文是源码对比和设计评估，建议尚未实现，也不代表 Weyra 已具备建议中的行为。

## 1. 版本、范围与本地更新结果

| 项目 | 分析基线 |
| --- | --- |
| Harness 原版本 | `d743267388641bc76f17c45ce8b4c231aed1d32c`，2026-10-09；根包版本 `0.2.1-alpha.2` |
| 本次取得的上游 master | `ce7073d51254c26cd63b22a60835b8f948deb447`，2026-10-30；根包版本 `0.2.2-alpha.2` |
| Harness 本地工作分支 | `codex/upstream-20261102`，跟踪 `origin/master`，已切换到新源码 |
| Harness 原版本保留 | 本地 `master` 仍指向 `d7432673` |
| Weyra | 当前工作区，HEAD 为 `ab0ccdc`，包含大量尚未提交的 `agents → weyra`、分层和文档重构 |

使用当前 Weyra 文件判断已有能力，而不是仅阅读 HEAD 中的旧 `agents/`。本次没有修改 Weyra 运行代码或覆盖已有改动，只新增本报告；Harness 原有 `.workbuddy/`、`local-settings/`、`tutorials/` 保留。

上游核验使用 `git ls-remote origin refs/heads/master`，返回的 SHA 与本地新 HEAD 一致。原仓库是浅克隆；普通 fetch 网络中断后，改用 `git -c http.version=HTTP/1.1 fetch --depth=1 --no-tags origin master` 获取最新快照。本文比较两个确定的源码树，不统计中间提交数量，也不把浅历史下的 `forced update` 提示解释为上游改写历史。

原始净差异为 **10,012 个文件、513,880 行增加、714,549 行删除**，其中包含翻译、生成文件、快照和文件移动，不能直接等同于功能规模。本文重点审阅 core、compaction、session、llm、subagent、goal、工具及相关升级指南；未逐行审计全部差异。

复查命令，在 Harness 目录执行：

```sh
git diff --shortstat master codex/upstream-20261102
git diff master codex/upstream-20261102 -- packages/core/agent-loop/src/agent.ts
git diff master codex/upstream-20261102 -- packages/compaction/compaction-basic/src/index.ts
git diff master codex/upstream-20261102 -- packages/session/session-projection/src/index.ts
```

## 2. 主要判断

Weyra 适合继续保留 `llm → core → coding → service → UI` 的 Python 分层，吸收上游对步骤、状态恢复和工具执行的具体约束。当前最有价值的设计更新是：**把一次执行步骤准备成固定的模型选择、工具集合和请求依据，并让其生命周期与持久化记录一致。**

上游仍采用 Cordis 的全插件架构；这个架构在旧基线中已经存在，不是本轮新增。Weyra 也没有必要为跟随上游而整体改成 Cordis 或围绕 Node SDK 重写。

需要明确区分三类信息：

| 类型 | 结论 |
| --- | --- |
| 本轮确实改变 | 步骤级模型选择、目标模型容量准入、条件工具集合、工具执行器拆分、投影注册句柄、历史读取与订阅组合、普通 fork 语义 |
| 旧版已有、Weyra 可进一步吸收 | 持久目标与进程内继续执行许可分离、纯投影恢复、模型可见输入可由日志重建 |
| Weyra 已有、应保留并补验证 | 请求全文及大对象引用、Usage Anchor、完整工具交互组、预算预留与结算、动态环境观察消息、工具审批后重检、订阅背压与先订阅后快照 |

## 3. 压缩模板无需升级，模型切换需要重新设计

### 已确认不变

`packages/compaction/compaction-basic/src/summarizer.ts` 在两版中的 Git blob 均为 `2d258fa410a7a87258f8d3a45c548cde0d383014`，整个文件字节相同。工具结果剪枝实现 `compaction-tool-result-pruner/src/index.ts` 也字节相同，blob 为 `5f536bfc9b9848d2de0c807c9c8d255cd45914b8`。

因此，不需要更换 Weyra 的八节模板、仅为追新修改 8192 字符剪枝规则，或把 `THIRD_PARTY_NOTICES.md` 中实际引入模板的旧 SHA 改成新 SHA。应另记“本次核验上游版本”，保留真实引入来源。

### 本轮改变

上游在提交新请求 header 前，使用**目标模型**的窗口、输出预留和专属策略测量上下文。目标容量未知、预算无效、压缩失败或没有安全可压缩区间时，返回 `MODEL_SWITCH_COMPACTION_FAILED` 并保留旧请求 header。已经完成的剪枝和检查点仍保留，因此不能把它描述为压缩内容整体回滚。

来源：[目标模型切换说明](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/upgrade-guide/v0.2.1-alpha.4/model-switch-compaction/guide.md)、[实现](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/compaction/compaction-basic/src/index.ts#L178)。

### 对 Weyra 的建议：P0

当前 `core/model_runtime.py::activate_route()` 直接更新路由并可记录 `model_change`；`core/model_step.py` 的故障切换随后更新 `context_manager.model`。现有机制并不等于完全缺乏容量检查：后续 `prepare()` 会检查新模型。缺口在于，**路由变更先于目标容量和压缩可行性的整体确认**。

建议增加一个由 coding 层协调的模型切换入口：

1. 解析候选模型元数据、该模型的上下文策略及主请求输出上限，保持旧主路由仍有效。
2. 对候选请求视图测量压力，必要时通过仍可处理旧历史的模型或独立 compression 路由压缩；摘要调用仍走既有预算运行时。
3. 重新验证目标输入加输出预留，满足准入条件后才提交有效路由和切换事实。
4. 失败时保留有效旧路由，明确记录失败原因；已提交的压缩记录仍然有效。

验收应覆盖大窗口切小窗口、短历史但 headroom 配置无效、无安全切分点、摘要失败和取消、主路由不可用但独立压缩路由可用。现有上下文签名变化检测只能证明变化，不能单凭摘要内容不同证明 token 数下降。

## 4. 模型选择和工具集合应按同一步骤固定

### 本轮改变

上游把模型选择归 AgentLoop 所有，保存 `lastUsed` 和尚未使用的 `pending`。一个步骤先取得 provider、model、reasoning effort，再解析条件工具、组装提示词和调用模型。同一步及其重试使用已取得的选择；运行中另选模型影响下一步。旧 `agent/request` 钩子仍可改部分控制参数，但已不能临时改 provider、model 或 reasoning effort。

条件工具通过 `register(..., { when })` 在步骤准备时统一解析。得到的集合同时用于声明、Native 执行、PTC 绑定及嵌套调用。条件为假、集合已释放、工具注册被替换或移除时，不允许通过名称调用绕过。条件解析异常会终止该轮，而非发布半套可用工具。

来源：[模型选择](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/upgrade-guide/v0.2.1-alpha.4/session-model-selection/guide.md)、[条件工具](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/upgrade-guide/v0.2.1-rc.2/conditional-tools/guide.md)。

### 对 Weyra 的建议：P0

Weyra 已有 `PreparedRequest`、确定性工具声明和重试时固定请求参数的测试。需要扩展的是步骤生命周期：当前 `declarations()` 从 `specs` 导出，而 `_run()` 执行时再次查 `self.specs`，两者没有共同持有一个具备注册版本的步骤工具集合。

建议引入一个小型步骤准备对象，字段至少包含模型选择、上下文版本、工具声明摘要、已准入工具的注册标识和请求序列标识。由 `run_model_step()` 和 `run_tool_step()` 共享，工具批次结束或步骤失败后释放。动态 MCP、技能工具重载不能让模型看到的旧声明悄悄指向新实现。

这不替代当前执行时的参数校验、路径校验、审批和审批后重检。步骤集合决定“这一步有哪些工具”；最新权限决定“此刻是否允许执行”。撤销权限仍须立即生效。

保留 Weyra 自动故障切换能力，但把换路由视为重新准入的新请求序列，并记录原因；同一序列内的网络重试保持模型和工具集合不变。这样可避免把“重试固定路由”错误实现为禁止所有自动容灾。

建议验收：重试期间模型选择变化、工具声明后被重新注册、MCP 重连、计划状态变化、权限在等待审批时被撤销，以及条件工具在 Native 与未来其他执行方式中保持一致。

## 5. 投影应由领域服务拥有，恢复不能重复运行副作用

### 本轮改变

旧版已具备统一的增量会话投影。本轮进一步把注册改为可等待的私有句柄，领域服务等待注册就绪后暴露业务方法；调用方通过领域方法读取，不再按字符串键读取任意内部状态。句柄释放全局注册后，持有者仍可在清理过程中读取原定义对应的状态，避免卸载顺序造成清理竞态。

投影恢复必须同步、确定、不访问模型或工具；检查点加后缀重放须与完整重放等价。状态版本与持久日志格式版本是不同概念。进程、连接、锁、任务和注册句柄都不属于可持久化事实。

来源：[投影注册决策](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/.agents/notes/implemented/architecture/2026-10-12-session-projection-registration-handles.md)、[投影规则](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/.agents/skills/dsh-session-projection/SKILL.md)。

### 对 Weyra 的建议：P1

当前 `SessionStore.project()`、`latest()` 和多处 coding/child 初始化分别扫描历史；`ContextSessionBridge` 主要是透传适配器，还不是独立投影层。可先建立少量纯 reducer，分别服务模型选择、输入队列、目标、子任务交付和预算恢复，由所属服务提供查询方法。

第一步不必做通用插件注册中心。先为投影定义事件输入、当前状态、版本和已应用水位，并验证分支、fork、压缩后的语义。分支切换必须使用分支身份或叶节点使缓存失效，不能只按数据库全局 seq 判断有效性；分支消息和会话全局预算账本应保留各自作用域。

同时补充 SQLite 单调 schema 迁移及事件解码版本策略。当前 `_schema()` 以 `CREATE TABLE IF NOT EXISTS` 初始化，不能独自承担后续字段、约束和事件语义变更的迁移。上游的版本治理不是本轮突然新增；这是 Weyra 在扩大可恢复状态前应补的基础。

验收：完整重放等于检查点加后缀；丢弃缓存不改变结果；未知必需事件或未来版本明确拒绝；恢复不触发 Shell、模型、自动发布或重复扣款；旧分支不能污染新分支。

## 6. 历史读取、观察、激活和目标继续执行应分别定义

### 本轮改变

上游把历史、投影、状态的读取和观察分别定义，再组合为 Follow 和 Wait。Follow 先安装监听，再计算有明确截止位置的快照，并丢弃快照已覆盖的增量。普通观察不隐式激活 Agent；需要激活时显式传 `activate: true`，且初始快照不承诺 Agent 已就绪。

会话列表只返回列表所需摘要，排除随历史增长的 outline、统计详情等完整投影。本轮目标驱动器另增加等待活跃子代理并在等待结束后复查状态，避免父目标为了等待子任务而不断空跑续轮。

来源：[历史与快照](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/.agents/notes/implemented/architecture/2026-10-21-session-history-stream-and-snapshot.md)、[显式激活](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/upgrade-guide/v0.2.1-rc.3/session-follow-activation/guide.md)、[目标驱动器差异位置](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/goal/goal-round-driver/src/index.ts#L170)。

### 对 Weyra 的建议：P1

Weyra 的 `subscribe()` 已先订阅再取快照，`through_seq`、会话代数和有界队列也已经存在；`list_sessions()` 已使用轻量元数据查询。因此这些应作为保留项，不能列作完全缺失能力。进一步需要把目前依赖“快照读取没有实际挂起点”的一致性说明，提升为可检验的读截止位置和覆盖增量规则，便于将来引入异步数据库或远程读取。

建议通过同一服务提供独立的只读历史/状态入口及观察入口。打开历史、恢复执行资源、允许目标自动续轮分别处理；关闭观察者只释放观察资源，不默认取消执行者。

目标部分还需区分“目标仍 active”与“本进程允许自动推进”。上游在旧基线中已经有持久 phase 与非持久 armed/disarmed，不能把它算作本轮新增。Weyra 当前保存整个 `Goal` 并恢复，其 `should_continue()` 以 3 次续轮、2 次停滞为硬限制；可改为显式状态与策略配置，但保留既有 token、调用及人民币预算约束。

本轮值得直接借鉴的是：子任务进行中等待完成事件，再复查目标 revision、用户输入、取消和预算，避免消耗空续轮。恢复默认不自动执行旧目标是建议策略，需要在正式设计中明确用户体验。工具结果日志不断增长形成的 hash 变化也不能直接代表任务实质进展，应结合验收证据。

## 7. 工具执行器可以拆分，PTC 暂缓

### 本轮改变

`dsh-tools` 将 Native 与 PTC 从自身配置拆成独立执行器，通用注册和执行管道保留共用的验证、授权、结果及日志逻辑。Native 执行器也必须明确挂载，配置缺失在启动时失败。PTC 还增加保留变量的进程内存模式，但这只是 best-effort 运行状态，不是持久恢复；进程失效时不自动重放程序，进程隔离本身也不提供文件访问隔离。

来源：[执行器拆分](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/upgrade-guide/v0.2.1-rc.2/tool-executor-plugins/guide.md)、[PTC 状态保留](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/.agents/notes/implemented/architecture/2026-09-23-best-effort-persistent-ptc.md)。

### 对 Weyra 的建议：P1 / P2

先在 `core/tools.py` 中明确注册、准入、执行、结果提交的内部职责，并保持单个 Native 实现。只有确实增加第二种执行方式时再引入执行器协议，避免先搭通用框架。

PTC 放在 P2：它会新增代码执行、隔离、资源清理和嵌套工具调度成本。在接入前，必须用同一批编码任务比较成功率、费用、工具轮次和恢复行为；不能仅由“少发几次工具请求”推导整体收益。取消、超时、未知副作用结果保持分开的错误分类，程序恢复不得重放写入。

## 8. 文件编辑适合增加受控 patch，但不默认启用

### 本轮改变

新增 `fs-patch` 纯文本解析库，并把 `apply_patch` 接入文件服务、观察版本和变更展示。先预检整份 patch，再按顺序提交；提交中可能部分完成，不承诺跨文件回滚。操作结果分别报告 completed、failed、unknown、skipped，未知结果要求重新读取。上游默认仍关闭该工具，未以新增能力为由改变所有模型工具声明。

来源：[文件编辑决策](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/.agents/notes/implemented/feature/2026-09-28-apply-patch-file-editing.md)。

### 对 Weyra 的建议：P1，可独立验证

在现有读写工具之上增加可选 patch 工具，复用路径、权限、文件观察版本和结果持久化。文件操作逐项给出状态，移动要区分目标已写入但源未删除。路径逃逸、并发外部修改和取消都要经过同样的治理入口。

这项能力与 Weyra 的编码任务和技能局部编辑较契合，但先通过配置开关试用，验证多文件任务是否实际降低调用次数、错误率及费用。技能发布仍必须经过已有重执行和独立验证门禁，patch 成功不代表技能改善。

## 9. 协议、提示词和来源需要更精确的约束

上游升级 pi-ai 后，跨 provider/API/model 的私有 reasoning 不再转为普通文本；完成响应中的坏工具参数被归为 `MALFORMED_RESPONSE`，默认不重试。Weyra 已按 provider/model 判断是否回放原生 reasoning，且严格解析工具参数，但 `core/model_runtime.py` 默认允许一次 `tool_arguments` 重试。这是可明确选择的策略差异，不是照抄错误码即可解决的问题。

建议在请求与原生响应元数据中明确协议身份及配置身份，补充 A→B→A 切换回放测试；把坏参数修复尝试做成显式有限策略，纳入物理调用预算并留下原始失败记录。严格保证未接受有效响应时不执行工具。

上游还把进程路径和 Web URL 从静态系统提示词移到运行上下文，避免恢复时破坏 Claude reasoning 签名。Weyra 的 Git/时间动态观察已经独立于 system；但 `ResourceLoader.system()` 仍拼入项目路径、规则、记忆索引及技能目录，应分别定义静态策略、版本化指令和动态事实的更新方式，保留旧日志及签名，不通过改写已记录历史完成升级。

上游将用户全局指令链和项目链分别限额，避免大项目规则挤掉全局要求。Weyra 目前主要读取项目根 `AGENTS.md`、`WEYRA.md`，尚未实现同样的全局和嵌套链；若扩展加载器，应同时设计独立预算、优先级、来源及省略提示。

实验性 Auto review 现在识别各输入通道中真实 user-sourced 指令，且明确文件、工具结果和检查点不能授权。Weyra 已有 `Message.source`，可以给后续来源模型增加“是否携带用户授权”的语义；从检索、记忆或技能优化产生的文字，不能因为被注入上下文就继承用户权限。当前无需因此加入一个额外 LLM 审批器。

来源：[协议回放](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/upgrade-guide/v0.2.1-rc.2/pi-ai-protocol-replay/guide.md)、[动态上下文与签名](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/.agents/notes/implemented/bug-fix/2026-10-11-process-specific-prompt-values-in-runtime-context.md)、[指令链预算](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/.agents/notes/implemented/architecture/2026-10-16-separate-instruction-chain-budgets.md)、[授权来源](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/upgrade-guide/v0.2.1-rc.2/auto-review-user-sources/guide.md)。

## 10. 普通 fork 和子任务委托应区分

上游新普通 fork 保留选择的历史前缀但不再记录持久 creator 关系，子代理仍保留直接创建者，避免普通分叉被当作子代理隐藏、通知或受父任务生命周期约束。旧日志不重写。

Weyra 的 `SessionStore.fork()` 目前记录 `source_session`/`source_leaf`，并复制 baseline 和 goal，既服务交互 `/fork` 又服务子任务。可以保留来源信息，但不应从来源字段推导委托权限、取消归属或自动执行许可。建议明确普通 fork 与 delegated fork 的用途参数，只由 ChildManager 建立运行时任务归属；普通 fork 是否复制目标、预算历史和审批状态必须分别定义。

来源：[普通 fork 决策](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/.agents/notes/implemented/architecture/2026-10-23-independent-session-forks.md)。

这不要求删除 Weyra 的来源审计字段，也不要求放弃 Pi 风格 Session Tree。上游普通 fork 的产品选择与 Weyra 的事件树可以独立演进。

## 11. 不宜进入当前主线的变化

| 上游变化 | 对 Weyra 的处理建议 |
| --- | --- |
| Plugin Market、插件推荐和动态组合 | 保留现有 Python 装配；出现明确外部扩展需求后再设计插件发行和兼容策略 |
| CUA 对象 API、macOS helper、应用级授权 | 除非 Weyra 要做桌面自动化，否则暂不进入编码主线 |
| Forge GitHub 面板、worktree 与变更请求关联 | 可作为未来编码工作台能力，先完成执行与恢复基础 |
| OAR 外部 Codex/Claude 委托 | 实验项；原生权限、费用、取消、历史和凭据需要单独适配，不能假设继承 Weyra 全部治理 |
| 独立 Schedule 与 RRULE、多种会话目标 | 当前没有同等调度产品需求；先定义通知和执行授权，再讨论长期托管 |
| Node >=24.9、pnpm 11、TypeScript 构建拆分 | 属于上游运行环境；不迁移到 Python 项目。Weyra 已有 wheel/sdist 构建和干净安装验证，继续维护 |

尤其不能照搬新版 Schedule 的 Host 全局任务管理范围：上游工具已经从“仅本会话提醒”扩大为“按 id 管理 Host 任务”，属于权限与产品语义变化，不只是更换定时格式。

## 12. 三种吸收路径与推荐次序

| 路径 | 做法 | 取舍 |
| --- | --- | --- |
| A：仅维护当前设计 | 保持实现，记录上游差异并补少量回归 | 成本最低，但步骤一致性与模型切换语义继续分散 |
| **B：保留分层，分批吸收执行和恢复约束（推荐）** | 先完成步骤准备与模型切换，再改投影和生命周期，最后验证可选工具 | 与现有代码吻合；每批都有独立验收，不依赖重写 |
| C：以 DSH SDK 或全插件运行时替换内核 | 大幅委托 Node/Cordis 或复刻通用插件系统 | 上游功能接入更直接，但增加跨进程部署、计费、取消、会话与技能维护适配成本 |

推荐的实施分批如下；这是评估顺序，不是已批准的实现计划或工期承诺。

| 批次 | 涉及 Weyra 文件 | 可观察的完成标准 |
| --- | --- | --- |
| P0-1 模型切换准入 | `core/model_step.py`、`core/model_runtime.py`、`core/context.py`、coding 装配 | 小窗口切换失败时有效路由不变；压缩和每次尝试均可查账 |
| P0-2 步骤准备与工具集合 | `core/loop.py`、`core/model_step.py`、`core/tool_step.py`、`core/tools.py` | 模型看见的工具与实际可调用工具使用同一准入依据，撤销仍即时生效 |
| P1-1 纯状态恢复与版本 | `session.py`、`coding/session_bridge.py`、`coding/lifecycle.py`、`subagent.py` | 重放等价、分支不串状态、恢复无副作用、未来版本安全拒绝 |
| P1-2 观察与执行生命周期 | `service.py`、`events.py`、`core/state.py`、`subagent.py` | 历史观察不启动任务；快照与增量连续；目标等待子任务不空跑 |
| P1-3 协议与来源 | `llm/providers.py`、`contracts.py`、`coding/resources.py` | 跨模型回放不泄露或破坏原生 reasoning；来源权限不因摘要和检索升级 |
| P1-4 可选 patch | `tools.py` 及文件执行入口 | 预检失败零写入，部分提交有明确结果，编码样本对比可复核 |
| P2 执行器及产品扩展 | 按实际需求立项 | 有任务收益证据后再引入 PTC、外部代理、定时任务或桌面能力 |

## 13. 原有设计文档应怎样更新

建议在正式设计修订时修改这些表述，保留原文档现状供本次评审：

- `ARCHITECTURE.md`：执行链加入步骤准备、目标模型准入、工具集合及请求序列；存储章区分事件树、领域投影和进程资源；目标与子任务分别说明恢复和继续执行规则。
- `FRAMEWORK_COMPARISON.md`：把“参考版本 d743267”与“后续核验版本 ce7073d5”分列，区分上游机制、Weyra 集成和可验证差异。“完全消除位置偏见”“API 400 免疫”“原生 Python 的重大工程超越”等绝对结论需要改为实际约束及测量结果；顺序交换可降低位置影响，不能证明全部消除。
- `API_REFERENCE.md`：补充模型选择何时生效、重试/故障切换区别、观察与执行的独立生命周期、fork 与委托的不同语义。
- `CONFIGURATION_REFERENCE.md`：明确每模型 headroom、摘要输出上限、主请求输出上限、目标续轮和坏参数修复策略；不要把独立预算混成一个上限。
- `BENCHMARKS.md`：新增恢复一致性、模型切换、工具集合和 patch 的评估协议；原 Polyglot 结果继续绑定原运行配置，不能直接用作新设计收益证明。
- `THIRD_PARTY_NOTICES.md`：只在实际引入或改编新源码时新增对应来源和许可；本轮提示词来源记录无需迁移。

还应纠正“纯函数循环”的含义：当前 `run_agent_loop()` 经宿主执行模型请求、工具和持久化，属于依赖注入的异步编排，并不是数学上的无副作用纯函数。真正需要纯且确定的是状态投影及其 reducer。

## 14. 已做验证与限度

已核对远端 master SHA、本地新旧分支、关键未变文件的 Git blob，阅读上游升级指南并对照双方实现。本轮不是完整安全审计，也没有对所有新增功能作运行验证。

在 Weyra 当前工作区执行以下离线回归：

```sh
.venv/bin/python -m pytest -q \
  tests/test_context_upstream.py \
  tests/test_context_usage_anchor.py \
  tests/test_compaction_output_retry.py \
  tests/test_compression_reliability.py \
  tests/test_model_runtime.py \
  tests/test_sqlite_sessions.py \
  tests/test_delivery_boundaries.py \
  tests/test_service_compatibility.py \
  tests/test_pi_layering.py \
  tests/test_llm_layering.py
```

结果：**134 passed in 2.20s**。这些测试验证当前基础行为；没有证明建议中的步骤工具集合、目标模型切换准入或新投影体系已经实现。

没有运行完整 Weyra 测试、真实模型评测或新版 Harness 构建，也没有安装/更新 Harness 依赖。当前结论限于已取得源码和上述离线回归，不包含费用、延迟或任务成功率提升承诺。

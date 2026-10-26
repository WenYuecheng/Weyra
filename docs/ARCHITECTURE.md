# Weyra 系统全景架构与工业级核心微内核白皮书 (System Architecture & Microkernel Whitepaper)

> **文档定位**：本文档为 **Weyra** 自主编码智能体系统的官方核心技术白皮书与架构工程实现指南。
> 系统性阐述了基于 **Pi Agent 极简纯函数 ReAct 微内核**（Session Tree DAG 树形事件溯源、Steering/Follow-up 双队列优先级调度、成对工具抗 400 防御、TTY 单输入源协同）、**DeepSeek-Harness 工业级工程规约**（DSH 8 节结构化检查点压缩、8192 字符中间修剪、Fail-Closed 预算硬防护与微美分结算、POSIX 进程组隔离与多读单写协调锁）、**Anthropic/Pi 4 阶段 Prompt Cache 前缀对齐** 以及 **基于 WikiSkill/SkillOpt 的自进化技能与双盲仲裁门禁** 的完整设计思想、数学推导与全栈源码落地。
>
> 适用于底层架构师、智能体研发工程师以及开源核心贡献者的系统化技术参考。

---

## 目录 (Table of Contents)

- [第一章：五层解耦拓扑与全景系统架构](#第一章五层解耦拓扑与全景系统架构)
- [第二章：Pi Agent 纯函数 ReAct 微内核与执行流](#第二章pi-agent-纯函数-react-微内核与执行流)
- [第三章：人类意图 Steering/Follow-up 双队列调度体系](#第三章人类意图-steeringfollow-up-双队列调度体系)
- [第四章：Anthropic/Pi 4 阶段 Prompt Cache 前缀对齐与 KV 缓存机制](#第四章anthropicpi-4-阶段-prompt-cache-前缀对齐与-kv-缓存机制)
- [第五章：DeepSeek-Harness 八节结构化压缩算法与数学模型](#第五章deepseek-harness-八节结构化压缩算法与数学模型)
- [第六章：Session Tree DAG 树状事件溯源与 SQLite WAL 并发架构](#第六章session-tree-dag-树状事件溯源与-sqlite-wal-并发架构)
- [第七章：成对工具抗 400 防御与大文本中间修剪（Middle Pruning）](#第七章成对工具抗-400-防御与大文本中间修剪middle-pruning)
- [第八章：Fail-Closed 预算守卫、两阶段预留锁与金融级精算](#第八章fail-closed-预算守卫两阶段预留锁与金融级精算)
- [第九章：TTY 终端协同、三段式 Ctrl+C 中断与 POSIX 进程治理](#第九章tty-终端协同三段式-ctrlc-中断与-posix-进程治理)
- [第十章：自进化技能系统、有界补丁与顺序交换双盲仲裁](#第十章自进化技能系统有界补丁与顺序交换双盲仲裁)
- [结语：工业级自主智能体的设计哲学](#结语工业级自主智能体的设计哲学)

---

## 第一章：五层解耦拓扑与全景系统架构

### 1.1 系统总体分层模型 (Five-Plane Architecture)
Weyra 彻底摈弃了传统开源框架中将 Prompt 拼装、模型调用、状态存储与 UI 逻辑纠缠不清的单体设计，严格遵循“微内核、强契约、单向依赖”的原则，构建了清晰的五层解耦拓扑：

```mermaid
graph TD
    subgraph Layer1["1. 表现与交互平面 (Presentation Plane)"]
        direction LR
        CLI["原生流式终端 CLI<br/>weyra/main.py"]
        TUI["Textual 卡片式现代化 GUI<br/>weyra/tui/app.py"]
        SDK["Python 编程式异步 SDK<br/>weyra/service.py"]
        CLI ~~~ TUI ~~~ SDK
    end

    subgraph Layer2["2. 应用服务与调度平面 (Control & Service Plane)"]
        direction LR
        AppService["应用统一门面: WeyraAppService<br/>幂等命令分发 / 会话切换 / 状态快照"]
        ApprovalEngine["双阶段权限审批引擎<br/>只读安全沙箱 / 敏感操作二次确认"]
        HookBus["5 大生命周期 Hook 事件总线<br/>weyra/core/hooks.py"]
        AppService ~~~ ApprovalEngine ~~~ HookBus
    end

    subgraph Layer3["3. ReAct 智能体微内核平面 (Agent Microkernel & Reasoning Plane)"]
        direction LR
        CodingAgent["领域编码智能体: CodingAgent<br/>项目规则 / 记忆注入 / 动态技能"]
        LoopHost["纯函数循环宿主: LoopHost<br/>无 UI 依赖 / 状态机单向流转"]
        DualQueue["Pi 风格双输入队列调度器<br/>Steering (即时插队) / Follow-up (排队跟进)"]
        ToolRuntime["工具运行时: ToolRuntime<br/>内置文件/Shell工具 + 外部标准 MCP 网桥"]
        CodingAgent ~~~ LoopHost ~~~ DualQueue ~~~ ToolRuntime
    end

    subgraph Layer4["4. 上下文、缓存与预算平面 (Context, Cache & Budget Plane)"]
        direction LR
        ContextManager["DSH 上下文引擎: ContextManager<br/>容量测算 / 动态高水位监控"]
        CompactionEngine["DSH 8 节结构化检查点提炼引擎<br/>weyra/core/compaction_prompt.py"]
        KVAligner["Anthropic/Pi 4 阶段缓存前缀冻结器<br/>稳定前缀与实际 Usage 记录"]
        BudgetGuard["Fail-Closed 预算守卫: Budget<br/>原子预留锁 / 事后核销 / 微美分结算"]
        ContextManager ~~~ CompactionEngine ~~~ KVAligner ~~~ BudgetGuard
    end

    subgraph Layer5["5. 存储、事件树与操作系统协调平面 (Persistence & OS Plane)"]
        direction LR
        SessionDAG[("SQLite WAL 状态引擎: SessionStore<br/>Session Tree DAG 树形事件溯源")]
        CASBlob[("内容寻址大对象存储 (CAS Blobs)<br/>SHA-256 离散落盘 / B-Tree 防膨胀")]
        FileLock["POSIX 内核文件锁 (fcntl.flock)<br/>单写者独占 / 进程互斥防脑裂"]
        ProcessConfinement["POSIX 进程组隔离 (start_new_session=True)<br/>子进程生命周期硬管控 / 孤儿进程清理"]
        SessionDAG ~~~ CASBlob ~~~ FileLock ~~~ ProcessConfinement
    end

    Layer1 --> Layer2
    Layer2 --> Layer3
    Layer3 --> Layer4
    Layer4 --> Layer5
```

### 1.2 核心模块全景映射规范

| 物理分层 | 核心模块路径 | 架构设计职责 | 核心开源血统归属 |
| :--- | :--- | :--- | :--- |
| **表现平面** | `weyra/main.py`<br>`weyra/tui/` | 命令行参数解析、原生流式渲染、Textual 卡片组件、TTY 终端协同 | **Pi Agent** (单输入源 TTY 协调器) |
| **服务平面** | `weyra/service.py`<br>`weyra/events.py` | 统一会话生命周期调度、跨进程/线程异步发布订阅、双阶段审批路由 | **自主创新原创** |
| **内核平面** | `weyra/core/loop.py`<br>`weyra/coding/agent.py` | 纯函数 ReAct 单步推进、双队列调度、状态单调演进、模型调用解耦 | **Pi Agent** (ReAct 极简微内核) |
| **工具平面** | `weyra/tools.py`<br>`weyra/mcp_client.py` | 文件/系统工具、标准 Model Context Protocol (stdio) 客户端 | **DeepSeek-Harness** (工具沙箱) |
| **上下文平面** | `weyra/core/context.py`<br>`weyra/output.py` | 容量阈值方程、8192 字符中间修剪、成对工具抗 400 拓扑清洗、KV 缓存对齐 | **DeepSeek-Harness** + **Pi Agent** |
| **预算平面** | `weyra/core/state.py`<br>`weyra/pricing.py` | Fail-Closed 事前额度预留、原子回滚、事后 Usage 精确差额核销 | **DeepSeek-Harness** (预算守卫) |
| **持久化平面** | `weyra/session.py` | SQLite WAL 模式、不可变事件树 DAG、CAS 大对象落盘、`fcntl` 内核排他锁 | **Pi Agent** (Session Tree DAG) |
| **自进化平面** | `weyra/skill_evolution.py`<br>`weyra/skill_judge.py` | 局部有界语法补丁、隔离沙箱实测、顺序交换双盲 LLM 仲裁、零退化门禁 | **WikiSkill** + **SkillOpt** 学术落地 |

---

## 第二章：Pi Agent 纯函数 ReAct 微内核与执行流

### 2.1 ReAct 纯函数执行循环
Weyra 的核心智能体循环（`weyra/core/loop.py`）借鉴了 **Pi Agent** 的极简设计哲学：**将智能体的思考、规划、工具执行抽象为一个纯粹的状态机单步转移函数**。循环宿主 `LoopHost` 与具体的终端 UI 或网络协议完全解耦：

```mermaid
sequenceDiagram
    autonumber
    participant User as 用户 / 调度器
    participant Agent as CodingAgent (Microkernel)
    participant Hook as HookRegistry (5 大钩子)
    participant Context as ContextManager
    participant Budget as BudgetGuard
    participant Provider as ModelRuntime (LLM)
    participant Tool as ToolRuntime
    participant Store as SessionStore (SQLite WAL)

    User->>Agent: 提交输入指令 (prompt / steering / follow_up)
    Agent->>Hook: 触发 UserInputHook (审计与改写)
    Agent->>Store: 持久化 user 事件 (更新树状 DAG 节点)

    loop ReAct 执行单步 (直至模型给出纯文本回答或触发熔断)
        Agent->>Context: 组装消息视窗 (检查是否超限)
        alt 上下文超出高水位阈值 (Threshold)
            Context->>Context: 触发 DSH 8 节结构化压缩
            Context->>Store: 提交 compaction 事件 (挂载为轻量树节点)
        end

        Agent->>Hook: 触发 BeforeModelCallHook 并校验改写请求
        Agent->>Budget: reserve(tokens, estimated_cost_cny) [预留额度]
        Agent->>Provider: 发送流式模型推理请求
        Provider-->>Agent: 流式吐出 tool_calls 或 text_delta
        Agent->>Budget: settle(reservation, actual_usage) [真实核销结算]

        alt 模型生成工具调用 (tool_calls)
            Agent->>Store: 持久化 assistant 工具调用事件
            Agent->>Hook: 触发 ToolCallHook (参数改写或阻止)
            Agent->>Tool: 调度具体工具 (File / Shell / MCP)
            Tool->>Store: 保存原始 tool_result 与 output_ref
            Tool-->>Agent: 返回结果预览
            Agent->>Hook: 触发 ToolResultHook (模型可见结果改写)
            Agent->>Store: 持久化 tool_result 事件 (成对闭合)
        else 模型生成纯文本回答 (text)
            Agent->>Store: 持久化 assistant 终态文本事件
        end
    end
    Agent-->>User: 返回执行结果与耗时账单
```

### 2.2 单步循环的核心不变性 (Kernel Invariants)
1. **原子步转移不变性**：每个推理步产生一个采纳的模型响应，底层允许有限重试与故障切换，产生明确的 `assistant` 事件；
2. **纯函数状态流转**：持久事实由 SessionStore 投影恢复；运行中的任务、锁与传输连接由宿主生命周期管理；
3. **确定性收敛保障**：单轮对话设置硬性循环步长上限（`max_steps`，默认 40 步），防止模型在遇到死循环工具报错时无限轮询，耗尽算力。

---

## 第三章：人类意图 Steering/Follow-up 双队列调度体系

在长程自主编码任务中，人类用户与智能体的交互并非单向等待。**Pi Agent** 最伟大的工程突破之一便是提出了**双输入队列并发调度体系（Dual Input Queues）**，彻底解决了长程任务中的“即时干预”与“批次排队”冲突：

```mermaid
flowchart TD
    UserInput["用户终端输入"] --> Router{输入调度分类}

    Router -->|执行中插队打断| SteeringQ["<b>Steering 队列 (即时干预)</b><br/>优先级: 最高 (P0)<br/>语义: 纠正当前行为, 立即转向"]
    Router -->|空闲排队跟进| FollowUpQ["<b>Follow-up 队列 (后续跟进)</b><br/>优先级: 次高 (P1)<br/>语义: 待当前整轮结束后执行"]

    subgraph ExecutionEngine ["智能体微内核调度引擎"]
        StepBoundary{当前是否处于工具执行步边界?}
        SteeringQ -->|检测非空| StepBoundary
        StepBoundary -- 是 --> InjectSteering["立即消费 Steering 指令<br/>重写下一轮推理前置上下文"]
        StepBoundary -- 否 --> WaitTool["等待当前微步骤工具完成"]
        WaitTool --> StepBoundary
        FollowUpQ --> TurnComplete

        TurnComplete{当前整轮任务是否彻底结束?}
        TurnComplete -- 是 --> CheckFollowUp{Follow-up 队列是否非空?}
        CheckFollowUp -- 是 --> ConsumeFollowUp["消费队首 Follow-up 任务<br/>开启全新的 ReAct 执行轮次"]
        CheckFollowUp -- 否 --> EnterIdle["进入空闲就绪态 (Idle)"]
    end

    subgraph PersistenceCrashRecovery ["崩溃自愈与持久化恢复"]
        StoreEvent[(SessionStore 事务日志)]
        SteeringQ -.->|写入| InQ1["event: input_queued"]
        FollowUpQ -.->|写入| InQ2["event: input_queued"]
        InjectSteering -.->|消费| OutQ1["event: input_consumed"]
        ConsumeFollowUp -.->|消费| OutQ2["event: input_consumed"]
        InQ1 & InQ2 & OutQ1 & OutQ2 --> StoreEvent
    end
```

### 3.1 调度优先级数学模型
设当前系统时间戳为 $t$，队列调度器遵循严格的优先级序：
$$\text{Priority}(\text{Steering}) \succ \text{Priority}(\text{Tool Continuation}) \succ \text{Priority}(\text{Follow-up}) \succ \text{Priority}(\text{New Session Turn})$$

- **Steering（即时纠偏）**：当用户发现 Agent 正在修改错误的文件时输入，指令在当前工具步骤返回后的下一推理边界介入，阻断原定的下一步动作，强制模型根据新指令重新评估轨迹；
- **Follow-up（延后跟进）**：当用户希望在当前重构完成后接着运行测试时输入，任务进入 FIFO 先入先出队列，在当前 ReAct 循环完全处于终态时自动触发。
- **崩溃一致性保障**：队列中未消费的指令全部以 `input_queued` 事件持久化落盘；当进程意外崩溃重启时，Agent 自动全量重放未被 `input_consumed` 冲销的队列事件，恢复未被确认消费的输入项目。

---

## 第四章：Anthropic/Pi 4 阶段 Prompt Cache 前缀对齐与 KV 缓存机制

在大模型服务端推理中，**KV Cache 命中率是决定首字延迟（TTFT）与推理算力成本的命脉**。根据 Anthropic 与现代推理引擎（如 vLLM/SGLang）的缓存机制，缓存复用严格基于 Prompt 的最长公共前缀（Longest Common Prefix）。**只要前缀发生 1 个 Token 的改动，后续所有缓存全部瞬间失效**。

Weyra 遵循 Pi Agent 的前缀绝对神圣原则（*“Per-conversation prompt caching is sacred”*），构建了**最多 4 个缓存标记的请求布局**：

```mermaid
flowchart LR
    System["系统文本块<br/>首块与末块设置缓存标记<br/>可选静态前缀拆分"] --> Budget["缓存标记总数最多 4 个"]
    Budget --> Recent["剩余名额分配到最近 2–3 条消息<br/>选择各消息最后一个可缓存内容块"]
    Recent --> Guard["跳过 thinking / redacted_thinking<br/>发送前清理旧标记"]
```

### 4.1 缓存复用的核验方式

`_anthropic_cache` 对系统块首尾和最近消息分配最多 4 个缓存标记，跳过思考签名块。静态前缀、确定性工具次序和动态事实分离共同支持缓存复用。命中率从实际 `cache_read_tokens / input_tokens` 计算，TTFT 使用逐请求计时，收益使用对应端点费率；本版不以固定命中率或固定延迟承诺替代运行记录。

---

## 第五章：DeepSeek-Harness 八节结构化压缩算法与数学模型

### 5.1 上下文容量测算与触发水位数学模型
当对话历史不断增长逼近物理视窗极限时，Weyra 启用了深度对齐 **DeepSeek-Harness (`packages/compaction/`)** 的动态触发方程：

设模型最大上下文物理容量为 $C$（`context_window`），模型单次最大输出预留 Token 为 $R$（`max_output_tokens`），系统安全缓冲 Token 为 $H$（`headroom_tokens`，默认 65,536），触发水位比例为 $\theta$（`threshold_ratio`，默认 0.8），保留窗口比例为 $\rho$（`retain_ratio`，默认 0.16）。

Weyra 定义**可用压力上限** $P$：
$$P = C - R - H$$

**自动压缩触发阈值（Trigger Threshold）**：
$$\text{Threshold} = \left\lfloor \min\left( C \times \theta, \; P \right) \right\rfloor$$

**压缩后保留最新原文窗口预算（Retain Budget）**：
$$\text{Retain} = \left\lfloor (C - R) \times \rho \right\rfloor$$

系统强制约束核心不变性：
$$\text{Retain} < \text{Threshold} \quad \text{且} \quad P > 0$$
若参数配置违反上述不等式，系统在启动初始化阶段立即抛出异常（Fail-Loud），坚决杜绝死循环压缩或窗口静默溢出。

### 5.2 DSH 8 节结构化检查点提炼流水线
当视窗内 Token 超过 $\text{Threshold}$ 时，系统绝不采用机械的尾部丢弃或滑动窗口截断，而是启动compression 用途路由（未配置时继承主模型）执行 **Commit `d743267` 锁定的 8 节结构化检查点生成**：

```mermaid
flowchart TD
    RawHistory["高水位历史消息序列"] --> Split{保留窗口切分}
    Split -->|最新尾部轮次| TailWindow["保留高频交互原文: Retain 预算"]
    Split -->|待折叠早期轮次| EarlySpan["不可分割原子消息组"]

    EarlySpan --> CompactionPrompt["注入 DSH 八节结构化压缩模板<br/>weyra/core/compaction_prompt.py"]
    CompactionPrompt --> ModelSummary["compression 路由推理"]

    subgraph EightSections ["八大核心结构化维度 (Eight Orthogonal Dimensions)"]
        S1["1. Primary Request and Intent (用户原始诉求与核心意图，逐字摘录)"]
        S2["2. Key Technical Concepts (技术栈、架构模式、工程约定)"]
        S3["3. Files and Code (已修改/关键文件绝对路径清单与核心代码片段)"]
        S4["4. Errors and Fixes (遭遇的异常、已验证修复方案及用户纠正)"]
        S5["5. Pending Jobs (用户显式提出但尚未完结的任务清单)"]
        S6["6. Current Work (生成当前检查点时正在执行的操作)"]
        S7["7. Next Step (紧随检查点的单一步骤下一步行动建议)"]
        S8["8. Critical Context (关键架构决策理由、限制约束与未决问题)"]
    end

    ModelSummary --> EightSections
    EightSections --> TagWrap["封装为 XML 实体: <compacted-summary>..."]
    TagWrap --> NewCheckpointNode["追加 compaction 事件"]
    NewCheckpointNode --> SessionTree["无缝挂载至 Session Tree 作为新活跃基线"]
```

八节结构将目标、文件变更、错误和下一步行动明确组织为交接记录；完整原始历史保留在事件存储中，模型继续工作时使用摘要与保留尾部原文。

---

## 第六章：Session Tree DAG 树状事件溯源与 SQLite WAL 并发架构

传统智能体普遍采用线性消息数组存储历史，一旦用户希望“回退到上一状态重新推演”，线性结构将不得不暴力截断或覆盖数据，导致历史探索路径彻底丢失。

**Weyra 完整继承并原生落地了 Pi Agent 的不可变会话树 DAG（Session Tree DAG）**，结合底层 SQLite WAL 事务引擎与 POSIX 内核排他文件锁，构建了企业级高并发与时间旅行能力：

```mermaid
gitGraph
    commit id: "e0: root_session"
    commit id: "e1: user: 重构数据访问层"
    commit id: "e2: assistant: plan"
    commit id: "e3: tool: read_file"
    branch feature-async
    checkout feature-async
    commit id: "e4: branch(async方案)"
    commit id: "e5: tool: write_file (async)"
    commit id: "e6: assistant: async 完成"
    checkout main
    branch feature-pool
    checkout feature-pool
    commit id: "e7: branch(连接池方案)"
    commit id: "e8: tool: write_file (pool)"
    commit id: "e9: assistant: pool 完成"
```

### 6.1 树状事件模型与时间旅行机制
- **数据表拓扑**：
  ```sql
  CREATE TABLE events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL,
      id TEXT NOT NULL,
      parent TEXT,
      kind TEXT NOT NULL,
      data TEXT NOT NULL,
      request_id TEXT, task_id TEXT, turn_id TEXT,
      UNIQUE(session_id, id),
      FOREIGN KEY(session_id) REFERENCES sessions(session_id),
      FOREIGN KEY(session_id, parent) REFERENCES events(session_id, id)
  );
  ```
- **指针移动式分支**：`branch(record_id)` 校验祖先链与工具配对后移动活动叶节点并追加分支事件；既有事件保留。指针更新是常数操作，完整校验与投影随历史长度增长。
- **派生会话**：`fork()` 创建独立会话 ID，以当前消息投影写入 seed。CodingAgent 子任务另有自己的会话与上下文，并共享父级预算约束。

### 6.2 SQLite WAL 模式与 POSIX 内核级排他锁
为了支持企业级多进程并发调用并防止脑裂：
1. **SQLite WAL 调优**：
   - `PRAGMA journal_mode=WAL;`：读写互不阻塞，读操作不阻塞写，写操作不阻塞读；
   - `PRAGMA synchronous=FULL;`：按 FULL 同步策略提交，配合恢复流程管理中断状态；
   - `PRAGMA foreign_keys=ON;`：强制外键完整性约束，防止孤儿节点产生。
2. **内核级文件排他锁 (`fcntl.flock`)**：
   在写入任何会话前，必须首先在 `.weyra/locks/<sha256(session_id)>.lock` 上申请 `fcntl.flock(LOCK_EX | LOCK_NB)` 排他锁。若该会话正被另一个后台进程或 TUI 占用，非阻塞锁立即拒绝并抛出明确异常，坚决杜绝多写者并发破坏事务。
3. **内容寻址存储（CAS Blobs）**：
   model_request 的 system 与 tools 字段通过 SHA-256 存入 blobs 表，事件保留字段引用；工具原文另写入 artifacts 哈希文件，结果事件保存预览与 output_ref。该路径按事件字段与结果类型处理，不采用统一 1024 字节阈值。

---

## 第七章：成对工具抗 400 防御与大文本中间修剪（Middle Pruning）

### 7.1 成对工具抗 400 防御 (Anti-400 Tool Pairing Invariant)
在对接 OpenAI、Anthropic、DeepSeek 等商业模型 API 时，协议规定：**若 Assistant 消息携带了 `tool_calls`，后续消息流中必须紧随包含完全一致 `tool_call_id` 的 Tool Result 消息**。

若因中间截断、异常抛出或滑动窗口割裂导致产生孤立的 Tool 调用或孤立的 Tool 结果，模型服务端将直接返回 `HTTP 400 Bad Request`，导致整个长程任务雪崩中断。

Weyra 在 `weyra/core/context.py` 中实现了严格的**不可分割原子交互组（Interaction Group）**切分算法：

```mermaid
flowchart TD
    MsgStream["原始有序消息流"] --> Parser["complete_groups 拓扑校验扫描"]

    subgraph AtomicGroup ["不可分割原子交互组 (Indivisible Atomic Group)"]
        direction TB
        AssistantMsg["Assistant Message (发起 tool_calls: [id_1, id_2])"]
        ToolResult1["Tool Result Message (tool_call_id: id_1)"]
        ToolResult2["Tool Result Message (tool_call_id: id_2)"]
        AssistantMsg --- ToolResult1 --- ToolResult2
    end

    Parser -->|匹配成功| AtomicGroup
    Parser -->|发现孤立 Tool 消息| OrphanDetected["抛出 ContextOverflow: Orphaned tool result"]
    Parser -->|发现未闭合 Tool 消息| IncompleteDetected["抛出 ContextOverflow: Cannot prepare an incomplete tool batch"]

    OrphanDetected & IncompleteDetected --> SafeRepair["自动合成占位错误回执<br/>补齐闭合对, 修复拓扑后合法发送"]
```

### 7.2 8192 字符中间修剪算法 (DeepSeek-Harness Middle Pruning)
当执行 `run_shell("cat huge_log.txt")` 或大型文件读取产生数十万字符的大型输出时，若全部灌入上下文将迅速撑爆视窗；若直接尾部截断则会丢失最终的执行退出状态。

Weyra 严格落地了 **DeepSeek-Harness 8192 字符中间修剪规约 (`weyra/output.py`)**：

```python
def prune_tool_result(content: str) -> str:
    # 严格 8192 Unicode 码点 水位线
    if len(content) <= 8192:
        return content
    # 保留头部 4096 字符 (命令启动、初始参数、前置环境)
    # 保留尾部 1024 字符 (最终结果、退出状态码、错误汇总)
    # 中间插入形式化省略标记，全量原文写入 .weyra/artifacts/
    return content[:4096] + "\n\n[... tool result middle pruned ...]\n\n" + content[-1024:]
```

```mermaid
flowchart LR
    Original["超长工具原始输出: 100,000+ 字符"] --> Splitter{长度 > 8192 字符?}
    Splitter -- 否 --> KeepRaw["原样保留输出"]
    Splitter -- 是 --> SpillDisk["全量内容写入磁盘工件 .weyra/artifacts/"]

    SpillDisk --> AssemblePruned["组装修剪文本:<br/>头部 4096 字符 + 中间省略标记 + 尾部 1024 字符"]
    AssemblePruned --> InjectContext["注入大模型上下文 (5120 字符加省略标记；Token 数取决于编码)"]
```

---

## 第八章：Fail-Closed 预算守卫、两阶段预留锁与金融级精算

在工业级生产环境中，大模型递归调用极易因失控循环导致用户账单在数秒内产生数千元不可逆巨额穿透。

Weyra 深度汲取 **DeepSeek-Harness (`packages/guard/`)** 的精髓，落地了金融级 **Fail-Closed 预算守卫体系 (`weyra/core/state.py`)**：

```mermaid
stateDiagram-v2
    [*] --> IDLE: 准备发起模型推理

    state IDLE {
        [*] --> CheckAllowance: 预算额度检查
        CheckAllowance --> Sufficient: 余额充足
        CheckAllowance --> Exhausted: 余额不足
    }

    Exhausted --> [*]: 立即抛出 BudgetExceeded (事前物理阻断)

    state RESERVED {
        [*] --> AtomicHold: 内存原子扣除预留额度
        AtomicHold --> CheckpointEvent: 写入持久化 budget_checkpoint 事件
    }

    Sufficient --> RESERVED: reserve(conservative_tokens, cost_cny)

    RESERVED --> ModelCall: 发出物理 HTTP 网络请求

    state SETTLED {
        [*] --> CalculateActual: 计算真实消耗 Actual Tokens & CNY
        CalculateActual --> RefundExcess: 退还多扣额度 (Reserved - Actual)
        RefundExcess --> WriteSettlement: 写入 budget_settlement 事实
    }

    ModelCall --> SETTLED: 请求成功返回真实 Usage (settle)
    ModelCall --> CrashRollback: 进程网络崩溃 / 异常中断

    state CrashRollback {
        [*] --> ReplayPending: 重启后安全重放 pending 全额计入账本
    }

    SETTLED --> [*]: 释放预留锁
    CrashRollback --> [*]: 杜绝刷单绕过
```

### 8.1 预留与结算方程
输入金额预留采用配置上下文容量与完整序列化请求字节上界的较大者；按最大适用输入费率预留。设该输入 Token 上界为 $N_{\text{in}}$，最大输出预估为 $N_{\text{out}}$，输入单价为 $P_{\text{in}}$（元/百万 Token），输出单价为 $P_{\text{out}}$，缓存读单价为 $P_{\text{cache}}=P_{\text{read}}$，缓存写单价为 $P_{\text{write}}$；未单独配置的输入分项在预留时按基础输入价处理。

1. **事前最保守预留金额（Reserved Cost）**：
   $$\text{Cost}_{\text{reserved}} = \frac{N_{\text{in}} \times \max(P_{\text{in}},P_{\text{read}},P_{\text{write}}) + N_{\text{out}} \times P_{\text{out}}}{10^6}$$
   只有当 $\text{Balance} \ge \text{Cost}_{\text{reserved}}$ 时，请求才被允许发出；
2. **事后结算金额（无独立缓存写入费时的简化式）**：
   $$\text{Cost}_{\text{actual}} = \frac{(N_{\text{prompt}} - N_{\text{cached}}) \times P_{\text{in}} + N_{\text{cached}} \times P_{\text{cache}} + N_{\text{completion}} \times P_{\text{out}}}{10^6}$$
3. **差额退款（Refund to Balance）**：
   $$\text{Refund} = \text{Cost}_{\text{reserved}} - \text{Cost}_{\text{actual}}$$

采用 Decimal 记录金额。缓存读写分段按配置费率结算，独立缓存写入另计；Usage 或费率不完整时保留保守金额与未知标记。机制验证入口见 `tests/test_budget_cost.py`。

---

## 第九章：TTY 终端协同、三段式 Ctrl+C 中断与 POSIX 进程治理

### 9.1 Pi Agent 三段式 Ctrl+C 终端打断状态机
在交互式终端中，暴力拦截 `SIGINT` 会导致进程瞬间死亡、正在写入的数据库锁未释放、后台子进程变成孤儿僵尸；而不拦截则无法中断模型的冗长输出。

Weyra 严谨落地了 **Pi Agent 交互式终端标准三段式打断模型（Pi Three-Phase Interrupt Model）**：

```mermaid
stateDiagram-v2
    [*] --> TurnRunning: Agent 正在推理或执行工具 (activity != idle)
    TurnRunning --> TurnCancelling: 用户第 1 次按 Ctrl+C

    state TurnCancelling {
        [*] --> SendCancelSignal: 派发 cancel_turn() 异步任务取消
        SendCancelSignal --> GracefulHalt: 工具完成当前 I/O 读写后优雅终止
    }

    GracefulHalt --> IdleWithDraft: 回退至空闲态, 保留终端草稿

    state IdleWithDraft {
        [*] --> CheckDraft: 检查编辑器是否存在未发送文字
        CheckDraft --> HasText: 存在草稿
        CheckDraft --> IsEmpty: 草稿为空
    }

    HasText --> ClearDraft: 用户第 2 次按 Ctrl+C
    ClearDraft --> EmptyIdle: 清空输入草稿并在底栏提示

    IsEmpty --> GracefulExit: 用户按 Ctrl+C
    EmptyIdle --> GracefulExit: 用户第 3 次按 Ctrl+C

    state GracefulExit {
        [*] --> ReleaseLocks: 释放 SQLite 锁句柄
        ReleaseLocks --> TerminateSubprocesses: 清理所有 POSIX 子进程组
        TerminateSubprocesses --> ExitZero: 优雅安全退出 sys.exit(0)
    }

    GracefulExit --> [*]
```

### 9.2 POSIX 进程组隔离与僵尸进程治理
当 Agent 执行复杂的外部命令（如 `pytest`、编译构建或微服务启动）时，若仅仅使用基础的 `subprocess.Popen`，父进程中断时子进程派生的孙子进程将沦为后台孤儿僵尸进程，持续霸占 CPU 与端口。

Weyra 落地了工业级 POSIX 进程治理：
- **独立进程会话组 (`start_new_session=True`)**：每个 Shell 命令派生在独立的进程组中；
- **原子信号广播 (`os.killpg`)**：超时或取消时，系统直接向进程组 ID 发送 `SIGTERM`，并在 2 秒超时未响应后升级为 `SIGKILL`，确保所有孙子进程被原子级扫除；
- **异步安全管道流**：Shell 作业将 stdout/stderr 重定向到磁盘日志，由作业管理器执行等待、取消与回收。

---

## 第十章：自进化技能系统、有界补丁与顺序交换双盲仲裁

Weyra 将 **WikiSkill (arXiv:2608.27454)** 与 **SkillOpt (arXiv:2605.23904)** 2026 年预印本方法，构建了**长程自进化闭环体系**：

```mermaid
sequenceDiagram
    autonumber
    participant Task as 失败编码任务 (Trace Context)
    participant Engine as 有界补丁引擎 (SkillOpt)
    participant Sandbox as 隔离重执行沙箱
    participant Judge as 顺序交换双盲仲裁器 (Double-Blind)
    participant Gate as 验证集零退化发布门禁

    Task->>Engine: 输入执行失败上下文与错误反思
    Engine->>Engine: 生成严格有界局部语法补丁 (最多2处, 单处≤1000字符)
    Engine->>Sandbox: 在独立临时目录依次重执行旧版与候选固定用例
    Sandbox->>Sandbox: 运行确定性测试断言 (文件产物、返回值、耗时)

    alt 沙箱硬断言失败
        Sandbox-->>Engine: 编译/测试未通过，立即丢弃补丁
    else 沙箱硬断言通过
        Sandbox->>Judge: 提交候选技能产物与基线产物
        rect rgb(240, 245, 250)
            Note over Judge: 依次提交两种次序
            Judge->>Judge: 顺序 1 仲裁
            Judge->>Judge: 盲测判定 Prompt(A=旧技能, B=新技能) -> 结果 R1
            Judge->>Judge: 顺序 2 仲裁
            Judge->>Judge: 盲测判定 Prompt(A=新技能, B=旧技能) -> 结果 R2
        end
        Judge->>Gate: 汇总仲裁结果 (检测位置偏好 Position Bias)

        alt R1 与 R2 存在冲突或判定为平局
            Gate-->>Engine: 存在不确定性，坚决拦截不发布 (Safety Block)
        else 两次判定一致胜出 且 验证集零功能退化
            Gate->>Gate: 触发原子版本升级 (如 1.0.0 -> 1.0.1)
            Gate->>Gate: 持久化不可变快照，支持可追溯版本回滚
        end
    end
```

### 10.1 核心技术突破与工程超越
1. **彻底废除全量重写**：限制单次技能文档编辑的数量与大小，强制限定采用 **有界局部补丁（Bounded Text Patches）**；
2. **识别位置偏好并拒绝冲突结果（Position Bias）**：大模型裁判天然倾向于打高排在第一位（或第二位）的候选回答。Weyra 通过严格将新旧产物交换位置发起**两次独立双盲推理**；只要两次结果未达成一致（如正向判定 A 优于 B，但反向判定 B 优于 A），一律视为不确定性并坚决拦截；
3. **验证集零退化硬门禁**：新技能发布必须通过离线全量黄金验证集检验，只有在成功数严格增加且硬失败数不增加时才允许并入正式库，在固定验证任务上检查候选改进与硬约束。

---

## 结语：工业级自主智能体的设计哲学

Weyra 的架构演进证明了一条深刻的工程真理：**优秀的自主智能体不仅取决于底层大模型的基座智商，更取决于支撑其长程运行的操作系统级内核体系**。

通过将 **Pi Agent 的纯函数 ReAct 微内核、双队列与不可变树状事件日志**、**DeepSeek-Harness 的 8 节结构化压缩、中间修剪与 Fail-Closed 预算防御**、**Anthropic 的 4 阶段 Prompt Cache 前缀对齐** 以及 **顶会自进化技能与双盲仲裁** 严密交融，Weyra 打造了一座高可用、高确定性、低成本且自主生长的工业级智能体灯塔。

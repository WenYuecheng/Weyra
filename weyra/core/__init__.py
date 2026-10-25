"""智能体核心执行基元与通用循环调度层 (Provider-Independent Execution Primitives)。

【架构定位】
本包是 Weyra 系统的微内核执行中枢，在架构分层上对应 Pi Agent 的 `packages/agent` 以及
DeepSeek-Harness 的 `packages/core` 核心内核。
包含纯粹的智能体推理闭环（Agent Loop）、单步模型调用管道（Model Step）、
原子工具批处理步骤（Tool Step）、全局生命周期拦截网格（HookRegistry）、重试策略（RetryPolicy）
以及状态机执行外壳（AgentHarness）。

【设计思想与开源血统】
1. 深度借鉴 Pi Agent (packages/agent & dg-ai-notes 第 3、7 章) 的无状态生成器微内核设计：
   - 纯函数式内层思考-行动循环（`run_agent_loop` & `LoopHost`）：将业务状态（会话数据库、物理路径、具体工具）
     通过依赖注入（LoopHost 契约）完全抽离，使执行循环成为高内聚、易单测的纯状态机；
   - 专职门禁 Hook 拦截点（`HookRegistry`）：独立于只读 Event 事件系统，专职负责在模型调用前 1ms、
     用户输入进入系统时、工具执行前以及工具结果产生后进行双向拦截、参数改写与动态安全门禁。
2. 深度借鉴 DeepSeek-Harness (packages/core, packages/llm, packages/compaction) 的工业级容灾与批处理规范：
   - 模型调用故障分级重试机制（`RetryPolicy`）：采用指数退避（Exponential Backoff）配合 Full Jitter 随机抖动，
     打散分布式集群重试引发的服务端惊群风暴；
   - 截断输出安全阻断与单事务原子提交：在工具调用批次中，严格防止残缺参数执行，并采用单一数据库事务落地所有工具产物；
   - 物理上下文溢出自愈闭环：在模型请求因窗口溢出（overflow）抛错时，自动触发结构化检查点压缩并原地重试。

【核心不变性与安全约束】
- 物理环境零耦合：本内核包绝对禁止直接硬编码依赖任何物理目录路径、特定 SQLite 数据库表或具体前端 UI，
  所有环境交互必须通过 `LoopHost` 或 `RunContext` 抽象协议注入。
- 单智能体实例非并发重入约束：通过 `AgentHarness` 的 `active_task` 守卫，同一智能体实例物理禁止多任务并发调用。
"""

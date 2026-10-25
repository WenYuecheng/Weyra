"""厂商中立大模型抽象层与网络传输层（Provider-Neutral Model Types & Transports）。

架构定位与分层哲学:
    本包对应 PiAgent 的 `packages/ai` 以及 DeepSeek-Harness 的 `packages/llm` 分层架构。
    严格作为底层的纯粹大模型通信驱动层，负责大模型类型契约定义、主流供应商协议适配（OpenAI/Anthropic）
    以及模型能力静态目录索引。
    
    【核心分层物理隔离保证】:
    本包禁止反向导入任何应用层或执行层逻辑（严禁导入 `weyra.core`、`weyra.coding` 或应用层模块），
    支持在无 Agent 主体、无持久化会话、无复杂工作区环境的纯净轻量场景下被独立加载与调用。

开源框架借鉴与学术溯源:
    1. PiAgent (packages/ai):
       - 借鉴其厂商中立（Vendor-Neutral）消息模型与流式归一化协议（Streaming Normalization）；
       - 严格隔离模型协议转换与智能体控制循环，传输层仅持有取消凭据（ModelContext），不绑定会话存储。
    2. DeepSeek-Harness (packages/llm):
       - 借鉴其按能力属性与商业资费严格解耦的静态规格目录（MODEL_CATALOG）；
       - 规范化请求指纹计算与前缀缓存对齐（Prompt Caching Invariant）。
"""


# Python API 参考

Weyra 提供两层常用接口：`CodingAgent` 用于直接执行任务，`WeyraAppService` 用于应用集成、事件订阅与会话管理。以下签名对应当前源码。

## 直接运行任务

```python
import asyncio
from pathlib import Path
from weyra.config import load_config
from weyra.coding.agent import CodingAgent

async def main():
    project = Path.cwd()
    runtime = load_config(project).build_runtime()
    agent = CodingAgent(project, runtime, enable_evolution=False)
    try:
        answer = await agent.run_once("阅读 README.md 并总结项目用途")
        print(answer)
    finally:
        await agent.close()

if __name__ == "__main__":
    asyncio.run(main())
```

此示例需要已配置的模型端点和密钥。默认权限会阻止未经批准的写入；嵌入应用可通过 `confirm` 回调提供审批。

`CodingAgent(project, runtime, *, session_id=None, context_policy=None, permission_mode="default", confirm=None, budget=None, system=None, max_steps=40, task_token_limit=None, enable_memory=True, enable_evolution=None, ...)`。

| 接口 | 返回 / 用途 |
| --- | --- |
| `await agent.run_once(prompt)` | `str`；执行任务并消费排队追问 |
| `agent.steer(text)` / `agent.follow_up(text)` | 输入项目 ID；插话或排队 |
| `await agent.resume_pending()` | `str`；恢复未消费追问 |
| `agent.abort()` | 取消当前执行 |
| `await agent.compact()` | 是否应用了压缩 |
| `agent.clear_session()` | 清空活动投影与旧排队输入，保留历史和账本 |
| `agent.branch(record_id)` | 新活动叶节点 ID |
| `await agent.spawn_child(mode="spawn")` | 子 CodingAgent；调用者负责关闭 |
| `await agent.close()` | 关闭资源；默认同时关闭 runtime |

流式接入使用 `on_event(kind, data)` 回调。底层 `AgentHarness.run_turn(host, context)` 同样返回字符串，完成状态写入 `context.turn_state`。

## 应用服务

`WeyraAppService(project, runtime_factory=None, *, session_id=None, permission_mode="default", max_steps=40, task_token_limit=None, enable_memory=True, enable_evolution=None, budget_limit_tokens=200000, budget_limit_calls=50, budget_limit_cost_cny=None, maintenance_limit=20000)`。

默认从项目目录加载配置；自定义运行时通过无参 `runtime_factory` 注入。返回对象定义在 `weyra.service` 与 `weyra.events` 中。

| 接口 | 契约 |
| --- | --- |
| `await service.start()` | `StateSnapshot` |
| `await service.submit(text, *, mode="auto", client_request_id=...)` | `SubmissionReceipt(accepted, request_id, mode, message)` |
| `await service.execute_command(line, *, client_request_id=...)` | `CommandResult(ok, code, message, data)` |
| `await service.cancel_turn()` | `CommandResult` |
| `await service.get_snapshot()` | `StateSnapshot` |
| `await service.list_sessions()` | `list[SessionSummary]` |
| `await service.open_session(session_id)` / `new_session()` | `StateSnapshot` |
| `await service.get_history(limit=100)` | `HistoryPage` |
| `await service.subscribe(subscriber_id=None)` | `(StateSnapshot, Subscription)` |
| `service.unsubscribe(subscriber_id)` | 是否已解除订阅 |
| `await service.resolve_approval(request_id, approved)` | `CommandResult` |
| `await service.close()` | `CommandResult` |

提交和命令的 `client_request_id` 为必填关键字参数。同一请求重试时复用 ID；不同任务使用不同 ID。

可直接运行的完整离线示例：[examples/sdk_service.py](../examples/sdk_service.py)。它创建预设模型响应，先订阅，再提交任务，等待 `turn_end`，最后解除订阅并关闭服务：

```bash
python -m examples.sdk_service
# Weyra SDK ready.
```

## 生命周期 Hook

```python
from weyra.core.hooks import AgentStartHook, HookResult

agent.hooks.register(
    AgentStartHook,
    lambda hook: HookResult(updated_system_prompt="按本应用的领域规则处理当前任务。"),
)
```

`UserInputHook` 支持改写输入；`AgentStartHook` 支持当前轮次系统提示词覆写；`BeforeModelCallHook` 支持模型请求消息改写；`ToolCallHook` 支持参数修改与阻止；`ToolResultHook` 支持模型可见结果改写。改写后仍执行相应模式、权限和容量校验。轮次提示词覆写不改变持久化基线。

## 存储与技能

- `SessionStore(project=..., session_id=...)`：`append(kind, data)` 返回事件字典；`project()` 返回消息序列；`recover_interrupted()` 修复未闭合调用；`fork()` 返回独立存储实例，使用后调用 `close()`。
- `MemoryStore(project_path)`：`create/update/get/list/search` 管理 Markdown 事实记忆。
- `SkillStore(root)`：`capture_catalog/load/search/resolve_target` 管理技能快照和选择。
- `EvolutionStore` 管理技能补丁、版本与发布记录；启用自动演化需要显式裁判路由和验证依据，见[技能演进](SKILL_EVOLUTION.md)。

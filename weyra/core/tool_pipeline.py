"""Shared invocation pipeline used by every native call scheduler."""
import asyncio
import inspect
import json
from jsonschema import Draft202012Validator
from weyra.contracts import ToolCall, ToolResult
from .hooks import ToolCallHook
from .tool_admission import ACTIVE_CALL


class ToolExecutionPipeline:
    def __init__(self, runtime):
        self.runtime = runtime

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    async def run(self, call, context, previous=None, prepared=None, tool_set=None):
        """执行单个工具调用的核心流转与异常捕获逻辑 (Execute Single Tool Invocation Core)。

        【执行管道】
        1. 依赖等待：若存在前置就绪事件 `previous`，等待前序工具准备完毕；
        2. 运行时拦截挂钩：调用 HookRegistry 的 ToolCallHook 支持动态修改参数或熔断阻断；
        3. 槽位获取与鉴权：获取读写互斥槽位并验证权限；
        4. 意图记录与事件广播：在 SessionStore 中记入 `tool_intent` 并派发 `tool_start`；
        5. 执行与结果归一化：捕获异常与取消信号，生成标准 ToolResult。

        Args:
            call: 待执行的工具调用请求。
            context: 运行上下文。
            previous: 可选的前置准备事件。
            prepared: 可选的当前准备就绪通知事件。

        Returns:
            ToolResult: 工具执行生成的标准结果。
        """
        inherited_set, parent_call = ACTIVE_CALL.get()
        tool_set = tool_set if tool_set is not None else inherited_set
        registration = self._registrations.get(call.name)
        spec = self.specs.get(call.name)
        token = None
        result = ToolResult(call.id, call.name, "", status="invalid")
        try:
            if previous is not None:
                await previous.wait()
            if tool_set is not None and (tool_set.runtime is not self.runtime or not tool_set.validate(call.name)
                    or (call.name.startswith('mcp__') and (parent_call != 'mcp_call' or not tool_set.validate('mcp_call')))):
                result.content = "Tool registration changed or not admitted for this step"
                return result
            if tool_set is None and registration is not None and registration.when is not None:
                result.content = "Conditional tool requires step admission"
                return result
            if spec is None:
                result.content = "Unknown tool"
                return result
            hooks = context.services.get("hooks")
            if hooks:
                intervention = await hooks.emit(ToolCallHook(call.id, call.name, call.arguments))
                if intervention and intervention.block:
                    result.status, result.content = "denied", intervention.reason or "Tool blocked"
                    return result
                if intervention and intervention.updated_args is not None:
                    call = ToolCall(call.id, call.name, intervention.updated_args)
            errors = list(Draft202012Validator(spec.schema).iter_errors(call.arguments))
            if errors:
                result.content = "Arguments do not satisfy the tool schema"
                return result
            read = (
                None
                if spec.execution_mode == "dispatch"
                else spec.execution_mode == "read" and spec.permission == "read"
            )
            async with self._slot(read):
                context.check_cancelled()
                if not await self._allowed(spec, call.arguments, context):
                    result.status, result.content = "denied", "Permission denied"
                    return result
                context.check_cancelled()
                if self.specs.get(call.name) is not spec or (tool_set is not None and not tool_set.validate(call.name)):
                    result.content = "Tool registration changed during authorization"
                    return result
                context.services["tools"] = self
                capture = context.services.get("replay_capture")
                if capture is not None:
                    capture.tool(call.name)
                if registration is not None:
                    registration.tasks.add(asyncio.current_task())
                self.session.append(
                    "tool_intent",
                    {"call_id": call.id, "name": call.name, "arguments": call.arguments},
                    checkpoint=True,
                )
                result.started = True
                emit = getattr(context, "emit", None)
                if emit:
                    await emit("tool_start", {"call_id": call.id, "name": call.name, "arguments": call.arguments})
                if prepared is not None:
                    prepared.set()
                token = ACTIVE_CALL.set((tool_set, call.name))
                value = spec.handler(call.arguments, context)
                value = await value if inspect.isawaitable(value) else value
                if isinstance(value, ToolResult):
                    result = value
                    result.call_id, result.name, result.started = call.id, call.name, True
                else:
                    result.content = (
                        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    )
                    result.structured = value if not isinstance(value, str) else None
                    result.status = "success"
                    result.effect = "none" if spec.permission == "read" else "completed"
        except asyncio.CancelledError:
            result.status, result.content = "cancelled", "Cancelled"
            result.effect = (
                "unknown"
                if result.started and spec is not None and spec.permission != "read"
                else "none"
            )
        except Exception as exc:
            result.status = "error"
            result.content = f"Tool failed ({type(exc).__name__})"
            result.effect = (
                "unknown"
                if result.started and spec is not None and spec.permission != "read"
                else "none"
            )
        finally:
            if token is not None:
                ACTIVE_CALL.reset(token)
            if registration is not None:
                registration.tasks.discard(asyncio.current_task())
            if prepared is not None:
                prepared.set()
        return result

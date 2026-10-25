"""子智能体层级派生、并发隔离与结构化交付物聚合引擎 (Hierarchical Subagent Concurrency & Delegation Runtime)。

【架构定位】
本模块负责管理复杂多智能体协作网络中的子任务委托与层级隔离。
主智能体可根据子任务性质将耗时调研、代码深层修改或工具试验下放给独立的子智能体，
实现长上下文解耦与局部风险隔离，并以标准结构化契约汇聚子任务产物与证据链。

【开源与工程设计借鉴】
1. 借鉴 DeepSeek-Harness (`packages/subagent/`) 的 Fork / Spawn 双模执行语义 (Dual-Mode Execution)：
   - `fork` 模式：深拷贝父级会话的消息历史快照、基线与已建立的目标，派生独立隔离会话，适合需要上下文背景的协同子任务；
   - `spawn` 模式：创建零历史污染的纯净沙箱环境，适合独立调研、代码构建或防止复杂探索破坏主上下文；
2. 有界并发与深度限制 (Bounded Concurrency & Depth Limit)：
   - 默认限制最大并发度为 3 (`max_concurrency=3`)，防止子智能体并发失控导致 API 限流或系统资源枯竭；
   - 递归调用深度硬性受限于 1 (`max_depth=1`)，彻底杜绝递归嵌套派生引发的死亡死循环；
3. 结构化交付物协议与证据链追踪 (`ChildResult`):
   - 归纳总结 `summary`、执行证据链 `evidence`（关联 `session:id#tool:call_id`）、生成的文件产物 `artifacts`、未竟事项 `unresolved` 以及 Token 消耗核算 `usage`；
4. 优雅取消与故障隔离 (`interrupt`, `cancel_owner`)：
   - 父轮次取消时级联终止所属所有子智能体，并在父会话收件箱中持久化记录取消状态，防止延迟回答破坏状态机。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from dataclasses import asdict
from pathlib import Path

from .contracts import ChildResult, new_id


class ChildManager:
    """子智能体生命周期与并发调度主控 (Child Agent Manager)。

    统筹管理子任务的创建（Spawn/Fork）、双向消息通信（Send）、等待终结（Wait）以及资源清理（Close）。
    """
    def __init__(self, parent_agent, max_concurrency=3, max_depth=1):
        """初始化子智能体并发调度管理器。

        【架构定位】
        负责主智能体对子任务的分派、并发限制、层级递归截断与交付物聚合。

        【设计思想与开源血统】
        深度借鉴 DeepSeek-Harness `packages/subagent` 有界并发模型：
        信号量限制最大并发度（默认 3），并硬性限制递归深度为 1 杜绝树状死循环。

        Args:
            parent_agent: 父级宿主 CodingAgent 实例。
            max_concurrency: 最大并发子智能体协程数，默认 3。
            max_depth: 允许派生的最大递归深度，默认 1（禁止间接派生）。

        Raises:
            ValueError: 当并发数小于 1 或深度小于 0 时抛出。
        """
        if max_concurrency < 1 or max_depth < 0:
            raise ValueError("Invalid child execution limits")
        self.parent = parent_agent
        self.max_depth = max_depth
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._tasks = {}
        self._results = []
        self._closed = False
        archived = {}
        for entry in getattr(parent_agent.session, "records", []):
            data = entry["data"]
            if entry["kind"] == "child_task":
                archived[data["task_id"]] = data
            elif entry["kind"] == "child_result" and data["task_id"] in archived:
                previous = archived[data["task_id"]]
                self._tasks[data["task_id"]] = {
                    **previous,
                    "result": copy.deepcopy(data),
                    "state": data["state"],
                    "child": None,
                    "task": None,
                    "queued": [],
                }

    def _view(self, record: dict) -> dict:
        """生成任务状态的安全只读视图字典。

        Args:
            record: 内部任务记录字典。

        Returns:
            dict: 浅拷贝或包含结果的展示字典。
        """
        if record["result"] is not None:
            return copy.deepcopy(record["result"])
        return {key: record[key] for key in ("task_id", "state", "owner", "mode", "role")}

    def list(self) -> list[dict]:
        """列举当前所有已知子智能体任务的状态摘要视图。

        Returns:
            list[dict]: 任务状态视图列表。
        """
        return [self._view(record) for record in self._tasks.values()]

    async def spawn(self, prompt: str, mode="spawn", background=False, role="", allowed_tools=None):
        """派生或分叉启动一个子智能体任务 (Spawn / Fork Child Agent)。

        【DSH Fork vs Spawn 执行模型】
        - `spawn`：初始化干净的新会话，具备独立空上下文；
        - `fork`：继承父级当前会话的所有历史消息、提示词基线与待办目标；
        - `background`：若为 True，立即返回挂起任务句柄；若为 False，阻塞等待执行完成并返回交付物。
        """
        if mode not in ("spawn", "fork"):
            raise ValueError("Child mode must be spawn or fork")
        if self.parent.depth >= self.max_depth:
            raise ValueError("Child depth limit reached")
        record = self._start(prompt, mode, role, allowed_tools)
        if background:
            return self._view(record)
        return await self.wait(record["task_id"], timeout=None)

    def _start(self, prompt, mode, role, allowed_tools, child=None):
        """构建子任务执行记录、注册持久化审计事件并创建异步协程任务。

        Args:
            prompt: 任务目标提示词。
            mode: 执行模式（"spawn" 或 "fork"）。
            role: 专职角色说明。
            allowed_tools: 限制开放的工具名称列表。
            child: 预先存在的子智能体实例（若已初始化）。

        Returns:
            dict: 初始化的内部任务记录对象。

        Raises:
            RuntimeError: 当管理器已处于关闭状态时抛出。
        """
        if self._closed:
            raise RuntimeError("Child manager is closed")
        context = self.parent.context
        record = {
            "task_id": new_id(),
            "state": "pending",
            "owner": context.id if context else None,
            "mode": mode,
            "role": role,
            "allowed_tools": copy.deepcopy(allowed_tools),
            "prompt": prompt,
            "child": child,
            "result": None,
            "task": None,
            "queued": [],
        }
        self.parent.session.append("child_task", {**self._view(record), "prompt": prompt}, True)
        self._tasks[record["task_id"]] = record
        record["task"] = asyncio.create_task(self._run(record))
        return record

    def _finish(self, record, state, summary="", usage=0, unresolved=None):
        """终态结算并持久化写入子智能体结构化交付物（ChildResult）。

        【证据链追踪与工件收集】
        从子会话历史中提取所有工具调用（`session_id#tool:call_id`）构建 evidence，
        从文件操作工具结果提取修改的文件物理路径生成 artifacts 交付物集合。

        Args:
            record: 内部任务记录字典。
            state: 终态标识（"completed", "failed", "cancelled"）。
            summary: 产出文本摘要。
            usage: 累计消耗 Token 计数。
            unresolved: 未解决问题或异常列表。
        """
        if record["result"] is not None:
            return
        record["state"] = state
        evidence, artifacts = [], []
        child = record.get("child")
        if child is not None:
            session = child.session
            session_id = getattr(session, "session_id", "child")
            evidence.append("session:" + session_id)
            records = getattr(session, "records", [])[record.get("history_start", 0) :]
            intents = {
                event["data"].get("call_id"): event["data"]
                for event in records
                if event["kind"] == "tool_intent"
            }
            for event in records:
                data = event["data"]
                if event["kind"] != "tool_result" or not data.get("started"):
                    continue
                evidence.append(f"{session_id}#tool:{data['call_id']}")
                if data.get("output_ref"):
                    artifacts.append(data["output_ref"])
                intent = intents.get(data["call_id"], {})
                path = intent.get("arguments", {}).get("path")
                if (
                    path
                    and data.get("status") == "success"
                    and data.get("name") in {"write_file", "edit_file"}
                ):
                    artifacts.append(str((Path(child.project) / path).resolve()))
        result = asdict(
            ChildResult(
                record["task_id"],
                state,
                summary=summary,
                evidence=evidence,
                artifacts=list(dict.fromkeys(artifacts)),
                unresolved=unresolved or [],
                usage=usage,
            )
        )
        if hasattr(self.parent.session, "record_child_result"):
            self.parent.session.record_child_result(result)
        else:
            self.parent.session.append("child_result", result, True)
        record["result"] = result
        self._results.append(copy.deepcopy(result))

    async def _run(self, record):
        """在信号量并发约束下执行子智能体生命周期循环。

        Args:
            record: 目标任务记录。
        """
        try:
            async with self._semaphore:
                if record["result"] is not None:
                    return
                record["state"] = "running"
                if record["child"] is None:
                    record["child"] = await self.parent.spawn_child(
                        record["mode"], record["role"], record["allowed_tools"]
                    )
                child = record["child"]
                child._delegation_task_id = record["task_id"]
                for kind, text in record["queued"]:
                    await self._deliver(child, text, kind)
                record["queued"].clear()
                before = len(getattr(child.session, "records", []))
                record["history_start"] = before
                summary = await child.run_once(record["prompt"])
                usage = self._usage(child, before)
                unfinished = [
                    entry["data"].get("state")
                    for entry in getattr(child.session, "records", [])[before:]
                    if entry["kind"] == "turn_end" and entry["data"].get("state") != "completed"
                ]
                self._finish(
                    record,
                    "failed" if unfinished else "completed",
                    str(summary),
                    usage,
                    unresolved=[f"Child turn ended {state}" for state in unfinished],
                )
        except asyncio.CancelledError:
            self._finish(record, "cancelled", unresolved=["Child turn was cancelled"])
        except Exception as exc:
            self._finish(record, "failed", unresolved=[type(exc).__name__])

    @staticmethod
    def _usage(child, start):
        """精确统计子任务轮次产生的 Token 消耗总和。

        Args:
            child: 子智能体实例。
            start: 记录起始索引。

        Returns:
            int: 消耗的 Token 总量。
        """
        records = getattr(child.session, "records", [])[start:]
        ledger = [record["data"] for record in records if record.get("kind") == "model_usage"]
        if ledger:
            return sum(
                data.get("accounted_tokens") or data.get("total_tokens") or 0 for data in ledger
            )
        total = 0
        for record in records:
            if record.get("kind") != "attempt_end":
                continue
            usage = record.get("data", {}).get("usage", 0)
            if isinstance(usage, int):
                total += usage
            elif isinstance(usage, dict):
                total += usage.get("total_tokens") or (
                    (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
                )
        return total

    async def wait(self, task_id, timeout=30):
        """阻塞等待指定子任务终结并返回结构化交付物。

        Args:
            task_id: 目标任务 ID。
            timeout: 超时秒数（None 表示无限等待）。

        Returns:
            dict: 结构化交付物视图。
        """
        record = self._tasks[task_id]
        if record["result"] is not None:
            return self._view(record)
        try:
            await asyncio.wait_for(asyncio.shield(record["task"]), timeout)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            if record["result"] is None:
                raise
        return self._view(record)

    async def _deliver(self, child, text, kind):
        """向运行中的子智能体异步投递引导或追问输入。

        Args:
            child: 子智能体实例。
            text: 消息内容。
            kind: 输入类型（"steer" 或 "follow_up"）。
        """
        result = getattr(child, "steer" if kind == "steer" else "follow_up")(text)
        if inspect.isawaitable(result):
            await result

    async def send(self, task_id, text, kind="follow_up"):
        """向指定子任务发送交互指令或开启后续轮次。

        Args:
            task_id: 目标任务 ID。
            text: 交互文本。
            kind: 发送模式（"follow_up" 或 "steer"）。

        Returns:
            dict: 最新任务视图。

        Raises:
            ValueError: 未知模式或子任务尚未初始化。
        """
        if kind == "steering":
            kind = "steer"
        if kind not in ("steer", "follow_up"):
            raise ValueError("Unknown child message kind")
        record = self._tasks[task_id]
        child = record["child"]
        if record["state"] in ("pending", "running"):
            if child is None:
                record["queued"].append((kind, text))
            else:
                await self._deliver(child, text, kind)
            return self._view(record)
        if child is None:
            raise ValueError("Child did not initialize")
        # Resolve historical task handles to the currently active turn of this child.
        active = next(
            (
                item
                for item in reversed(list(self._tasks.values()))
                if item["child"] is child and item["state"] in ("pending", "running")
            ),
            None,
        )
        if active is not None:
            await self._deliver(child, text, kind)
            return self._view(active)
        return self._view(
            self._start(text, record["mode"], record["role"], record["allowed_tools"], child)
        )

    async def interrupt(self, task_id):
        """协同中断指定子任务，并原子化写入取消终态防止延迟结果污染。

        Args:
            task_id: 待中断任务 ID。

        Returns:
            dict: 中断后的任务视图。
        """
        record = self._tasks[task_id]
        if record["result"] is not None:
            return self._view(record)
        # Mark durably before cancellation so a provider ignoring cancellation cannot publish a late answer.
        self._finish(record, "cancelled", unresolved=["Child turn was cancelled"])
        record["task"].cancel()
        child = record["child"]
        if child is not None:
            result = child.abort()
            if inspect.isawaitable(result):
                await result
        await asyncio.gather(record["task"], return_exceptions=True)
        return self._view(record)

    async def cancel_owner(self, parent_turn_id):
        """级联取消归属于指定父轮次的所有未决子智能体。

        Args:
            parent_turn_id: 父级轮次 ID。
        """
        await asyncio.gather(
            *(
                self.interrupt(key)
                for key, record in self._tasks.items()
                if record["owner"] == parent_turn_id and record["result"] is None
            )
        )

    def drain_results(self):
        """提取并清空父会话中所有待合并的子智能体交付物。

        Returns:
            list[dict]: 待确认的子智能体交付物结果列表。
        """
        # Production consumers acknowledge delivery only with the parent message commit.
        if hasattr(self.parent.session, "pending_child_results"):
            return self.parent.session.pending_child_results()
        results, self._results = self._results, []
        return results

    async def close(self):
        """级联关闭所有子任务并释放所有派生智能体资源。

        Raises:
            RuntimeError: 当清理过程发生未捕获异常时抛出。
        """
        self._closed = True
        errors = await asyncio.gather(
            *(self.interrupt(key) for key in self._tasks), return_exceptions=True
        )
        children = {
            id(record["child"]): record["child"]
            for record in self._tasks.values()
            if record["child"] is not None
        }
        errors += await asyncio.gather(
            *(child.close(close_runtime=False) for child in children.values()),
            return_exceptions=True,
        )
        if any(isinstance(error, BaseException) for error in errors):
            raise RuntimeError("Child cleanup failed after draining all children")

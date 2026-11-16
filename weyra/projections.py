"""纯函数领域状态重放与投影还原引擎 (Pure Session-Domain Projections & Reducers)。

【架构定位】(Architectural Role)
    位于 `weyra` 核心持久化与状态层，负责从底层 SQLite WAL 不可变事件日志流中，
    通过纯函数式状态规约（Pure Reducers）确定性还原各领域状态（消息视窗、输入双队列、模型路由、目标状态机、子任务）。
    Reducers 严格保持纯函数特性，绝不持有任何运行时资源、网络连接或产生外部副作用。

【开源设计思想与架构血统】(Design Pedigree)
    1. 遵循 Pi Agent 的不可变会话事件树（Session Tree DAG）投影模型：
       历史状态通过确定性事件折叠（Event Folding）计算得出，确保多分支、时间旅行与崩溃恢复的一致性；
    2. 借鉴 DeepSeek-Harness 的领域独立投影规约标准（Domain Projection Reducers）：
       细粒度解耦 messages, model, queues, goal, children 等独立业务切片，支持按需局部高效投影；
    3. 支持检查点（Checkpoint）快照与增量回放混合加速，保证长程会话状态还原的高性能。

【核心不变性与安全约束】(Invariants & Safety)
    - 纯函数无副作用：禁止在 Reducer 内部发起任何 I/O、网络请求或修改全局状态；
    - 幂等性与确定性：同一组事件输入必须产生完全一致的状态输出；
    - 严格不可变性：对外部状态做深拷贝（`deepcopy`）隔离，防止调用方意外篡改投影内部数据。
"""

from copy import deepcopy

VERSION = 1


def initial_state() -> dict:
    """构建会话领域状态的初始空结构快照。

    【架构定位】
    定义未消费任何事件时的全量会话领域默认结构。

    Returns:
        dict: 包含最新事件、双队列、子任务及消息序列的初始状态字典。
    """
    return {
        "latest": {},
        "queues": {"steering": [], "follow_up": []},
        "children": {},
        "messages": [],
    }


def apply_event(state: dict, record: dict) -> dict:
    """将单条持久化事件原子应用到会话状态聚合并返回最新聚合。

    【架构定位】
    多领域规约器的组合分发聚合器，驱动各独立领域单元执行状态递推。

    【开源设计思想与架构血统】
    保持与历史原地修改接口兼容的同时，复用 `SessionStore` 精细局部读取与检查点恢复的共享规约器。

    Args:
        state: 待更新的当前聚合状态字典。
        record: 单条持久化事件记录（包含 kind, data 等字段）。

    Returns:
        dict: 更新后的聚合状态字典。
    """
    for key in state:
        state[key] = UNITS[key].apply(state[key], record)
    return state


def replay(records: list[dict], state: dict | None = None) -> dict:
    """顺序重放事件流切片，还原出确定性的会话领域状态。

    【架构定位】
    事件溯源（Event Sourcing）核心回放驱动器，支持从零重放或从指定基线状态增量回放。

    Args:
        records: 有序事件记录列表。
        state: 可选的基准起始状态，缺省时自动使用 `initial_state()`。

    Returns:
        dict: 经全量事件规约后的终态聚合字典。
    """
    state = initial_state() if state is None else deepcopy(state)
    for record in records:
        apply_event(state, record)
    return state


class ProjectionUnit:
    """带版本控制的单一领域确定性状态规约单元 (Versioned Deterministic Domain Projection Unit)。

    【架构定位】
    封装针对特定业务领域（如消息、模型、队列、目标）的状态校验、初值分配与状态递推逻辑。
    """

    state_version = 1

    def __init__(self, key: str, kinds: set[str], initial):
        """初始化领域投影单元。

        Args:
            key: 领域唯一标识（如 'messages', 'queues', 'goal'）。
            kinds: 该领域关心的事件类型集合。
            initial: 该领域的初始默认值。
        """
        self.key, self.kinds, self.initial = key, frozenset(kinds), initial

    def init(self):
        """获取当前领域的初始隔离深拷贝状态。

        Returns:
            初始状态对象的独立副本。
        """
        return deepcopy(self.initial)

    def validate(self, state) -> bool:
        """严格校验当前领域状态对象的结构与字段类型契约。

        【核心不变性与安全约束】
        对各领域结构（如消息格式、队列内容、目标修订版本）施加严格契约断言，
        防止因数据损坏导致后续规约产生非预期行为。

        Args:
            state: 待校验的领域状态对象。

        Returns:
            bool: 结构合规返回 True，否则返回 False。
        """
        if self.key == "messages":
            return isinstance(state, list) and all(
                isinstance(m, dict)
                and m.get("role") in {"user", "assistant", "system", "tool"}
                and isinstance(m.get("content", ""), str)
                for m in state
            )
        if self.key == "model":
            return state is None or (
                isinstance(state, dict)
                and all(isinstance(state.get(k), str) for k in ("provider_id", "model_id"))
            )
        if self.key == "goal":
            return (
                state is None
                or state == {}
                or (
                    isinstance(state, dict)
                    and isinstance(state.get("objective"), str)
                    and type(state.get("revision", 1)) is int
                    and state.get("revision", 1) >= 1
                )
            )
        if self.key == "queues":
            return (
                isinstance(state, dict)
                and set(state) == {"steering", "follow_up"}
                and all(
                    isinstance(q, list)
                    and all(
                        isinstance(item, dict)
                        and all(isinstance(item.get(k), str) for k in ("id", "text"))
                        for item in q
                    )
                    for q in state.values()
                )
            )
        return isinstance(state, dict) and all(isinstance(v, dict) for v in state.values())

    def apply(self, state, record: dict):
        """根据单条事件演进当前领域状态。

        【架构定位】
        领域规约器的核心状态转移逻辑，处理消息追加/替换、双队列进出队、目标版本递增等。

        Args:
            state: 当前领域状态。
            record: 事件记录（包含 kind, data 等）。

        Returns:
            演进后的新领域状态副本。

        Raises:
            ValueError: 当目标创建序列不合法或修订版本不连续时抛出。
        """
        kind, data = record["kind"], record["data"]
        if record.get("_projectable") is False or (
            self.key != "latest" and kind not in self.kinds
        ):
            return state
        if self.key == "messages":
            return state + [deepcopy(data)] if kind == "message" else deepcopy(data["messages"])
        if self.key == "model":
            return deepcopy(data)
        if self.key == "goal":
            if kind == "goal_change":
                if data["operation"] == "create":
                    if data["revision"] != 1 or (state and state.get("state") != "completed"):
                        raise ValueError("Invalid goal creation sequence")
                elif (
                    not state
                    or data["revision"] != state.get("revision", 1) + 1
                    or (state.get("goal_id") and state["goal_id"] != data["goal_id"])
                ):
                    raise ValueError("Discontinuous goal revision")
            return deepcopy(data.get("snapshot", {}) if kind == "goal_change" else data)
        result = deepcopy(state)
        if self.key == "latest":
            result[kind] = deepcopy(data)
            if kind == "goal_change":
                result["goal"] = deepcopy(data["snapshot"])
        elif self.key == "queues":
            if kind == "input_queued":
                result.setdefault(data["kind"], []).append(deepcopy(data))
            else:
                result = {
                    key: [item for item in queue if item["id"] != data["id"]]
                    for key, queue in result.items()
                }
        elif self.key == "children":
            task = result.setdefault(data["task_id"], {})
            task.update(deepcopy(data))
            if kind == "child_result":
                task["result"] = deepcopy(data)
            elif kind == "child_result_ack":
                task["acknowledged"] = True
        return result

    def checkpoint(self, state):
        """校验并生成当前领域用于持久化检查点的安全快照。

        Args:
            state: 待固化的领域状态。

        Returns:
            校验通过的快照副本。

        Raises:
            ValueError: 当领域状态损坏时抛出。
        """
        if not self.validate(state):
            raise ValueError("Invalid projection state: " + self.key)
        return deepcopy(state)


UNITS = {
    "messages": ProjectionUnit("messages", {"message", "seed", "compaction"}, []),
    "model": ProjectionUnit("model", {"model_change"}, None),
    "queues": ProjectionUnit(
        "queues", {"input_queued", "input_consumed"}, {"steering": [], "follow_up": []}
    ),
    "goal": ProjectionUnit("goal", {"goal", "goal_change"}, None),
    "children": ProjectionUnit(
        "children", {"child_task", "child_result", "child_result_ack"}, {}
    ),
    "latest": ProjectionUnit("latest", set(), {}),
}

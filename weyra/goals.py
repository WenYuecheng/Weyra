"""事务性长期目标状态机与驱动服务 (Transactional Goal Mutation Service)。

【架构定位】(Architectural Role)
    位于 `weyra` 任务目标管理层，负责高阶宏观任务目标（Goal）、待办事项（Todos）的事务性演进、
    修订版本乐观并发控制（Revision CAS）与停滞自停状态流转。
    执行权限与策略判定严格受控，执行细节与外部副作用绝不直接侵入日志层。

【开源设计思想与架构血统】(Design Pedigree)
    1. 深度对齐 DeepSeek-Harness 的自主目标自治驱动理念（Goal-Driven Autonomy）：
       引入 `Goal` / `goal_change` 事件体系，支持长程任务状态（active, paused, completed, blocked）；
    2. 继承工业级乐观并发控制（Optimistic Concurrency Control）：
       通过 `GoalRef(goal_id, revision)` 确保每次状态变更基于预期前置版本，杜绝跨轮次竞态冲突；
    3. 停滞熔断保护（Stagnation Breaker）：支持最大尝试次数（`max_continuations`）与停滞轮次熔断。

【核心不变性与安全约束】(Invariants & Safety)
    - 事务原子性：所有目标变更操作必须在 `with session.transaction():` 事务上下文内提交；
    - 单调修订递增：每次目标变更 `revision` 必须严格等于上一版本加 1，杜绝版本跳跃或倒退；
    - 状态合法流转：已完成（`completed`）的目标严禁直接恢复，必须通过新建或重启进行新生命周期演进。
"""

from copy import deepcopy
from dataclasses import asdict, dataclass

from .contracts import digest, new_id
from .core.state import Goal


@dataclass(frozen=True)
class GoalRef:
    """目标实体版本引用快照 (Goal Version Reference)。

    【架构定位】
    用于乐观锁比较的轻量不可变实体，封装目标唯一标识与修订版本号。

    Attributes:
        goal_id: 目标唯一标识符。
        revision: 当前单调自增版本号（>= 1）。
    """

    goal_id: str
    revision: int

    def __post_init__(self):
        """校验目标版本引用合法性。

        Raises:
            ValueError: 当 goal_id 为空或 revision 不是正整数时抛出。
        """
        if (
            not isinstance(self.goal_id, str)
            or not self.goal_id
            or type(self.revision) is not int
            or self.revision < 1
        ):
            raise ValueError("Invalid goal reference")


class GoalError(ValueError):
    """目标状态转移与并发版本冲突异常 (Goal Mutation Error)。

    Attributes:
        code: 结构化错误代码（如 'goal_not_found', 'stale_goal_revision', 'invalid_goal_transition'）。
    """

    def __init__(self, code: str):
        """初始化目标操作异常。

        Args:
            code: 结构化错误标识码（如 'goal_not_found', 'stale_goal_revision' 等）。
        """
        self.code = code
        super().__init__(code)


class GoalService:
    """事务性目标管理服务门面 (Transactional Goal Management Service)。

    【架构定位】
    负责目标的创建、属性修订、状态机流转与事务性持久化。
    """

    def __init__(self, session, policy: dict | None = None):
        """初始化目标管理服务。

        Args:
            session: 底层持久化会话实例（SessionStore）。
            policy: 目标执行策略配置字典（如包含 'max_continuations'）。
        """
        self.session = session
        self.policy = dict(policy or {})
        self._armed = False

    def current(self) -> Goal | None:
        """从持久化会话中还原并获取当前活跃的目标实体。

        Returns:
            Goal | None: 还原后的目标实体，若当前会话无目标则返回 None。
        """
        data = self.session.goal_state()
        if not data:
            return None
        goal = Goal.restore(data)
        goal.goal_id = goal.goal_id or digest(self.session.session_id + ":legacy-goal")[:32]
        goal.armed = self._armed and goal.state == "active"
        return goal

    def ref(self) -> GoalRef:
        """获取当前活跃目标实体的版本引用。

        Returns:
            GoalRef: 包含目标 ID 与当前版本的引用对象。

        Raises:
            GoalError: 当当前无活跃目标时抛出 'goal_not_found'。
        """
        goal = self.current()
        if goal is None:
            raise GoalError("goal_not_found")
        return GoalRef(goal.goal_id, goal.revision)

    def _commit(self, expected: GoalRef, operation: str, change, armed: bool | None = None) -> Goal:
        """在独占事务中执行基于乐观锁版本校验的目标状态变更提交。

        【核心不变性与安全约束】
        1. 必须在 `with self.session.transaction():` 事务块内原子完成；
        2. 校验当前目标版本必须严格等于 `expected`，若已被其他操作递增则抛出版本陈旧错误；
        3. 变更后的 `revision` 必须自增 1，并将完整快照写入 `goal_change` 事件。

        Args:
            expected: 预期的当前目标版本引用。
            operation: 操作标识符（如 'create', 'update', 'resume', 'pause' 等）。
            change: 状态转换回调函数，接收上一版本深拷贝副本并返回修改后候选实体。
            armed: 是否激活就绪状态。

        Returns:
            Goal: 提交并递增版本后的新目标实体。

        Raises:
            GoalError: 当目标不存在或版本陈旧产生冲突时抛出。
        """
        with self.session.transaction():
            current = self.current()
            if current is None:
                raise GoalError("goal_not_found")
            if GoalRef(current.goal_id, current.revision) != expected:
                raise GoalError("stale_goal_revision")
            candidate = change(deepcopy(current))
            candidate.goal_id, candidate.revision = current.goal_id, current.revision + 1
            self.session.append(
                "goal_change",
                {
                    "goal_id": candidate.goal_id,
                    "revision": candidate.revision,
                    "operation": operation,
                    "snapshot": asdict(candidate),
                },
                True,
            )
        self._armed = candidate.armed if armed is None else armed
        candidate.armed = self._armed and candidate.state == "active"
        return candidate

    def create(self, objective: str, todos: list[dict] | None = None) -> Goal:
        """创建全新任务目标并持久化初始状态。

        【核心不变性与安全约束】
        若已有处于未完结态的目标，严禁重复创建新目标，必须等待旧目标完成或显式终结。

        Args:
            objective: 目标核心意图描述文本。
            todos: 可选的细粒度待办事项列表。

        Returns:
            Goal: 初始化生成的目标实体对象。

        Raises:
            GoalError: 当当前存在尚未完成的目标时抛出 'invalid_goal_transition'。
        """
        if self.current() is not None and self.current().state != "completed":
            raise GoalError("invalid_goal_transition")
        goal = Goal(
            objective, todos=todos or [{"text": objective, "done": False}], goal_id=new_id()
        )
        with self.session.transaction():
            if self.current() is not None and self.current().state != "completed":
                raise GoalError("invalid_goal_transition")
            self.session.append(
                "goal_change",
                {
                    "goal_id": goal.goal_id,
                    "revision": goal.revision,
                    "operation": "create",
                    "snapshot": asdict(goal),
                },
                True,
            )
        self._armed = True
        return goal

    def update(self, expected: GoalRef, candidate: Goal, operation: str = "update") -> Goal:
        """将当前目标更新为指定的候选目标对象。

        Args:
            expected: 预期的基线目标版本。
            candidate: 拟更新的目标对象。
            operation: 操作类型名称，默认为 'update'。

        Returns:
            Goal: 提交后的目标实体对象。
        """
        return self._commit(expected, operation, lambda previous: deepcopy(candidate))

    def mutate(self, expected: GoalRef, operation: str, **values) -> Goal:
        """驱动目标执行标准状态转移变更（恢复、重启、修订、暂停、完成、阻塞）。

        【状态转移逻辑】
        - `resume`: 恢复暂停或阻塞的目标至活跃态，受最大继续次数约束；
        - `restart` / `revise`: 重置停滞与继续计数，激活新目标；
        - `pause` / `complete` / `block`: 将目标流转至终止态或挂起态，解除就绪激活。

        Args:
            expected: 预期的目标基准版本引用。
            operation: 状态转移操作（'resume', 'restart', 'revise', 'pause', 'complete', 'block'）。
            **values: 传递给操作的动态参数（如 objective, todos, code, reason 等）。

        Returns:
            Goal: 流转完成后的新版本目标实体。

        Raises:
            GoalError: 当状态流转不合法或达到继续上限时抛出。
        """

        def change(goal):
            """对目标实体执行具体状态转移与字段变更的纯函数操作。"""
            if operation == "resume":
                if goal.state == "completed":
                    raise GoalError("invalid_goal_transition")
                if goal.continuations >= self.policy.get("max_continuations", 3):
                    raise GoalError("goal_limit_reached")
                goal.state, goal.armed = "active", True
                goal.blocker_code = goal.blocker_reason = ""
            elif operation in {"restart", "revise"}:
                if operation == "revise":
                    goal.objective = values["objective"]
                    goal.todos = values.get("todos") or [{"text": goal.objective, "done": False}]
                goal.continuations = goal.stagnant_rounds = 0
                goal.progress = goal.blocker_code = goal.blocker_reason = ""
                goal.state, goal.armed = "active", True
            elif operation in {"pause", "complete", "block"}:
                goal.state = {
                    "pause": "paused",
                    "complete": "completed",
                    "block": "blocked",
                }[operation]
                goal.armed = False
                if operation == "block":
                    goal.blocker_code = values.get("code", "execution-failed")
                    goal.blocker_reason = values.get("reason", "")
            else:
                raise GoalError("invalid_goal_transition")
            return goal

        return self._commit(expected, operation, change)

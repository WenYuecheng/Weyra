"""受管持久化事件与扩展事件的信封模式与校验规范 (Owned Event Schema & Validation)。

【架构定位】(Architectural Role)
    位于 `weyra` 底层事件契约层，负责校验所有写入与回放的持久化事件信封合规性。
    在事件进入 SQLite WAL 或从日志回放前执行强类型与字段完整性校验，杜绝非法脏数据污染底层日志树。

【开源设计思想与架构血统】(Design Pedigree)
    1. 深度对齐 DeepSeek-Harness 的事件模式版本契约（Event Schema Versioning）：
       显式区分受管核心事件（OWNED）与可选扩展事件，严格验证信封（Envelope）与必填载荷；
    2. 继承 Pi Agent 事件日志不可变设计，所有受管事件必须具备确定性的载荷格式；
    3. 支持渐进式版本演进，通过 `ignorable` 标记控制非关键未知事件的安全降级。

【核心不变性与安全约束】(Invariants & Safety)
    - 快速失败（Fail-Fast）：核心载荷缺失必填字段时立即抛出 `ValueError`，杜绝静默吞掉异常；
    - 信封单调版本：当前支持版本为 1，非忽略的未知版本坚决阻断；
    - 状态机强约束：对 `goal_change`、`input_queued`、`message` 等关键事件施加严格的状态与字段枚举校验。
"""

OWNED = frozenset((
    "attempt_end",
    "attempt_start",
    "baseline",
    "branch",
    "budget_checkpoint",
    "budget_settlement",
    "child_result",
    "child_result_ack",
    "child_task",
    "cleanup_errors",
    "compaction",
    "compression_failure",
    "context_measurement",
    "context_version",
    "environment",
    "evaluation_case",
    "evaluation_case_start",
    "evaluation_group",
    "evaluation_report",
    "evolution_result",
    "goal",
    "goal_change",
    "input_consumed",
    "input_queued",
    "job_cancel",
    "job_start",
    "job_status",
    "job_transfer",
    "mcp_trust",
    "memory_recall",
    "message",
    "model_change",
    "model_request",
    "model_route_change",
    "model_usage",
    "pending_window",
    "project_rules",
    "resource_context",
    "seed",
    "session",
    "skill_evolution",
    "skill_judge",
    "skill_proposal",
    "skill_validation",
    "summary_segment",
    "tool_intent",
    "tool_result",
    "tool_result_view",
    "turn_end",
    "turn_start",
))


def validate_event(kind: str, data: dict, version: int = 1, ignorable: bool = True) -> bool:
    """校验持久化事件信封与载荷字段的完整性与合法性。

    【架构定位】
    事件写入与反序列化边界的核心校验器，拦截格式破损、缺失必填字段或版本不兼容的事件。

    【开源设计思想与架构血统】
    遵循 DeepSeek-Harness 的事件网关校验规范，针对不同的事件类型分别施加细粒度字段断言，
    保证进入 SessionStore 的每一个事件均满足不可变树的重放前置条件。

    【核心不变性与安全约束】
    - 若事件为受管核心事件（OWNED）且版本支持，校验载荷结构并返回 True；
    - 若事件非受管或版本不受支持：若 `ignorable=True` 则安全忽略返回 False，若 `ignorable=False` 则强阻断抛出异常。

    Args:
        kind: 事件类型标识符（如 'message', 'goal_change', 'input_queued'）。
        data: 事件载荷字典。
        version: 事件模式版本（默认 1）。
        ignorable: 当遇到未知事件或不支持版本时，是否允许安全忽略而不抛出异常。

    Returns:
        bool: 校验通过返回 True；若为可忽略的外部未知事件则返回 False。

    Raises:
        ValueError: 当信封格式不合规、版本不合法、不支持不可忽略事件或必填字段缺失时抛出。
    """
    if not isinstance(kind, str) or not kind or not isinstance(data, dict):
        raise ValueError("Invalid event envelope")
    if type(version) is not int or version < 1 or type(ignorable) is not bool:
        raise ValueError("Invalid event version declaration")
    if kind not in OWNED or version != 1:
        if not ignorable:
            raise ValueError("Unsupported required event version")
        return False
    required = {
        "message": ("role",),
        "seed": ("messages",),
        "compaction": ("messages",),
        "input_queued": ("id", "kind", "text"),
        "input_consumed": ("id",),
        "child_task": ("task_id",),
        "child_result": ("task_id",),
    }
    if any(key not in data for key in required.get(kind, ())):
        raise ValueError("Invalid owned event payload: " + kind)
    if kind in {"seed", "compaction"} and not isinstance(data["messages"], list):
        raise ValueError("Invalid message projection")
    if kind == "message":
        if data["role"] not in {"user", "assistant", "system", "tool"} or not isinstance(
            data.get("content", ""), str
        ):
            raise ValueError("Invalid message payload")
    if kind == "input_queued" and (
        data["kind"] not in {"steering", "follow_up"}
        or any(not isinstance(data[k], str) for k in ("id", "text"))
    ):
        raise ValueError("Invalid queued input")
    if kind == "goal" and data and not isinstance(data.get("objective"), str):
        raise ValueError("Invalid goal objective")
    if kind == "goal_change":
        if (
            data.get("operation")
            not in {
                "create",
                "revise",
                "restart",
                "resume",
                "pause",
                "complete",
                "block",
                "update",
            }
            or not isinstance(data.get("goal_id"), str)
            or not data["goal_id"]
            or type(data.get("revision")) is not int
            or data["revision"] < 1
            or not isinstance(data.get("snapshot"), dict)
            or data["snapshot"].get("goal_id") != data["goal_id"]
            or data["snapshot"].get("revision") != data["revision"]
        ):
            raise ValueError("Invalid goal change envelope")
    return True

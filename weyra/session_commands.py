"""会话作用域交互指令集与斜杠命令派发器 (Session Slash Commands Dispatcher)。

【架构定位】
本模块定义了 CLI 终端交互与 Textual TUI 共用的统一斜杠控制命令系统（Slash Commands）。
在人机协同（Human-in-the-Loop）开发中，用户需要穿透底层的模型自动生成循环，对 Agent 运行时的
分支状态、队列插队、长期记忆、技能版本、成本预算与执行模式进行确定性的控制。
本模块确保命令行界面与图形 TUI 具备完全对齐、零偏差的语义控制策略（Identical Policy & Behavior）。

【设计思想与开源血统】
1. 深度借鉴 Pi Agent (packages/coding-agent) 的会话交互与斜杠命令体系：
   - 树形分支控制指令（/branch, /fork）：依托 Pi 的 Session Tree 树形日志，支持在历史断点开辟分支或派生独立子会话；
   - 动态交互双队列机制：通过 `/steer` 向引导队列压入高优先级抢占式干预指令，通过 `/followup` 向追问队列压入
     排队任务，赋予用户在模型长程思考中实时纠偏的能力；
   - 事务级会话原子重置（/clear）：在事务内安全消费清理未决输入并写入 `user_clear` 检查点；
   - 并发状态栅栏（Busy Guard）：在命令执行前严格检查 `busy = agent._running`，防止在正在进行的模型推演或
     工具执行过程中并发篡改会话树分支、技能版本或触发上下文清理，杜绝状态机脑裂。
2. 深度借鉴 DeepSeek-Harness (packages/plan, packages/goal, packages/compaction) 的企业级控制指令：
   - 两阶段计划模式切换（/plan [on|off]）：在只读/审批模式（plan）与自动执行模式（default）间平滑切换；
   - 长期自治目标驱动指令（/goal）：查看与更新当前会话的长期自治推进目标与待办事项；
   - 8 段式结构化检查点压缩指令（/compact）：按需主动触发上下文压缩与清理。
3. 借鉴 SkillOpt (arXiv:2605.23904, 2026) 的技能演化审计：
   - 技能语义版本回滚（/skill-rollback）与演化审计追踪（/skill-history）。
4. 会话物理路径寻址与合法性校验：
   - 原生支持 "latest" 特殊标识快速恢复最新会话，并对会话 ID 字符进行形式化正则安全校验。

【核心不变性与安全约束】
- 运行时并发互斥：活跃轮次（active turn）运行期间，严禁执行破坏性状态变更指令（如 /branch, /fork, /clear, /compact）。
- 金融级成本可信度：成本结算基于 Python `Decimal` 高精度计算，未完成调用或未定价模型显式标记未决风险。
"""

import re
import shlex
from decimal import Decimal


def budget_summary(budget):
    """提取高精度金融级预算消耗与未决成本摘要。

    【算法机制】
    基于 `decimal.Decimal` 统计已记账调用次数、Token 消耗以及预扣押金成本，
    当且仅当存在明确的定价模型且未知状态调用为 0 时，标记成本绝对可信（`is_cost_reliable=True`）。

    Args:
        budget: 宿主 Budget 账本实例。

    Returns:
        dict: 包含 calls, tokens, pending_tokens, unknown_calls, cost_cny, pending_cost_cny, is_cost_reliable 的摘要字典。
    """
    pending = getattr(budget, "_pending", {})
    return {
        "calls": budget.calls,
        "tokens": budget.total_tokens,
        "pending_tokens": budget.pending_tokens,
        "unknown_calls": budget.unknown_calls,
        "cost_cny": (
            str(budget.total_cost_cny)
            if getattr(budget, "_pricing", None) is not None or budget.total_cost_cny
            else None
        ),
        "pending_cost_cny": str(
            sum((Decimal(str(v.get("cost", 0))) for v in pending.values()), Decimal(0))
        ),
        "is_cost_reliable": (
            getattr(budget, "_pricing", None) is not None and budget.unknown_calls == 0
        ),
    }


def session_path(project, identifier, *, existing=True):
    """解析并校验会话标识符或物理存储路径。

    【执行流程】
    1. 特殊别名处理：若为 "latest"，自动在项目 state.db 中查询最后更新的会话 ID；
    2. 安全字符校验：正则强校验 `^[A-Za-z0-9_-]+(\\.jsonl)?$`，防止路径穿越攻击；
    3. 会话存在性核对：在 state.db 或历史 `.weyra/sessions/` 目录中定位物理存在；
    4. 边界抛错：若要求已存在 (`existing=True`) 但未定位到则抛出 ValueError。

    Args:
        project: 项目根目录 Path 对象。
        identifier: 会话 ID 字符串、'latest' 或遗留会话文件路径。
        existing: 是否要求目标会话必须已存在，默认为 True。

    Returns:
        str | Path: 解析后的标准会话 ID 字符串或历史会话文件 Path。

    Raises:
        ValueError: 当会话标识非法、不存在或试图创建已存在的同名会话时抛出。
    """
    from .session import SessionStore

    if identifier == "latest":
        rows = SessionStore.list_sessions(project)
        if not rows:
            raise ValueError("No session to resume")
        return rows[0]["session_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.jsonl)?", identifier):
        raise ValueError("Invalid session identifier")
    rows = SessionStore.list_sessions(project)
    if identifier in {row["session_id"] for row in rows}:
        if not existing:
            raise ValueError("Session already exists")
        return identifier
    legacy = project / ".weyra" / "sessions" / identifier
    if not legacy.suffix:
        legacy = legacy.with_suffix(".jsonl")
    if existing:
        if legacy.is_file() and legacy.resolve().is_relative_to(project):
            return legacy
        raise ValueError("Session does not exist")
    return identifier


async def run_session_command(agent, line, output):
    """统一解析并执行交互式斜杠控制命令 (Slash Commands Dispatch)。

    【命令体系与执行规则】
    1. /cancel: 协同中断当前正在执行的轮次；
    2. /steer [text]: 抢占式向引导队列注入即时干预文本；
    3. /followup [text]: 顺序向追问队列追加排队任务；
    4. /branch [id]: 在指定历史节点开辟新分支（忙碌时互斥）；
    5. /fork: 派生独立子会话沙箱（忙碌时互斥）；
    6. /clear: 原子清理未决输入并写入 user_clear 检查点（忙碌时互斥）；
    7. /plan [on|off]: 切换 DSH 两阶段确认计划模式；
    8. /goal [text]: 查看或更新 DSH 自治长期目标；
    9. /compact: 主动触发 DSH 八段式结构化压缩；
    10. /budget: 打印金融级精度成本与 Token 消耗审计报表；
    11. /skill-*: 技能回滚与演化审计。

    Args:
        agent: 当前交互的 CodingAgent 宿主实例。
        line: 包含斜杠命令的原始用户输入字符串。
        output: 响应输出回调函数（接受单字符串入参）。

    Raises:
        ValueError: 当参数语法错误、状态机并发冲突或遇到未知命令时抛出。
    """
    parts = shlex.split(line)
    name, args = parts[0], parts[1:]
    busy = agent._running
    # 并发安全检查：正在执行时禁止篡改会话状态
    if name == "/mcp-trust" and busy:
        raise ValueError("Wait for the active turn before trusting an MCP server")
    if name in {"/branch", "/fork", "/refresh", "/skill-rollback", "/goal", "/plan", "/compact", "/clear"} and busy:
        raise ValueError("Wait for the active turn before changing session state")
    if name == "/cancel":
        agent.abort()
    elif name in {"/steer", "/followup"}:
        if not args:
            raise ValueError(f"Usage: {name} <text>")
        text = " ".join(args)
        if name == "/steer":
            agent.steer(text)
            output("Queued steering input.")
        else:
            agent.follow_up(text)
            output("Queued follow-up input.")
    elif name == "/plan":
        if not args:
            mode = "on" if agent.permission_mode == "plan" else "off"
            output(f"Plan mode is {mode}.")
            return
        mode = args[0].lower()
        if mode not in {"on", "off"}:
            raise ValueError("Usage: /plan [on|off]")
        if mode == "on":
            if agent.permission_mode != "plan":
                agent._permission_before_plan = agent.permission_mode
            agent.permission_mode = "plan"
        elif agent.permission_mode == "plan":
            agent.permission_mode = getattr(agent, "_permission_before_plan", "default")
        agent.tools.permission_mode = agent.permission_mode
        output(f"Plan mode turned {mode}.")
    elif name == "/branch":
        point = args[0] if args else agent.session.leaf
        if not point:
            raise ValueError("No branch point available")
        branch_id = agent.branch(point)
        output(f"Created branch {branch_id} from {point}.")
    elif name == "/fork":
        if args:
            child = agent.session.fork(session_path(agent.project, args[0], existing=False))
            output(child.session_id)
            child.close()
        else:
            forked = agent.session.fork()
            try:
                output(forked.session_id)
            finally:
                forked.close()
    elif name == "/tree":
        output(
            [
                {"id": r["id"], "parent": r["parent"], "kind": r["kind"]}
                for r in agent.session.records
            ]
        )
    elif name == "/memory":
        if not args:
            output(agent.memory.list() if getattr(agent, "memory", None) else [])
        elif args[0] == "save" and len(args) == 3:
            if busy or agent.permission_mode in {"plan", "dontAsk"}:
                raise ValueError("Memory writes unavailable in the current state")
            output(agent.memory.update(args[1], args[2]))
        else:
            output(agent.memory.search(" ".join(args)) if getattr(agent, "memory", None) else [])
    elif name == "/skills":
        catalog = agent.skills.capture_catalog() if getattr(agent, "skills", None) else {}
        output(
            [
                {"name": s.name, "version": s.version, "description": s.description}
                for s in catalog.values()
            ]
        )
    elif name == "/clear":
        agent.clear_session()
        output("Cleared session context.")
    elif name == "/compact":
        applied = await agent.compact()
        output("Compacted context." if applied else "Context did not require compaction.")
    elif name == "/goal":
        if args:
            description = " ".join(args)
            agent.set_goal(description)
            output(f"Updated goal: {description}")
        else:
            goal = agent.goal
            if goal and goal.description:
                output(f"Active goal: {goal.description}")
            else:
                output("No active goal.")
    elif name == "/usage":
        output(
            {
                "calls": agent.budget.calls,
                "tokens": agent.budget.total_tokens,
                "pending_tokens": agent.budget.pending_tokens,
                "unknown_calls": agent.budget.unknown_calls,
            }
        )
    elif name == "/budget":
        summary = budget_summary(agent.budget)
        reliable_text = " (verified)" if summary["is_cost_reliable"] else " (estimated/partial)"
        cost_line = f"Cost: ¥{summary['cost_cny'] or '0.000000'}{reliable_text}"
        output(f"Calls: {summary['calls']} | Tokens: {summary['tokens']} | {cost_line}")
    elif name == "/refresh":
        if getattr(agent, "skills", None) and hasattr(agent.skills, "_refresh"):
            agent.skills._refresh()
        if getattr(agent, "memory", None) and hasattr(agent.memory, "_refresh"):
            agent.memory._refresh()
        if hasattr(agent, "context_manager") and hasattr(agent, "_system"):
            catalog = agent.skills.capture_catalog() if getattr(agent, "skills", None) else {}
            agent.context_manager.refresh(agent._system(catalog))
        output("Refreshed skills and memory manifests.")
    elif name == "/skill-rollback":
        if not args:
            raise ValueError("Usage: /skill-rollback <skill_name> [version]")
        if agent.permission_mode in {"plan", "dontAsk"}:
            raise ValueError("Skill writes unavailable in the current mode")
        skill_name = args[0]
        version = (int(args[1]) if args[1].isdigit() else args[1]) if len(args) > 1 else None
        if hasattr(agent, "rollback_skill"):
            res = agent.rollback_skill(skill_name, version)
            if hasattr(res, "__await__"):
                res = await res
            output(res)
        else:
            record = agent.evolution.rollback_skill(skill_name, version=version)
            output(record)
    elif name == "/skill-history":
        if not args:
            raise ValueError("Usage: /skill-history <skill_name>")
        skill_name = args[0]
        history = agent.evolution.history(skill_name) if getattr(agent, "evolution", None) else []
        if not history:
            output(f"No evolution history for {skill_name}.")
        else:
            lines = [f"History for {skill_name}:"]
            for h in history:
                lines.append(f"  v{h['version']} [{h['timestamp']}] parent={h['parent_version']} hash={h['code_hash'][:8]}")
            output("\n".join(lines))
    elif name == "/mcp":
        if getattr(agent, "mcp", None):
            output(
                [
                    {
                        "name": mcp_name,
                        "command": client.command,
                        "args": list(client.args),
                        "trusted": client.trusted,
                    }
                    for mcp_name, client in sorted(agent.mcp.clients.items())
                ]
            )
        else:
            output([])
    elif name == "/mcp-trust":
        if not args:
            raise ValueError("Usage: /mcp-trust <server_name>")
        server_name = args[0]
        if getattr(agent, "mcp", None):
            client = agent.mcp.clients.get(server_name)
            if client is None:
                raise ValueError("Unknown MCP server")
            result = agent.trust_mcp(server_name, authorized=True) if hasattr(agent, "trust_mcp") else agent.mcp.trust_server(server_name)
            if hasattr(result, "__await__"):
                result = await result
            output(result)
        else:
            raise ValueError("No MCP manager available")
    elif name == "/help":
        output(
            "/steer text; /followup text; /cancel; /goal text; /tree; /branch record; /fork name; /memory [query | save name \"content\"]; /skills; /skill-history name; /skill-rollback name version; /refresh; /usage; /mcp; /mcp-trust name; /quit"
        )
    else:
        raise ValueError(f"Unknown session command: {name}")

"""Weyra 生产与运维命令中心（Operational Command Dispatcher）。

架构定位与设计哲学:
    本模块负责处理非交互式的生产与运维指令（Operational Subcommands）。
    包括离线/在线场景演示（demo）、系统级确定性回归评测（eval）、配置就绪前置检查（check-config）
    以及跨机器会话导入导出（export-session / import-session）。
    本模块确保所有运维命令与主交互会话共享完全相同的核心运行时（ModelRuntime）、会话状态机（SessionStore）
    以及金融级预算记账引擎（Budget），保证评测与实操的一致性。

开源框架借鉴与设计考量:
    1. DeepSeek-Harness (packages/session-query & scripts/):
       - 借鉴其离线会话可移植性设计（Session Portability）：支持通过标准 JSONL 格式导入与导出
         复杂的多轮会话历史记录（export-session / import-session），使得故障重放（Failure Replay）
         与跨开发环境复现无需依赖特定的 SQLite 文件锁或底层数据库状态。
       - 借鉴其大厂防御性预检规范（Fail-loud Pre-flight Check）：执行任何联网任务前强制调用
         `check_config` 进行配置段白名单校验，在发起昂贵的大模型 API 请求前阻断配置错误。
       - 严格的资源生命周期闭环（Resource Cleanup）：在 `finally` 块中级联销毁运行时（runtime.close()）
         并显式释放 SQLite 独占文件锁（journal.close()），根除连接与句柄泄露。
    2. PiAgent:
       - 严格区分离线模式（--offline）与在线模式（Live Execution），离线模式下无缝注入确定性模拟
         提供者（ScriptedProvider），实现 0 费用、0 外部依赖的快速 CI 行为验证。
"""

import argparse
import json
from pathlib import Path

from .config import check_config, load_config
from .runtime_state import Budget
from .session import SessionStore

# 支持的标准化运维命令集合
COMMANDS = {"demo", "eval", "check-config", "sessions", "export-session", "import-session"}


def parser():
    """构建运维子命令的命令行参数解析器。

    【架构定位】
    定义 Weyra 生产运维子命令（Operational Subcommands）的 CLI 参数模式。
    支持评测套件运行、会话跨机器迁移、配置合规性检查与场景化演示。

    Returns:
        argparse.ArgumentParser: 配置就绪的命令行参数解析器实例。
    """
    p = argparse.ArgumentParser(prog="weyra")
    p.add_argument("command", choices=sorted(COMMANDS))
    p.add_argument("--project", type=Path, default=Path.cwd())
    p.add_argument("--provider")
    p.add_argument("--model")
    p.add_argument("--scenario", choices=["analysis", "writing"], default="analysis")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--group", action="append")
    p.add_argument("--session-id")
    p.add_argument("--file", type=Path)
    p.add_argument("--max-cost-cny", default="20")
    p.add_argument("--budget-id", default="validation-live-cny20")
    p.add_argument("--max-calls", type=int)
    p.add_argument("--max-tokens", type=int)
    return p


async def run(args):
    """异步执行具体的运维子命令。

    【架构定位】
    非交互式命令行任务的中央调度器。
    统筹配置预检（check_config）、会话数据离线导出与导入（export-session / import-session）、
    回归基准评测（evaluation）以及离线/在线场景演示（demos）。

    【设计思想与开源血统】
    1. 借鉴 DeepSeek-Harness 工业级运维工具链设计：
       - 会话可移植性（Session Portability）：通过 JSONL 文件原子导入导出多轮 DAG 会话；
       - 大厂防御性预检（Fail-loud Pre-flight Check）：联网前强制执行配置段白名单校验；
       - 金融级记账与严格资源闭环：在 `finally` 块中级联销毁运行时并释放 SQLite 独占锁。
    2. 借鉴 Pi Agent：严格支持 `--offline` 快速离线断言，注入 ScriptedProvider 降低测试成本。

    Args:
        args: 解析完成的命令行参数命名空间。

    Returns:
        dict | list: 命令执行结果结构体。

    Raises:
        ValueError: 缺少必要参数（如缺少 `--file`）或配置格式异常。
    """
    project = args.project.resolve()
    overrides = {key: getattr(args, key) for key in ("provider", "model") if getattr(args, key)}
    # 静态配置检查命令
    if args.command == "check-config":
        return check_config(project, overrides)
    # 查看现有持久化会话列表
    if args.command == "sessions":
        return SessionStore.list_sessions(project)
    # 会话可移植性导入与导出
    if args.command in {"export-session", "import-session"}:
        if args.file is None:
            raise ValueError("--file is required")
        if args.command == "import-session":
            store = SessionStore(args.file, project)
            try:
                return {"session_id": store.session_id, "imported": True}
            finally:
                store.close()
        store = SessionStore(project=project, session_id=args.session_id)
        try:
            store.export_jsonl(args.file)
            return {"session_id": store.session_id, "exported": str(args.file)}
        finally:
            store.close()

    # 初始化金融级预算配额
    runtime = None
    budget = Budget(
        token_limit=args.max_tokens,
        call_limit=args.max_calls,
        cost_limit_cny=None if args.offline else args.max_cost_cny,
    )
    journal = None
    try:
        if not args.offline:
            check_config(project, overrides)
            runtime = load_config(project, overrides).build_runtime()
            journal = SessionStore(project=project, session_id=args.budget_id)
            budget.attach(journal)
        if args.command == "demo":
            from .demos import run_demo

            if not args.offline:
                budget.token_limit = budget.total_tokens + min(
                    20000 if args.max_tokens is None else args.max_tokens, 20000
                )
                budget.call_limit = budget.calls + min(
                    8 if args.max_calls is None else args.max_calls, 8
                )
            result = await run_demo(
                args.scenario,
                provider=args.provider,
                offline=args.offline,
                project=project,
                runtime=runtime,
                budget=budget,
                owns_runtime=False,
            )
        else:
            from .evaluation import run_evaluation

            result = await run_evaluation(
                offline=args.offline,
                runtime=runtime,
                budget=budget,
                project=project,
                groups=args.group,
                owns_runtime=False,
            )
        # 统一注入本次运行的精确账本消耗指标
        if isinstance(result, dict):
            result.setdefault(
                "budget",
                {
                    "id": args.budget_id if not args.offline else None,
                    "calls": budget.calls,
                    "tokens": budget.total_tokens,
                    "accounted_cost_cny": str(budget.total_cost_cny),
                    "unknown_calls": budget.unknown_calls,
                },
            )
        return result
    finally:
        # 严格闭环释放资源，避免子进程与数据库文件锁残留
        try:
            if runtime is not None:
                await runtime.close()
        finally:
            if journal is not None:
                journal.close()


def render(result):
    """标准化格式化输出 JSON 执行报告至标准输出。

    【架构定位】
    确保所有运维指令的标准输出具备确定性、UTF-8 字符安全以及机器可读性（Machine-readable stdout）。

    Args:
        result: 待打印的字典、列表或原始结果对象。
    """
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2), flush=True)


"""Operational commands using the same runtime, state and budget as chat."""

import argparse
import json
from pathlib import Path

from .config import check_config, load_config
from .runtime_state import Budget
from .session import SessionStore

COMMANDS = {"demo", "eval", "check-config", "sessions", "export-session", "import-session"}


def parser():
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
    project = args.project.resolve()
    overrides = {key: getattr(args, key) for key in ("provider", "model") if getattr(args, key)}
    if args.command == "check-config":
        return check_config(project, overrides)
    if args.command == "sessions":
        return SessionStore.list_sessions(project)
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
        try:
            if runtime is not None:
                await runtime.close()
        finally:
            if journal is not None:
                journal.close()


def render(result):
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2), flush=True)

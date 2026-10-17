"""Weyra command line with one asynchronous input owner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .agent import Agent
from .config import load_config
from .contracts import ModelInfo, ModelResponse, ToolCall, Usage
from .model_runtime import ModelError, ModelRuntime, ProviderRegistry, ScriptedProvider
from .runtime_state import Budget


def parser():
    result = argparse.ArgumentParser(prog="weyra", description="Weyra project agent")
    result.add_argument("prompt", nargs="?")
    result.add_argument("--project", type=Path, default=Path.cwd())
    result.add_argument("--provider")
    result.add_argument("--protocol", choices=["openai", "anthropic"])
    result.add_argument("--model")
    result.add_argument("--context-window", type=int)
    result.add_argument("--max-output-tokens", type=int)
    result.add_argument("--base-url")
    result.add_argument("--api-key-env")
    result.add_argument("--resume", nargs="?", const="latest")
    result.add_argument(
        "--permission-mode",
        choices=["default", "plan", "acceptEdits", "dontAsk", "bypassPermissions"],
        default="default",
    )
    result.add_argument("--max-tokens", type=int, default=200000)
    result.add_argument("--max-calls", type=int, default=50)
    result.add_argument("--max-steps", type=int, default=40)
    result.add_argument("--task-token-limit", type=int)
    result.add_argument("--no-evolution", action="store_true")
    result.add_argument("--no-memory", action="store_true")
    result.add_argument(
        "--demo",
        action="store_true",
        help="Run a local scripted demonstration without credentials or network",
    )
    return result


def session_path(project, identifier, *, existing=True):
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


class InputCoordinator:
    """A single add_reader source routes both commands and approval answers."""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.pending = None
        self.eof = False
        self.loop = None
        self.fd = None
        self.buffer = bytearray()

    def start(self):
        self.loop = asyncio.get_running_loop()
        self.fd = sys.stdin.fileno()
        # Regular files cannot be registered with epoll/kqueue; they are finite.
        import stat

        if stat.S_ISREG(os.fstat(self.fd).st_mode):
            for line in sys.stdin:
                self.queue.put_nowait(line.rstrip("\n"))
            self._eof()
            return
        try:
            self.loop.add_reader(self.fd, self._readable)
        except (OSError, PermissionError):
            self._eof()

    def _readable(self):
        data = os.read(self.fd, 65536)
        if not data:
            if self.buffer:
                self._line(self.buffer.decode(errors="replace"))
                self.buffer.clear()
            self._eof()
            return
        self.buffer.extend(data)
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer[:] = rest
            self._line(line.decode(errors="replace").rstrip("\r"))

    def _line(self, line):
        if self.pending is not None and not self.pending.done():
            self.pending.set_result(line.strip().lower() in {"y", "yes"})
            if line.startswith("/"):
                self.queue.put_nowait(line)
        else:
            self.queue.put_nowait(line)

    def _eof(self):
        self.eof = True
        if self.loop and self.fd is not None:
            self.loop.remove_reader(self.fd)
        if self.pending is not None and not self.pending.done():
            self.pending.set_result(False)
        self.queue.put_nowait(None)

    async def confirm(self, spec, args, context):
        if self.eof:
            return False
        self.pending = asyncio.get_running_loop().create_future()
        print(f"Approve {spec.name}? [y/N]", flush=True)
        try:
            return await self.pending
        finally:
            self.pending = None

    def close(self):
        if self.loop and self.fd is not None:
            self.loop.remove_reader(self.fd)
        if self.pending and not self.pending.done():
            self.pending.set_result(False)


def show(value):
    if is_dataclass(value):
        value = asdict(value)
    print(
        json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value,
        flush=True,
    )


async def command(agent, line):
    parts = shlex.split(line)
    name, args = parts[0], parts[1:]
    busy = agent._running
    if name == "/mcp-trust" and busy:
        raise ValueError("Wait for the active turn before trusting an MCP server")
    if name in {"/branch", "/fork", "/refresh", "/skill-rollback", "/goal"} and busy:
        raise ValueError("Wait for the active turn before changing session state")
    if name == "/cancel":
        agent.abort()
    elif name in {"/steer", "/followup"}:
        if not args:
            raise ValueError("Provide a message")
        (agent.steer if name == "/steer" else agent.follow_up)(" ".join(args))
    elif name == "/goal":
        if not args:
            show(agent.goal)
        else:
            agent.set_goal(" ".join(args))
            show(agent.goal)
    elif name == "/tree":
        show(
            [
                {"id": r["id"], "parent": r["parent"], "kind": r["kind"]}
                for r in agent.session.records
            ]
        )
    elif name == "/branch" and len(args) == 1:
        show(agent.branch(args[0]))
    elif name == "/fork" and len(args) == 1:
        child = agent.session.fork(session_path(agent.project, args[0], existing=False))
        show(child.session_id)
        child.close()
    elif name == "/memory":
        if not args:
            show(agent.memory.list())
        elif args[0] == "save" and len(args) == 3:
            if busy or agent.permission_mode in {"plan", "dontAsk"}:
                raise ValueError("Memory writes unavailable in the current state")
            show(agent.memory.update(args[1], args[2]))
        else:
            show(agent.memory.search(" ".join(args)))
    elif name == "/skills":
        show(
            [
                {"name": s.name, "version": s.version, "description": s.description}
                for s in agent.skills.capture_catalog().values()
            ]
        )
    elif name == "/skill-history" and len(args) == 1:
        show(agent.evolution.history(args[0]))
    elif name == "/skill-rollback" and len(args) == 2:
        if agent.permission_mode in {"plan", "dontAsk"}:
            raise ValueError("Skill writes unavailable in the current mode")
        show(await agent.rollback_skill(args[0], args[1]))
    elif name == "/refresh":
        agent.context_manager.refresh(agent._system(agent.skills.capture_catalog()))
        show("Project instructions, memory and skills refreshed")
    elif name == "/usage":
        show(
            {
                "calls": agent.budget.calls,
                "tokens": agent.budget.total_tokens,
                "pending_tokens": agent.budget.pending_tokens,
                "unknown_calls": agent.budget.unknown_calls,
            }
        )
    elif name == "/mcp" and not args:
        show(
            [
                {
                    "name": name,
                    "command": client.command,
                    "args": list(client.args),
                    "trusted": client.trusted,
                }
                for name, client in sorted(agent.mcp.clients.items())
            ]
        )
    elif name == "/mcp-trust" and len(args) == 1:
        client = agent.mcp.clients.get(args[0])
        if client is None:
            raise ValueError("Unknown MCP server")
        show({"name": args[0], "command": client.command, "args": list(client.args)})
        result = agent.trust_mcp(args[0], authorized=True)
        if hasattr(result, "__await__"):
            result = await result
        show(result)
    elif name == "/help":
        show(
            '/steer text; /followup text; /cancel; /goal text; /tree; /branch record; /fork name; /memory [query | save name "content"]; /skills; /skill-history name; /skill-rollback name version; /refresh; /usage; /mcp; /mcp-trust name; /quit'
        )
    else:
        raise ValueError("Unknown command or invalid arguments; use /help")


async def interactive(agent, inputs, display=None):
    active = None
    status = 0

    async def turn(text, queued=False):
        nonlocal status
        try:
            answer = await agent.resume_pending() if queued else await agent.run_once(text)
            (display.finish if display else show)(answer)
        except asyncio.CancelledError:
            show("Turn cancelled")
            status = 130
        except Exception as exc:
            show(f"Turn failed ({type(exc).__name__})")
            status = 1

    show("Weyra ready. /help for commands; /quit to exit.")
    while True:
        line = await inputs.queue.get()
        if line is None or line.strip() == "/quit":
            if active and not active.done():
                agent.abort()
            break
        if not line.strip():
            continue
        try:
            if line.startswith("/"):
                await command(agent, line)
                if line.startswith("/followup ") and (active is None or active.done()):
                    active = asyncio.create_task(turn(None, queued=True))
                    await asyncio.sleep(0)
            elif active and not active.done():
                agent.steer(line)
            else:
                if active:
                    await active
                active = asyncio.create_task(turn(line))
                await asyncio.sleep(0)
        except Exception as exc:
            show(f"Command failed ({type(exc).__name__})")
    if active:
        await active
    return status


def demo_runtime(project):
    fixture = project / ".weyra" / "demo-fixture.txt"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    if fixture.is_symlink() or not fixture.resolve().is_relative_to(project):
        raise ValueError("Demo fixture path escapes project")
    fixture.write_text("Weyra offline demo: the local tool execution succeeded.\n")
    registry = ProviderRegistry()
    registry.register(
        "demo",
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall("demo-read", "read_file", {"path": ".weyra/demo-fixture.txt"}),
                    ),
                    usage=Usage(20, 10),
                ),
                ModelResponse(
                    "Weyra offline demo: read_file completed and its evidence is saved in the session.",
                    usage=Usage(30, 20),
                ),
            ]
        ),
    )
    model = ModelInfo("demo", "scripted", 65536, 4096)
    runtime = ModelRuntime(
        registry, {purpose: model for purpose in ("main", "compression", "memory", "evolution")}
    )
    runtime.context_policy = {"headroom_tokens": 4096, "max_tokens": 4096}
    return runtime


async def run(args):
    project = args.project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    if args.max_steps < 1:
        raise ValueError("max_steps must be positive")
    inputs = InputCoordinator()
    runtime = None
    agent = None
    try:
        if args.demo:
            runtime = demo_runtime(project)
        else:
            override = {
                name: getattr(args, name)
                for name in (
                    "provider",
                    "protocol",
                    "model",
                    "context_window",
                    "max_output_tokens",
                    "base_url",
                    "api_key_env",
                )
            }
            runtime = load_config(project, override).build_runtime()
        path = session_path(project, args.resume) if args.resume else None
        # One-shot prompts have no competing input consumer; EOF denies approvals.
        inputs.start()

        from .ui import StreamDisplay

        display = StreamDisplay()

        async def events(kind, data):
            display.event(kind, data)

        agent = Agent(
            project,
            runtime,
            session_path=path,
            permission_mode=args.permission_mode,
            confirm=inputs.confirm,
            budget=Budget(args.max_tokens, args.max_calls, 20000),
            max_steps=args.max_steps,
            task_token_limit=args.task_token_limit,
            enable_memory=not args.no_memory,
            enable_evolution=not args.no_evolution and not args.demo,
            on_event=events,
        )
        if args.prompt is not None or args.demo:
            display.finish(
                await agent.run_once(
                    args.prompt or "Read .weyra/demo-fixture.txt and report the result."
                )
            )
            return 0
        return await interactive(agent, inputs, display=display)
    finally:
        inputs.close()
        if agent:
            await agent.close()
        elif runtime:
            await runtime.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        from . import commands

        if argv and argv[0] in commands.COMMANDS:
            result = asyncio.run(commands.run(commands.parser().parse_args(argv)))
            commands.render(result)
            return 0
        args = parser().parse_args(argv)
        return asyncio.run(run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Weyra cancelled", file=sys.stderr)
        return 130
    except ModelError as exc:
        print(f"Weyra model request failed ({exc.kind})", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError) as exc:
        # Configuration validation messages contain field names, never SDK payloads.
        print(
            f"Weyra configuration/input error ({type(exc).__name__}); check configuration, explicit context limits, and session identifier.",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(f"Weyra failed ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

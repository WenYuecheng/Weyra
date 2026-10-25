"""Weyra 应用程序主入口与统一异步标准输入协调器（InputCoordinator）。

架构定位与设计哲学:
    本模块为 Weyra CLI 与 TUI 终端应用的统一引导启动入口（Application Bootstrapper）。
    在异步终端程序中，用户输入源极其复杂：既包含常规的提示词输入（Prompts），又包含带优先级的
    斜杠控制命令（Slash Commands），同时还要实时响应危险操作的人工安全审批询问（Tool Approval Prompts）。
    若使用多个独立的 input() 或多线程读取，极易引发终端回显破坏、文件描述符竞争以及死锁。
    本模块通过内核级事件循环绑定（add_reader）构建单所有权输入协调器（InputCoordinator），
    并根据终端环境自动决策加载 Rich Textual TUI 或极简流式输出终端。

开源框架借鉴与设计原则:
    1. Pi Agent (dg-ai-notes Ch. 2, 7, 11) & DeepSeek-Harness (packages/boot & packages/client):
       - 单输入源协调器（Single Input Owner）：将标准输入文件描述符绑定至 asyncio 事件循环的 `add_reader`，
         统一管理未决输入队列与审批 Future（pending），并支持动态抢占插入（Steering / Follow-up）；
       - POSIX 特性适配：针对 Linux/macOS 下重定向常规文件（stat.S_ISREG）无法注册 epoll/kqueue 的内核限制，
         自动降级为全量同步批次读取，彻底保证脚本管道输入与交互终端输入的双重兼容。
       - 界面自适应引导（UI Mode Auto-detection）：检测当前标准输入输出是否为 TTY 并探测 textual 依赖，
         无缝切换全屏响应式 TUI 应用（weyra.tui.app）或无缓冲流式标准输出，兼顾开发者日常沉浸式开发与 CI 批处理。
    2. Hermes Agent:
       - 行为配置与机密凭据严格分离设计，支持多模型路由映射与外部环境快照。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .agent import Agent
from .session_commands import run_session_command, session_path
from .config import load_config
from .contracts import ModelInfo, ModelResponse, ToolCall, Usage
from .model_runtime import ModelError, ModelRuntime, ProviderRegistry, ScriptedProvider
from .runtime_state import Budget


def parser():
    """构建 Weyra 命令行启动参数解析器。"""
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
    result.add_argument(
        "--ui",
        choices=["auto", "tui", "plain"],
        default="auto",
        help="UI mode: 'tui' for rich Textual interface, 'plain' for classic streaming CLI, 'auto' detects TTY and dependencies",
    )
    result.add_argument(
        "--no-icons",
        action="store_true",
        help="Use ASCII text indicators instead of Unicode icons",
    )
    return result


class InputCoordinator:
    """单一所有权异步标准输入调度器。

    【开源设计思想与架构定位】
    借鉴 Pi Agent 单终端输入流架构与 DeepSeek-Harness 事件驱动调度：
        - 统一通过 asyncio.loop.add_reader(sys.stdin.fileno()) 监听底层字节流；
        - 当系统正在等待人工工具审批（self.pending 活跃）时，输入直接解析为审批布尔值；
        - 若同时伴随斜杠命令（如 `/cancel`），支持直接拦截派发，避免审批卡死主循环；
        - 支持双队列动态交互，保证人类意图抢占（Steering）与后续跟进（Follow-up）有序投递。
    """

    def __init__(self):
        """初始化输入调度器队列、缓冲区与审批状态。"""
        self.queue = asyncio.Queue()
        self.pending = None
        self.eof = False
        self.loop = None
        self.fd = None
        self.buffer = bytearray()

    def start(self):
        """启动事件循环读监听，适配常规文件与 TTY 终端。"""
        self.loop = asyncio.get_running_loop()
        self.fd = sys.stdin.fileno()
        # 常规文件（如管道重定向输入）无法注册到 kqueue/epoll，需走批次迭代模式
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
        """底层字节流就绪回调，拼装按行分割并转送。"""
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
        """分发单行输入至审批 Future 或常规消息队列。"""
        if self.pending is not None and not self.pending.done():
            self.pending.set_result(line.strip().lower() in {"y", "yes"})
            if line.startswith("/"):
                self.queue.put_nowait(line)
        else:
            self.queue.put_nowait(line)

    def _eof(self):
        """处理标准输入流到达 EOF 事件。"""
        self.eof = True
        if self.loop and self.fd is not None:
            self.loop.remove_reader(self.fd)
        if self.pending is not None and not self.pending.done():
            self.pending.set_result(False)
        self.queue.put_nowait(None)

    async def confirm(self, spec, args, context):
        """向终端用户发起人工确认提示并挂起协程等待响应。"""
        if self.eof:
            return False
        self.pending = asyncio.get_running_loop().create_future()
        print(f"Approve {spec.name}? [y/N]", flush=True)
        try:
            return await self.pending
        finally:
            self.pending = None

    def close(self):
        """关闭输入调度器并解绑底层文件描述符监听。"""
        if self.loop and self.fd is not None:
            self.loop.remove_reader(self.fd)
        if self.pending and not self.pending.done():
            self.pending.set_result(False)



def show(value):
    """格式化打印输出对象或文本内容至标准输出。"""
    if is_dataclass(value):
        value = asdict(value)
    print(
        json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value,
        flush=True,
    )


async def command(agent, line):
    """派发并执行斜杠控制命令的异步入口封装。"""
    return await run_session_command(agent, line, show)


async def interactive(agent, inputs, display=None):
    """运行终端交互式 REPL 循环。

    【核心不变性与安全约束】
    遵循 Pi Agent 双队列调度范式：
    1. 当存在活跃轮次时，非斜杠输入自动转为 `agent.steer(line)` 抢占式干预；
    2. 当系统处于空闲时，新输入触发全新单轮推理；
    3. 支持 `/followup` 触发后台队列跟进轮次。
    """
    active = None
    status = 0

    async def turn(text, queued=False):
        """执行单轮推理或恢复排队任务的异步子协程。"""
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
    """构造用于零网络与免密演示的纯离线预设 Mock ModelRuntime。"""
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
    """根据解析参数引导启动 Weyra 智能体（自动决策 TUI 模式或传统命令行模式）。

    【核心流程】
    1. 解析项目路径并校验最大步数；
    2. 加载运行时 ModelRuntime（支持 --demo 本地免密沙箱）；
    3. 模式决策：若交互式 TTY 且具备 Textual 则自动启动 TUI 应用；
    4. 降级模式：启动 InputCoordinator 与 StreamDisplay 执行单轮或 REPL 交互。
    """
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

        # Determine whether to launch TUI
        use_tui = False
        if args.prompt is None and (not args.demo or args.ui == "tui"):
            if args.ui == "tui":
                if not sys.stdin.isatty():
                    print("Error: TUI mode requires an interactive TTY terminal.", file=sys.stderr)
                    return 1
                try:
                    import textual  # noqa: F401
                except ImportError:
                    print(
                        "Error: Textual is required for TUI mode. Install with: pip install 'weyra[tui]'",
                        file=sys.stderr,
                    )
                    return 1
                use_tui = True
            elif args.ui == "auto":
                try:
                    import textual  # noqa: F401
                    has_textual = True
                except ImportError:
                    has_textual = False
                if has_textual and sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("CI"):
                    use_tui = True

        if use_tui:
            def make_runtime():
                """工厂函数：返回当前构造的共享模型运行时。"""
                return runtime

            from .service import WeyraAppService
            from .tui import run_tui_async

            service = WeyraAppService(
                project,
                runtime_factory=make_runtime,
                session_id=str(path) if path else None,
                permission_mode=args.permission_mode,
                budget_limit_tokens=args.max_tokens,
                budget_limit_calls=args.max_calls,
                max_steps=args.max_steps,
                task_token_limit=args.task_token_limit,
                enable_memory=not args.no_memory,
                enable_evolution=False if args.no_evolution or args.demo else None,
            )
            return await run_tui_async(service, ascii_only=args.no_icons)

        # One-shot prompts have no competing input consumer; EOF denies approvals.
        inputs.start()

        from .ui import StreamDisplay

        display = StreamDisplay()

        async def events(kind, data):
            """将智能体事件转发给 StreamDisplay 终端展示器。"""
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
            enable_evolution=False if args.no_evolution or args.demo else None,
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
    """Weyra CLI 应用程序统一同步入口点。"""
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

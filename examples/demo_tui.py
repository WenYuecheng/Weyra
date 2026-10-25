"""Offline demonstration of Weyra Rich Terminal TUI with 3 scripted scenarios.

Scenarios:
1. 'analysis': Project inspection with read_file tool card, diff/preview, and structured Markdown answer.
2. 'skills': Skill inspection and execution workflow.
3. 'context': Multi-turn execution with thinking reasoning and compaction notice.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from weyra.contracts import ModelInfo, ModelResponse, ToolCall, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.service import WeyraAppService
from weyra.tui import run_tui


def make_scenario_runtime(scenario: str) -> ModelRuntime:
    registry = ProviderRegistry()

    if scenario == "analysis":
        responses = [
            ModelResponse(
                content="",
                tool_calls=(
                    ToolCall("call-analysis-1", "read_file", {"path": "src/main.py"}),
                ),
                usage=Usage(25, 10),
            ),
            ModelResponse(
                content=(
                    "## 项目代码分析报告\n\n"
                    "已完成对 `src/main.py` 的结构化代码审查：\n\n"
                    "| 模块 | 职责 | 质量评分 |\n"
                    "|---|---|---|\n"
                    "| `main.py` | 启动入口与参数解析 | A+ |\n"
                    "| `service.py` | 应用生命周期管理 | A |\n\n"
                    "```python\n"
                    "# 核心优化建议\n"
                    "def optimize_cache(key: str) -> None:\n"
                    "    print(f'Cache optimized for {key}')\n"
                    "```\n\n"
                    "> [!NOTE]\n"
                    "> 模块依赖解耦良好，无全局状态泄漏。"
                ),
                usage=Usage(120, 65),
            ),
        ]
    elif scenario == "skills":
        responses = [
            ModelResponse(
                content="",
                tool_calls=(
                    ToolCall("call-skill-1", "list_files", {"path": "."}),
                ),
                usage=Usage(15, 5),
            ),
            ModelResponse(
                content=(
                    "### 技能与演化状态汇报\n\n"
                    "当前项目已加载内置演化技能库：\n"
                    "- `code_refactor` [v1.2.0]: 自动化重构与代码简化\n"
                    "- `doc_sync` [v1.0.0]: 文档自动同步\n\n"
                    "按 `Ctrl+K` 可打开交互式技能管理器进行版本回滚或详情审计。"
                ),
                usage=Usage(80, 45),
            ),
        ]
    else:  # context
        responses = [
            ModelResponse(
                content="思考完毕，开始执行上下文维护...",
                usage=Usage(30, 15),
                provider_data={"reasoning_content": "用户要求回顾长任务背景。当前会话已有多轮历史，检查 token 预算并触发必要压缩。"},
            ),
            ModelResponse(
                content="长任务背景已成功恢复，上下文压缩状态良好，就绪接收下一指令。",
                usage=Usage(50, 20),
            ),
        ]

    registry.register("scripted", ScriptedProvider(responses))
    model = ModelInfo("scripted", "demo-model", 131072, 4096)
    runtime = ModelRuntime(
        registry,
        {purpose: model for purpose in ("main", "compression", "memory", "evolution")},
    )
    runtime.context_policy = {"headroom_tokens": 4096, "max_tokens": 4096}
    return runtime


def run_demo(scenario: str = "analysis", *, ascii_only: bool = False) -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)
        (project_dir / "src").mkdir(parents=True, exist_ok=True)
        (project_dir / "src" / "main.py").write_text(
            '"""Entry point for sample demo project."""\n\ndef main():\n    print("Hello from Weyra Demo")\n',
            encoding="utf-8",
        )

        def runtime_factory():
            return make_scenario_runtime(scenario)

        service = WeyraAppService(
            project_dir,
            runtime_factory=runtime_factory,
            session_id=f"demo_{scenario}",
            permission_mode="default",
            enable_memory=False,
            enable_evolution=False,
        )

        return run_tui(service, ascii_only=ascii_only)


def main() -> int:
    parser = argparse.ArgumentParser(description="Weyra TUI Offline Demo")
    parser.add_argument(
        "--scenario",
        choices=["analysis", "skills", "context"],
        default="analysis",
        help="Demo scenario to reproduce offline",
    )
    parser.add_argument(
        "--no-icons",
        action="store_true",
        help="Use ASCII text indicators instead of Unicode icons",
    )
    args = parser.parse_args()
    return run_demo(args.scenario, ascii_only=args.no_icons)


if __name__ == "__main__":
    sys.exit(main())

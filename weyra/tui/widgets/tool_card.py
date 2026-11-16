"""可折叠工具调用卡片、Unified Diff 语法高亮与超长输出截断组件 (Collapsible Tool Card)。

【架构定位】
负责工具调用的状态生命周期呈现（执行中、成功、失败、拒绝等）、入参格式化展示、
文件修改的统一直方差异高亮 (Unified Diff) 以及超大输出结果的安全截断。

【开源设计思想与架构血统】
1. 借鉴 BearCode 的终端直观交互：
   - 工具调用采用专属 Emoji/字符图标与颜色徽章标注状态。
   - 若工具涉及文件修改（如 `patch` 或 `write_file`），自动在卡片内展开带有 `+` 绿和 `-` 红
     高亮配色的差异对比，并附带 `+N lines · -M lines` 变更统计摘要。
2. 借鉴 DeepSeek-Harness 的产物截断与溢出保护机制 (Presenter Output Spilling & Truncation)：
   - 当工具输出超过 200 行或 20,000 字符时，终端卡片自动截断并展示提示；
   - 提供“查看完整输出 →”按钮，引导用户通过模态视窗或落地持久化产物文件查看全量细节，
     避免万行日志把终端界面撑爆。
"""

from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Button, Collapsible, Static

from weyra.presentation import get_tool_icon, get_tool_summary, truncate_preview


class ToolCardWidget(Collapsible):
    """工具调用与结果呈现折叠卡片组件 (Tool Execution & Diff Display Card)。

    【架构定位】
    封装工具执行生命周期卡片，支持实时状态更新、Diff 着色渲染与超长结果截断。
    """

    def __init__(
        self,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        *,
        ascii_only: bool = False,
        no_color: bool = False,
        **kwargs,
    ) -> None:
        """初始化工具调用折叠卡片组件 (Tool Execution Card Widget)。"""
        self.call_id = call_id
        self.tool_name = name
        self.arguments = arguments
        self.ascii_only = ascii_only
        self.no_color = no_color
        self.duration: float | None = None
        self.status: str = "running"
        self.artifact_path: str | None = None
        self.full_content: str | None = None

        self._content_static = Static("正在执行…", markup=False)
        self.output_button = Button("查看完整输出 →", classes="view-output")
        self.output_button.display = False
        self._body_container = Vertical(self._content_static, self.output_button)

        title = self._build_title()
        super().__init__(self._body_container, title=title, collapsed=True, **kwargs)
        self.add_class("running")

    def _build_title(self) -> Content | str:
        """组装工具卡片标题栏徽章、执行耗时与参数摘要富文本。"""
        icon = get_tool_icon(self.tool_name, ascii_only=self.ascii_only)
        summary = get_tool_summary(self.tool_name, self.arguments)
        summary_str = f" ({summary})" if summary else ""

        if self.no_color:
            if self.status == "running":
                badge = "[RUNNING]"
            elif self.status == "success":
                badge = "[COMPLETED]"
            else:
                badge = f"[{self.status.upper()}]"
            dur_str = f" {self.duration:.2f}s" if self.duration is not None else ""
            return f"{icon} {self.tool_name}{summary_str} · {badge}{dur_str}"

        # 赛博朋克 / BearCode 胶囊徽章样式渲染
        if self.status == "running":
            markup = (
                f"[bold black on #fbbf24] ⚡ TOOL DISPATCH [/] "
                f"[bold #fbbf24]› {icon} {self.tool_name}[/] "
                f"[dim #64748b]{summary_str}[/]"
            )
        elif self.status == "success":
            dur_str = f" [dim #64748b]({self.duration:.2f}s)[/]" if self.duration is not None else ""
            markup = (
                f"[bold black on #10b981] ✓ TOOL COMPLETED [/] "
                f"[bold #10b981]› {icon} {self.tool_name}[/] "
                f"[dim #64748b]{summary_str}[/]{dur_str}"
            )
        else:
            dur_str = f" [dim #64748b]({self.duration:.2f}s)[/]" if self.duration is not None else ""
            badge_text = {"denied": "DENIED", "cancelled": "CANCELLED", "invalid": "INVALID"}.get(
                self.status, "FAILED"
            )
            markup = (
                f"[bold black on #f43f5e] ✖ TOOL {badge_text} [/] "
                f"[bold #f43f5e]› {icon} {self.tool_name}[/] "
                f"[dim #64748b]{summary_str}[/]{dur_str}"
            )
        return Content.from_markup(markup)

    def complete(
        self,
        status: str,
        result: str,
        duration: float | None = None,
        preview: dict[str, Any] | None = None,
        artifact_path: str | None = None,
    ) -> None:
        """更新工具卡片执行结果、耗时、Unified Diff 与截断预览 (Complete Tool Execution & Render Diff)。

        【开源设计思想与架构血统】
        遵循 DeepSeek-Harness 的卡片展现规范：
        1. 状态由 running 转移至 success / failed / denied，并更新耗时标签；
        2. 格式化参数 JSON；
        3. 解析 Unified Diff 语法行并高亮着色；
        4. 检查超长输出截断，并在必要时显示独立产物查看按钮。

        Args:
            status: 工具执行状态码（'success', 'failed', 'denied' 等）。
            result: 工具执行原始返回结果。
            duration: 工具执行耗时秒数。
            preview: 结构化预览字典（可能包含 diff、stats 等元数据）。
            artifact_path: 大输出持久化产物落盘路径。
        """
        self.status = status
        self.duration = duration
        self.artifact_path = artifact_path
        self.full_content = result

        self.remove_class("running")
        if status == "success":
            self.add_class("success")
        else:
            self.add_class("failed")

        self.title = self._build_title()

        body_text = Text()

        # 1. 调用入参段 (Parameters & Arguments)
        if self.arguments:
            body_text.append("▶ Parameters (Arguments):\n", style="bold #38bdf8" if not self.no_color else "bold")
            arg_str = json.dumps(self.arguments, ensure_ascii=False, indent=2)
            body_text.append(f"{arg_str}\n\n", style="dim #64748b" if not self.no_color else "dim")

        # 2. 代码变更直方差异段 (Unified Diff Section)
        diff = preview.get("diff") if isinstance(preview, dict) else None
        if diff:
            diff_lines = diff.splitlines()
            adds = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
            dels = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))

            body_text.append("▶ File Modified (Unified Diff):\n", style="bold #10b981" if not self.no_color else "bold")
            for line in diff_lines:
                if line.startswith("@@"):
                    body_text.append(f"  {line}\n", style="bold #00f2fe" if not self.no_color else "")
                elif line.startswith("-"):
                    body_text.append(f"  {line}\n", style="bold #f43f5e" if not self.no_color else "")
                elif line.startswith("+"):
                    body_text.append(f"  {line}\n", style="bold #10b981" if not self.no_color else "")
                else:
                    body_text.append(f"  {line}\n", style="dim #64748b" if not self.no_color else "")

            body_text.append(
                f"\n  ✦ Summary: +{adds} lines · -{dels} lines\n\n",
                style="bold #34d399" if not self.no_color else "bold",
            )

        # 3. 运行执行输出与超长截断段 (Execution Output & Truncation)
        if result:
            body_text.append("▶ Execution Output:\n", style="bold #00f2fe" if not self.no_color else "bold")
            preview_text, was_truncated, total_l, total_c = truncate_preview(
                result, max_lines=200, max_chars=20000
            )
            body_text.append(preview_text)
            if was_truncated:
                loc_hint = f"，完整结果保存在: {artifact_path}" if artifact_path else " (完整结果在会话记录中)"
                body_text.append(
                    f"\n\n... [已截断，共 {total_l} 行 / {total_c} 字符{loc_hint}]\n",
                    style="bold #fbbf24" if not self.no_color else "bold",
                )

        self._content_static.update(body_text)
        self.output_button.display = bool(artifact_path or len(result) > 20000)

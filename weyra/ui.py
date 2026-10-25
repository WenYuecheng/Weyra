"""终端纯文本流式渲染器（StreamDisplay）。

架构定位与设计哲学:
    本模块为经典 CLI 流式输出提供零开销、事件驱动的终端呈现器。
    严格独立于 Agent 核心执行循环与底层协议驱动，仅作为观察者订阅并渲染实时事件流。
    负责增量 Token 打字机打印、模型多轮尝试中断重置提示与工具执行状态刷新。

开源框架借鉴与设计原则:
    1. DeepSeek-Harness (packages/client):
       - 尝试重置语义（attempt_reset）：当大模型流式生成遭遇网络抖动、协议解析失败或被抢占式打断时，
         向终端输出明确的临时输出重置提示（`[本次尝试未完成，以上为临时输出]`），重置内部文本缓冲，
         防止多轮重试产生的垃圾输出与正常回答混杂。
    2. PiAgent:
       - 严格的实时刷新与流输出解耦：每个 chunk 均通过 `flush=True` 即时推向标准输出，
         确保在大模型长文本生成过程中用户具备零延迟的打字机交互体验。
"""

import sys


class StreamDisplay:
    """轻量级终端事件流渲染器。"""

    def __init__(self, output=None):
        """初始化流式渲染器。

        Args:
            output: 输出流目标对象，默认为 sys.stdout。
        """
        self.output = output if output is not None else sys.stdout
        self.current = ""

    def event(self, kind: str, data):
        """处理来自 Agent 循环的实时事件更新。

        Args:
            kind: 事件类型（'text', 'attempt_reset', 'tool_completed'）。
            data: 事件附带的数据。
        """
        if kind == "text":
            self.current += str(data)
            print(data, end="", file=self.output, flush=True)
        elif kind == "attempt_reset":
            if self.current:
                print("\n[本次尝试未完成，以上为临时输出]", file=self.output, flush=True)
            self.current = ""
        elif kind == "tool_completed":
            print(
                f"\nTool {data.get('call_id', '')}: {data.get('status', '')}",
                file=self.output,
                flush=True,
            )
            self.current = ""

    def finish(self, answer: str):
        """轮次结束时的最终答案对齐与换行刷新。"""
        if self.current == answer:
            print(file=self.output, flush=True)
        else:
            print(answer, file=self.output, flush=True)
        self.current = ""


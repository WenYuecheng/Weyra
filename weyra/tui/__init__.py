"""基于 Textual 的 Weyra 全屏交互式终端用户界面（Rich Terminal TUI）。

架构定位与设计哲学:
    本包为 Weyra 运行时提供沉浸式、全异步、响应式的终端图形交互界面（Textual TUI）。
    严格遵循表现层与核心业务服务解耦原则，整个 TUI 仅依赖 `WeyraAppService` 对外暴露的异步接口与事件流，
    实现组件化卡片流式渲染、思考链抽屉折叠、实时成本看板、交互式审批弹窗与会话树切换。
"""

from __future__ import annotations

from weyra.service import WeyraAppService
from weyra.tui.app import WeyraTUIApp

__all__ = ["WeyraTUIApp", "run_tui", "run_tui_async"]


def run_tui(service: WeyraAppService, *, ascii_only: bool = False) -> int:
    """同步阻塞启动交互式 Textual TUI 应用。"""
    app = WeyraTUIApp(service, ascii_only=ascii_only)
    app.run()
    return 0


async def run_tui_async(service: WeyraAppService, *, ascii_only: bool = False) -> int:
    """在现有 asyncio 事件循环中异步启动 Textual TUI 协程。"""
    app = WeyraTUIApp(service, ascii_only=ascii_only)
    await app.run_async()
    return 0



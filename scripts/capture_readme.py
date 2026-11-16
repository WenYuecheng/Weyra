"""Capture the running Textual app using an isolated offline model and real tools.

Install with: python -m pip install -e '.[tui,screenshots]'
Run from the repository: python scripts/capture_readme.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET

from rich.cells import cell_len

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from weyra.contracts import ModelInfo, ModelResponse, ToolCall, Usage
from weyra.model_runtime import ModelRuntime, ProviderRegistry, ScriptedProvider
from weyra.service import WeyraAppService
from weyra.tui.app import WeyraTUIApp


NOTE = "# Weyra demo\n\nReal local tools, session storage and permission approval.\n"


def offline_runtime() -> ModelRuntime:
    """Supply deterministic model responses; file access uses production handlers."""
    registry = ProviderRegistry()
    registry.register("offline", ScriptedProvider([
        ModelResponse(tool_calls=(ToolCall("read-readme", "read_file", {
            "path": "README.md",
        }),), usage=Usage(32, 12)),
        ModelResponse(
            "## 工作区读取完成\n\n"
            "已通过 `read_file` 读取 `README.md`。\n\n"
            "- 当前工程：Weyra 演示工作区\n"
            "- 工具结果与消息已写入本地会话\n"
            "- 可通过 F2 查看会话，或用 `/usage` 检查用量\n\n"
            "> 离线演示：模型回复预设，文件读取与会话持久化实际执行。",
            usage=Usage(96, 60),
        ),
        ModelResponse(tool_calls=(ToolCall("write-note", "write_file", {
            "path": "notes.md", "content": NOTE,
        }),), usage=Usage(48, 24)),
        ModelResponse("已在批准后创建 `notes.md`，操作结果已保存。", usage=Usage(64, 20)),
    ]))
    model = ModelInfo("offline", "scripted-demo", 131072, 4096)
    runtime = ModelRuntime(registry, {"main": model})
    runtime.context_policy = {"headroom_tokens": 4096, "max_tokens": 4096}
    return runtime


async def wait_until(pilot, predicate) -> None:
    """Wait for a real state change with a bounded deadline."""
    async with asyncio.timeout(15):
        while not predicate():
            await pilot.pause(0.05)
    await pilot.pause(0.25)


async def save(app: WeyraTUIApp, page, destination: Path, name: str) -> None:
    """Rasterize the screen, retaining terminal cell widths for CJK characters."""
    svg = app.export_screenshot(title="Weyra · offline runtime demo")
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    screen = ET.fromstring(svg)
    # Rich's SVG exporter uses len(text) for textLength. Terminal wide glyphs
    # occupy two cells, so restore that width before rasterizing the screen.
    for element in screen.iter("{http://www.w3.org/2000/svg}text"):
        text = element.text or ""
        if text and "textLength" in element.attrib:
            length = float(element.attrib["textLength"])
            element.set("textLength", str(length * cell_len(text) / len(text)))
    svg = ET.tostring(screen, encoding="unicode")
    dimensions = screen.attrib
    width, height = map(float, dimensions["viewBox"].split()[2:])
    await page.set_viewport_size({
        "width": round(width),
        "height": round(height),
    })
    await page.set_content(
        '<!doctype html><style>html,body{margin:0}svg{display:block}'
        'svg text{font-family:Menlo,"PingFang SC","Noto Sans CJK SC",monospace!important}'
        '</style>' + svg,
    )
    await page.evaluate("document.fonts.ready")
    await page.screenshot(path=str(destination / f"{name}.png"))
    print(f"Captured {name}.png", flush=True)


async def capture(destination: Path) -> None:
    """Operate the app through its input widget, hotkeys and approval dialog."""
    from playwright.async_api import async_playwright

    destination.mkdir(parents=True, exist_ok=True)
    # Capture the normal color interface even when a parent automation disables ANSI.
    os.environ.pop("NO_COLOR", None)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route("https://**/*", lambda route: route.abort())
            await capture_app(page, destination)
        finally:
            await browser.close()


async def capture_app(page, destination: Path) -> None:
    """Capture one isolated app session with a shared SVG rendering page."""
    with tempfile.TemporaryDirectory(prefix="weyra-readme-") as folder:
        project = Path(folder) / "weyra-demo"
        project.mkdir()
        (project / "README.md").write_text(
            "# Weyra demo workspace\n\n"
            "A temporary project for local tools, session storage and approval.\n",
            encoding="utf-8",
        )
        service = WeyraAppService(
            project, runtime_factory=offline_runtime, session_id="readme-demo",
            enable_memory=False, enable_evolution=False,
        )
        app = WeyraTUIApp(service)
        async with app.run_test(size=(132, 46)) as pilot:
            await pilot.pause(0.5)
            await save(app, page, destination, "01-welcome")
            app.editor_area.set_text("读取 README.md，确认工作区信息。")
            await pilot.press("enter")
            await wait_until(pilot, lambda: service._last_outcome == "completed")
            assert app.chat_view.tool_cards["read-readme"].full_content
            await save(app, page, destination, "02-tool-workflow")
            app.editor_area.set_text("创建 notes.md，写入这次运行的说明。")
            await pilot.press("enter")
            await wait_until(pilot, lambda: app._active_approval_modal is not None)
            assert not (project / "notes.md").exists()
            await save(app, page, destination, "03-approval")
            await pilot.press("y")
            await wait_until(pilot, lambda: (
                (project / "notes.md").exists() and service._last_outcome == "completed"
            ))
            assert (project / "notes.md").read_text(encoding="utf-8") == NOTE
            await pilot.press("f2")
            await pilot.pause(0.4)
            await save(app, page, destination, "04-sessions")
            await pilot.press("escape", "f1")
            await pilot.pause(0.4)
            await save(app, page, destination, "05-help")


if __name__ == "__main__":
    asyncio.run(capture(Path(__file__).resolve().parents[1] / "docs" / "screenshots"))

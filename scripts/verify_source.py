"""Verify the installable application offline, without benchmark files or credentials."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import pkgutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import weyra
from weyra.main import demo_runtime
from weyra.service import WeyraAppService
from weyra.tui.app import WeyraTUIApp


async def verify_tui(project: Path) -> None:
    """Exercise real service events, tools, persistence and keyboard interactions."""
    project = project.resolve()
    service = WeyraAppService(
        project, runtime_factory=lambda: demo_runtime(project),
        enable_memory=False, enable_evolution=False,
    )
    app = WeyraTUIApp(service)
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.pause()
        assert app.query_one("#welcome-banner")
        app.editor_area.set_text("Read .weyra/demo-fixture.txt and report the result.")
        await pilot.press("enter")
        async with asyncio.timeout(15):
            while service._last_outcome is None:
                await pilot.pause(0.05)
        assert service._last_outcome == "completed"
        history = await service.get_history()
        assert any(card.get("kind") in {"tool", "tool_call"} for card in history.cards)
        assert app.chat_view.tool_cards["demo-read"].full_content
        await pilot.press("f1")
        assert app.screen.__class__.__name__ == "HelpModal"
        await pilot.press("escape", "f2")
        assert app.screen.__class__.__name__ == "SessionPickerModal"
        await pilot.press("escape")
    assert (project / ".weyra" / "state.db").is_file()
    assert service.lifecycle == "closed"
    print("PASS: TUI, service events, local tool, history, sessions and help")


def main() -> None:
    """Check entry points and the integrated app in temporary workspaces."""
    # On a public checkout every packaged module must import without private files.
    if not Path(weyra.__file__).with_name("evaluation.py").exists():
        for module in pkgutil.walk_packages(weyra.__path__, "weyra."):
            if module.name != "weyra.__main__":
                importlib.import_module(module.name)
        print("PASS: all public application modules import without local data")
    with tempfile.TemporaryDirectory(prefix="weyra-source-check-") as folder:
        root = Path(folder)
        for arguments in (
            ["--help"],
            ["--demo", "--project", str(root / "cli")],
            ["demo", "--offline", "--scenario", "analysis", "--project", str(root / "analysis")],
            ["demo", "--offline", "--scenario", "writing", "--project", str(root / "writing")],
            ["sessions", "--project", str(root / "cli")],
        ):
            result = subprocess.run(
                [sys.executable, "-m", "weyra", *arguments],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            print("PASS: weyra " + " ".join(arguments[:3]))
        project = root / "tui"
        project.mkdir()
        asyncio.run(verify_tui(project))


if __name__ == "__main__":
    main()

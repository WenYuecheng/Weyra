import io
import subprocess
import sys
from weyra.main import main


def test_check_config_prints_only_safe_resolved_fields(tmp_path, capsys):
    (tmp_path / "weyra.toml").write_text("""[routes.main]
provider="deepseek"
model="deepseek-flash"
[routes.main.parameters]
max_output_tokens=512
[context_policy]
max_tokens=512
""")
    assert main(["check-config", "--project", str(tmp_path)]) == 0
    assert "65536" in capsys.readouterr().out


def test_unified_demo_command_offline(tmp_path):
    for scenario in ("analysis", "writing"):
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "weyra.main",
                "demo",
                "--offline",
                "--scenario",
                scenario,
                "--project",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert run.returncode == 0, run.stderr
        assert scenario in run.stdout


def test_stream_display_marks_failed_attempt_and_avoids_duplicate_final():
    from weyra.ui import StreamDisplay

    out = io.StringIO()
    display = StreamDisplay(out)
    display.event("text", "partial")
    display.event("attempt_reset", {"kind": "transient"})
    display.event("text", "final")
    display.finish("final")
    text = out.getvalue()
    assert text.count("final") == 1
    assert "未完成" in text


def test_live_commands_reuse_the_project_verification_budget(tmp_path, monkeypatch):
    import asyncio
    import pytest
    from types import SimpleNamespace
    from weyra import commands, demos
    from weyra.runtime_state import BudgetExceeded

    class Runtime:
        async def close(self):
            pass

    monkeypatch.setattr(commands, "check_config", lambda *args: {})
    monkeypatch.setattr(
        commands, "load_config", lambda *args: SimpleNamespace(build_runtime=Runtime)
    )

    async def fake_demo(*args, budget, **kwargs):
        reservation = await budget.reserve(1, estimated_cost_cny="12")
        await budget.settle(reservation, 1, actual_cost_cny="12")
        return {"model_calls": 1}

    monkeypatch.setattr(demos, "run_demo", fake_demo)
    args = commands.parser().parse_args(["demo", "--project", str(tmp_path)])
    first = asyncio.run(commands.run(args))
    assert first["budget"]["accounted_cost_cny"] == "12"
    with pytest.raises(BudgetExceeded):
        asyncio.run(commands.run(args))


def test_demo_subbatch_limit_keeps_prior_activity_spend(tmp_path, monkeypatch):
    import asyncio
    import pytest
    from types import SimpleNamespace
    from weyra import commands, demos
    from weyra.runtime_state import Budget, BudgetExceeded
    from weyra.session import SessionStore

    class Runtime:
        async def close(self):
            pass

    monkeypatch.setattr(commands, "check_config", lambda *args: {})
    monkeypatch.setattr(
        commands, "load_config", lambda *args: SimpleNamespace(build_runtime=Runtime)
    )

    async def fake_demo(*args, budget, **kwargs):
        r = await budget.reserve(1, estimated_cost_cny="0")
        await budget.settle(r, 1, actual_cost_cny="0")
        return {}

    monkeypatch.setattr(demos, "run_demo", fake_demo)

    async def run():
        s = SessionStore(project=tmp_path, session_id="validation-live-cny20")
        b = Budget(cost_limit_cny="20")
        b.attach(s)
        for _ in range(8):
            r = await b.reserve(1, estimated_cost_cny="0")
            await b.settle(r, 1, actual_cost_cny="0")
        s.close()
        args = commands.parser().parse_args(["demo", "--project", str(tmp_path)])
        result = await commands.run(args)
        assert result["budget"]["calls"] == 9
        args.max_calls = 0
        with pytest.raises(BudgetExceeded):
            await commands.run(args)

    asyncio.run(run())

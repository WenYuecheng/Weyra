import os
import subprocess
import sys
from pathlib import Path
import pytest
from weyra.config import load_config


def invoke(*args):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    return subprocess.run(
        [sys.executable, "-m", "weyra.main", *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_help_and_offline_demo(tmp_path):
    assert invoke("--help").returncode == 0
    result = invoke("--demo", "--project", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "Weyra offline demo" in result.stdout
    assert (tmp_path / ".weyra" / "state.db").is_file()


def test_config_requires_limits_and_fallback(tmp_path):
    path = tmp_path / "weyra.toml"
    path.write_text(
        '[providers.local]\nprotocol="openai"\napi_key_env="LOCAL_KEY"\n[routes.main]\nprovider="local"\nmodel="local-model"\ncontext_window=65536\nmax_output_tokens=4096\n'
    )
    config = load_config(tmp_path)
    assert config.routes["memory"] == config.routes["main"]
    path.write_text(path.read_text().replace("context_window=65536\n", ""))
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_resume_traversal_and_missing_config(tmp_path):
    assert invoke("--project", str(tmp_path), "--resume", "../escape", "hello").returncode != 0
    result = invoke("--project", str(tmp_path), "hello")
    assert result.returncode != 0
    assert "context" in result.stderr.lower() or "configuration" in result.stderr.lower()


def test_config_rejects_plaintext_and_protocol(tmp_path):
    path = tmp_path / "weyra.toml"
    base = '[providers.test]\nprotocol="openai"\napi_key_env="TEST_API_KEY"\n[routes.main]\nprovider="test"\nmodel="test"\ncontext_window=65536\nmax_output_tokens=4096\n'
    path.write_text(base.replace('api_key_env="TEST_API_KEY"', 'api_key="secret-value"'))
    with pytest.raises(ValueError):
        load_config(tmp_path)
    path.write_text(base.replace('protocol="openai"', 'protocol="unknown"'))
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_single_input_approval_and_eof():
    import asyncio
    from types import SimpleNamespace
    from weyra.main import InputCoordinator

    async def run():
        inputs = InputCoordinator()
        pending = asyncio.create_task(inputs.confirm(SimpleNamespace(name="write"), {}, None))
        await asyncio.sleep(0)
        inputs._line("yes")
        assert await pending
        pending = asyncio.create_task(inputs.confirm(SimpleNamespace(name="shell"), {}, None))
        await asyncio.sleep(0)
        inputs._eof()
        assert not await pending
        assert not await inputs.confirm(SimpleNamespace(name="shell"), {}, None)

    asyncio.run(run())


def test_session_fork_and_refresh_commands(tmp_path):
    import asyncio
    from weyra.main import command, demo_runtime
    from weyra.agent import Agent

    async def run():
        agent = Agent(tmp_path, demo_runtime(tmp_path), enable_memory=False, enable_evolution=False)
        try:
            await command(agent, "/refresh")
            await command(agent, "/fork copied")
            assert any(row["session_id"] == "copied" for row in __import__("weyra.session",fromlist=["SessionStore"]).SessionStore.list_sessions(tmp_path))
            # The fork's writer is released immediately.
            from weyra.session import SessionStore

            store = SessionStore(project=tmp_path,session_id="copied")
            store.close()
        finally:
            await agent.close()

    asyncio.run(run())


def test_config_explicit_fallbacks(tmp_path):
    path = tmp_path / "weyra.toml"
    path.write_text(
        '[routes.main]\nprovider="deepseek"\nmodel="main"\ncontext_window=65536\nmax_output_tokens=4096\n[fallbacks]\nmain=[{provider="openai",model="backup",context_window=65536,max_output_tokens=4096}]\n'
    )
    config = load_config(tmp_path)
    assert config.fallbacks["main"][0].provider_id == "openai"
    runtime = config.build_runtime()
    assert runtime.fallbacks == config.fallbacks
    assert set(runtime.registry.providers) == {"deepseek", "openai"}


def test_branch_command_dispatch():
    import asyncio
    from types import SimpleNamespace
    from weyra.main import command

    called = []
    agent = SimpleNamespace(_running=False, branch=lambda record: called.append(record))
    asyncio.run(command(agent, "/branch record123"))
    assert called == ["record123"]


def test_mcp_config_and_model_capabilities(tmp_path):
    path = tmp_path / "weyra.toml"
    base = '[routes.main]\nprovider="openai"\nmodel="test"\ncontext_window=65536\nmax_output_tokens=4096\nsupports_tools=false\nsupports_reasoning=true\nsupports_cache=false\n'
    path.write_text(
        base
        + '[mcp.servers.local]\ncommand="python"\nargs=["fixture.py"]\nenv={SERVER_TOKEN="LOCAL_SERVER_TOKEN"}\n'
    )
    config = load_config(tmp_path)
    assert config.routes["main"].supports_tools is False
    assert config.routes["main"].supports_reasoning is True
    assert config.mcp_servers["local"]["env"] == {"SERVER_TOKEN": "LOCAL_SERVER_TOKEN"}
    assert config.build_runtime().mcp_servers == config.mcp_servers
    path.write_text(path.read_text() + "trusted=true\n")
    with pytest.raises(ValueError):
        load_config(tmp_path)
    path.write_text(base.replace("supports_tools=false", 'supports_tools="false"'))
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_mcp_commands_do_not_display_environment(capsys):
    import asyncio
    from types import SimpleNamespace
    from weyra.main import command

    calls = []
    client = SimpleNamespace(
        command="python", args=["fixture.py"], trusted=False, env={"SECRET": "hidden-value"}
    )
    agent = SimpleNamespace(
        _running=False,
        mcp=SimpleNamespace(clients={"local": client}),
        trust_mcp=lambda name, authorized=False: calls.append((name, authorized)),
    )
    asyncio.run(command(agent, "/mcp"))
    output = capsys.readouterr().out
    assert "python" in output and "hidden-value" not in output
    asyncio.run(command(agent, "/mcp-trust local"))
    assert calls == [("local", True)]
    agent._running = True
    with pytest.raises(ValueError):
        asyncio.run(command(agent, "/mcp-trust local"))

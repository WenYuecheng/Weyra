import asyncio

import pytest

from weyra.agent import Agent
from weyra.config import load_config
from weyra.demos import scripted_runtime


def test_self_evolution_is_opt_in(tmp_path):
    async def check():
        agent = Agent(tmp_path, scripted_runtime([]))
        assert agent.enable_evolution is False
        await agent.close()
    asyncio.run(check())


def test_enabled_evolution_requires_explicit_judge_even_with_callback(tmp_path):
    async def gate(old, candidate):
        return {}
    with pytest.raises(ValueError, match='skill_judge'):
        Agent(tmp_path, scripted_runtime([]), enable_evolution=True,
              evolution_validation_gate=gate)


def test_config_exposes_separate_judge_without_inheriting_it(tmp_path, monkeypatch):
    monkeypatch.setenv('PROVIDER', 'openai')
    monkeypatch.setenv('MODEL', 'gpt-4o')
    monkeypatch.setenv('CONTEXT_WINDOW', '128000')
    monkeypatch.setenv('MAX_OUTPUT_TOKENS', '4096')
    (tmp_path / 'weyra.toml').write_text('''
[routes.skill_judge]
provider = "openai"
model = "gpt-5.6-luna"
context_window = 128000
max_output_tokens = 16384
[skill_evolution]
enabled = false
judge_route = "skill_judge"
''')
    config = load_config(tmp_path)
    runtime = config.build_runtime()
    assert runtime.routes['skill_judge'].model_id == 'gpt-5.6-luna'
    assert 'skill_judge' not in runtime.inherited_purposes
    assert runtime.skill_evolution['enabled'] is False


def test_config_without_judge_does_not_invent_one(tmp_path, monkeypatch):
    monkeypatch.setenv('PROVIDER', 'deepseek')
    monkeypatch.setenv('MODEL', 'deepseek-chat')
    monkeypatch.setenv('CONTEXT_WINDOW', '128000')
    monkeypatch.setenv('MAX_OUTPUT_TOKENS', '4096')
    config = load_config(tmp_path)
    assert 'skill_judge' not in config.routes
    assert config.skill_evolution['enabled'] is False


def test_enabled_config_requires_host_manifest_and_judge(tmp_path, monkeypatch):
    monkeypatch.setenv('PROVIDER', 'deepseek')
    monkeypatch.setenv('MODEL', 'deepseek-chat')
    monkeypatch.setenv('CONTEXT_WINDOW', '128000')
    monkeypatch.setenv('MAX_OUTPUT_TOKENS', '4096')
    (tmp_path / 'weyra.toml').write_text('[skill_evolution]\nenabled=true\n')
    with pytest.raises(ValueError, match='skill_judge'):
        load_config(tmp_path)


def test_validation_manifest_is_hidden_from_agent_file_tools(tmp_path):
    from weyra.tool_runtime import ToolRuntime
    from weyra.session import SessionStore
    (tmp_path / 'private-grader.json').write_text('HOST_ONLY_GOLD')
    session = SessionStore(project=tmp_path)
    tools = ToolRuntime(tmp_path, session, 'acceptEdits')
    tools.private_paths = {tmp_path / 'private-grader.json'}
    with pytest.raises(PermissionError):
        tools.resolve_path('private-grader.json')
    assert tools.resolve_path('ordinary.txt') == tmp_path / 'ordinary.txt'
    session.close()

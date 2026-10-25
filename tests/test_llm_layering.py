"""Model transport can be used without the agent application."""
import ast
import importlib
from pathlib import Path


def test_neutral_model_types_have_shared_identity():
    from weyra import contracts
    from weyra.llm import types
    for name in ('Message', 'ToolCall', 'ModelInfo', 'Usage', 'ModelResponse',
                 'StreamEvent', 'PreparedRequest'):
        assert getattr(contracts, name) is getattr(types, name)


def test_legacy_runtime_is_same_module_and_provider_identity():
    legacy = importlib.import_module('weyra.model_runtime')
    runtime = importlib.import_module('weyra.core.model_runtime')
    providers = importlib.import_module('weyra.llm.providers')
    assert legacy is runtime
    for name in ('ProviderRegistry', 'OpenAIProvider', 'AnthropicProvider', 'ScriptedProvider'):
        assert getattr(legacy, name) is getattr(providers, name)


def test_llm_has_no_agent_application_dependencies():
    from weyra import llm
    for source in Path(llm.__file__).parent.glob('*.py'):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.level <= 1, (source, node.module)
                assert not (node.module or '').startswith(('weyra.core', 'weyra.coding'))


def test_retry_policy_preserves_dsh_delay_and_defaults():
    from weyra.core.retry import RetryPolicy
    policy = RetryPolicy()
    assert policy.max_retries == 2
    assert policy.delay(0, uniform=lambda low, high: 1.0) == 0.5
    assert policy.delay(1, uniform=lambda low, high: 1.0) == 1.0
    assert policy.delay(8, uniform=lambda low, high: 1.1) == 11.0


def test_llm_import_does_not_load_execution_or_application():
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, '-c',
         'import sys; from weyra.llm.providers import OpenAIProvider, default_model; '
         'from weyra.llm.types import Message, PreparedRequest; '
         'default_model("custom", "offline", 1000, 100); '
         'assert not any(n.startswith(("weyra.core", "weyra.coding")) for n in sys.modules)'],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_default_retry_sequence_and_legacy_patch_path(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from weyra.model_runtime import (
        ModelError, ModelRuntime, ProviderRegistry, ScriptedProvider,
    )
    from weyra.llm.types import ModelInfo, ModelResponse, PreparedRequest

    delays = []
    calls = []

    async def sleep(delay):
        delays.append(delay)

    registry = ProviderRegistry()
    registry.register('offline', ScriptedProvider())
    model = ModelInfo('offline', 'model', 1000, 100)
    runtime = ModelRuntime(registry, {'main': model})

    async def stream_once(request, context):
        calls.append(request)
        if len(calls) < 3:
            raise ModelError('transient')
        from weyra.llm.types import StreamEvent
        yield StreamEvent('response', ModelResponse('done'))

    monkeypatch.setattr(runtime, '_stream_once', stream_once)
    monkeypatch.setattr('weyra.model_runtime.asyncio.sleep', sleep)
    monkeypatch.setattr('weyra.model_runtime.random.uniform', lambda low, high: 1.0)
    request = PreparedRequest(model, '', ())
    result = asyncio.run(runtime.complete(
        request, SimpleNamespace(check_cancelled=lambda: None),
    ))
    assert result.content == 'done'
    assert delays == [0.5, 1.0]
    assert len(calls) == 3 and all(item is request for item in calls)

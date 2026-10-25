"""Exercise public SDK examples against real runtime/service contracts offline."""
import asyncio
from pathlib import Path
import re
from types import SimpleNamespace

from weyra.contracts import ModelResponse
from tests.test_agent import scripted


def test_online_sdk_example_with_local_provider(tmp_path, monkeypatch, capsys):
    manual = Path('docs/API_REFERENCE.md').read_text()
    example = re.search(r'```python\n(.*?)```', manual, re.S).group(1)
    runtime, provider = scripted([ModelResponse('project summary')])
    monkeypatch.setattr('weyra.config.load_config', lambda project: SimpleNamespace(
        build_runtime=lambda: runtime))
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'README.md').write_text('SDK fixture')
    exec(compile(example, 'docs/API_REFERENCE.md', 'exec'), {'__name__': '__main__'})
    assert 'project summary' in capsys.readouterr().out
    assert len(provider.requests) == 1


def test_offline_service_example_reaches_completion(capsys):
    from examples.sdk_service import main
    asyncio.run(main())
    assert 'Weyra SDK ready.' in capsys.readouterr().out

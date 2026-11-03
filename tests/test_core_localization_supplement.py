import importlib.util
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_entrypoint():
    spec = importlib.util.spec_from_file_location(
        'localization_supplement', ROOT / 'scripts/reports/core_localization_supplement.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_supplement_has_unique_balanced_inputs():
    module = load_entrypoint()
    manifest, freeze = module.load_frozen_manifest(ROOT)
    assert len(manifest['cases']) == freeze['cases'] == 60
    assert len({json.dumps(c['input'], sort_keys=True) for c in manifest['cases']}) == 60
    assert set(Counter(c['category'] for c in manifest['cases']).values()) == {10}
    assert len(Counter(c['category'] for c in manifest['cases'])) == 6


def test_frozen_hash_rejects_changes(tmp_path):
    import pytest
    module = load_entrypoint()
    (tmp_path / 'wiki').mkdir()
    (tmp_path / 'benchmarks/data').mkdir(parents=True)
    (tmp_path / 'wiki/core-localization-v2-freeze.json').write_bytes(
        (ROOT / 'wiki/core-localization-v2-freeze.json').read_bytes()
    )
    (tmp_path / 'benchmarks/data/core-localization-v2.json').write_text('{}')
    with pytest.raises(ValueError, match='hash'):
        module.load_frozen_manifest(tmp_path)


def test_cli_refuses_existing_result_without_running(tmp_path, monkeypatch):
    import pytest
    module = load_entrypoint()
    output = tmp_path / 'result.json'
    output.write_text('original')
    def forbidden_execution():
        raise AssertionError('Existing result must stop before execution')
    monkeypatch.setattr(module, 'execute', forbidden_execution)
    with pytest.raises(FileExistsError):
        module.main(['--output', str(output)])
    assert output.read_text() == 'original'

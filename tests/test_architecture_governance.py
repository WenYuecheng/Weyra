import importlib
from pathlib import Path


def test_historical_imports_are_source_compatibility_aliases():
    for path in Path('weyra').glob('*.py'):
        if path.stem.startswith(('core_', 'finish_', 'final_')):
            old = importlib.import_module('weyra.' + path.stem)
            new = importlib.import_module('benchmarks.' + path.stem)
            assert old is new


def test_source_manifest_covers_nested_runtime_and_benchmark_data(tmp_path):
    from benchmarks._sources import source_manifest
    for name in ('weyra/tui/app.py', 'benchmarks/core_evaluation.py', 'benchmarks/data/core-eval-v1.json', 'data/upstream/prompt.txt'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('first', encoding='utf-8')
    first = source_manifest(tmp_path)
    assert len(first) == 4
    path.write_text('second', encoding='utf-8')
    assert source_manifest(tmp_path)[str(path.relative_to(tmp_path))] != first[str(path.relative_to(tmp_path))]


def test_runtime_data_and_dependency_sources_are_separate():
    import tomllib
    config = tomllib.loads(Path('pyproject.toml').read_text())
    data = config['tool']['setuptools']['data-files']['share/weyra/data']
    assert data == ['data/scenarios.json']
    assert Path('requirements.txt').read_text().strip() == '-e .'


def test_legacy_source_entrypoint_works_outside_checkout(tmp_path):
    import os
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-m', 'weyra.final_delivery_eval', '--help'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    assert result.returncode == 0, result.stderr
    assert '--output' in result.stdout

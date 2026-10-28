"""Verify the core runtime wheel and source archive offline; temporary installs self-clean."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def package_environment():
    """Keep clean installs independent of the developer's Python paths."""
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(key, None)
    env['PYTHONNOUSERSITE'] = '1'
    return env


def verify():
    wheel = max((ROOT / 'dist').glob('weyra-*.whl'), key=lambda p: p.stat().st_mtime)
    archive = wheel.with_name(wheel.name.split('-py3-')[0] + '.tar.gz')
    checks = []
    with zipfile.ZipFile(wheel) as contents:
        entries = contents.infolist()
        assert not any(e.filename.startswith(('benchmarks/', 'agents/')) for e in entries)
        assert not any('/data/' in e.filename and Path(e.filename).name.startswith(('core-', 'finish-', 'final-')) for e in entries)
        assert any(e.filename.endswith('theme.tcss') for e in entries)
        assert not any(e.filename == 'weyra/_benchmark_compat.py' for e in entries)
        unpacked = sum(e.file_size for e in entries)
    env = package_environment()
    with tempfile.TemporaryDirectory(prefix='weyra-package-check-') as folder:
        work = Path(folder)

        def run(name, args, *, cwd=work, expected=0, environment=env):
            result = subprocess.run([str(a) for a in args], cwd=cwd, env=environment,
                                    capture_output=True, text=True, timeout=300)
            checks.append({'name': name, 'exit_code': result.returncode,
                           'passed': result.returncode == expected, 'output': (result.stdout + result.stderr)[-8000:]})
            if result.returncode != expected:
                raise RuntimeError(name + ': ' + checks[-1]['output'])
            print(name + ': PASS', flush=True)
            return result

        run('isolated_environment', [sys.executable, '-m', 'venv', work / 'env'])
        python = work / 'env/bin/python'
        cli = work / 'env/bin/weyra'
        run('wheel_install', [python, '-m', 'pip', 'install', wheel])
        run('installed_dependencies', [python, '-m', 'pip', 'check'])
        (work / 'agents').mkdir()
        (work / 'agents/__init__.py').write_text('unrelated = True\n', encoding='utf-8')
        run('wheel_resources', [python, '-c',
            "import weyra,agents; from weyra.evaluation import load_cases; "
            "assert '/env/lib/' in weyra.__file__; assert agents.unrelated; assert len(load_cases()) == 20"])
        run('cli_help', [cli, '--help'])
        config = work / 'configuration'
        config.mkdir()
        (config / 'weyra.toml').write_text('[routes.main]\nprovider="deepseek"\nmodel="deepseek-chat"\ncontext_window=131072\nmax_output_tokens=8192\n[context_policy]\nheadroom_tokens=4096\nsummary_max_tokens=1024\n', encoding='utf-8')
        run('check_config', [cli, 'check-config', '--project', config])
        for scenario in ('analysis', 'writing'):
            run(scenario + '_demo', [cli, 'demo', '--offline', '--scenario', scenario, '--project', work / scenario])
        run('offline_evaluation', [cli, 'eval', '--offline', '--project', work / 'evaluation'])
        run('sdist_install', [python, '-m', 'pip', 'install', '--force-reinstall', archive])
        run('sdist_dependencies', [python, '-m', 'pip', 'check'])
        run('sdist_cli_help', [cli, '--help'])
        source_root = work / 'source'
        source_root.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(source_root, filter='data')
        extracted = next(source_root.iterdir())
        source_env = {**env, 'PYTHONPATH': str(extracted)}
        run('source_resources', [python, '-c',
            "from pathlib import Path; import weyra; from weyra.evaluation import load_cases; "
            "assert Path(weyra.__file__).resolve().is_relative_to(Path.cwd()); "
            "assert len(load_cases()) == 20"], cwd=extracted, environment=source_env)
    assert not work.exists()
    return {'passed': True, 'version': wheel.name.split('-')[1], 'wheel': str(wheel),
            'compressed_bytes': wheel.stat().st_size, 'uncompressed_bytes': unpacked,
            'temporary_environment_removed': True, 'checks': checks}


def main():
    report = verify()
    destination = ROOT / '.benchmarks/results/engineering/package.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

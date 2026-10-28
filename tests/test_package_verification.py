"""Keep package smoke checks independent of the developer's Python environment."""
import os

from scripts import verify_package


def test_package_environment_removes_host_python_paths(monkeypatch):
    monkeypatch.setenv('PYTHONPATH', '/host/site-packages')
    monkeypatch.setenv('PYTHONHOME', '/host/python')
    monkeypatch.setenv('VIRTUAL_ENV', '/host/venv')
    monkeypatch.setenv('PATH', os.environ['PATH'])

    environment = verify_package.package_environment()

    assert 'PYTHONPATH' not in environment
    assert 'PYTHONHOME' not in environment
    assert 'VIRTUAL_ENV' not in environment
    assert environment['PYTHONNOUSERSITE'] == '1'
    assert environment['PATH'] == os.environ['PATH']
    assert os.environ['PYTHONPATH'] == '/host/site-packages'

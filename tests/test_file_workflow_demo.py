import asyncio
import importlib.util
from pathlib import Path


def test_file_demo_runs_real_tools_preserves_input(tmp_path):
    source = Path(__file__).parents[1] / 'examples' / 'file_workflow.py'
    spec = importlib.util.spec_from_file_location('file_workflow', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = asyncio.run(module.run_file_demo(tmp_path))
    assert report['passed']
    assert report['actual_file_reads'] >= 1
    assert report['actual_file_writes'] >= 1
    assert report['input_unchanged']
    assert report['output'] == {'count': 2, 'total': 12}

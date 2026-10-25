"""Offline real-file demo. Scripted responses prove the tool path, not model quality."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile

from weyra.agent import Agent
from weyra.contracts import ModelResponse, ToolCall, Usage
from weyra.demos import scripted_runtime


async def run_file_demo(project):
    destination = Path(project).resolve() / '.weyra' / 'demos'
    destination.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix='files-', dir=destination))
    source = fixture / 'input.json'
    source.write_text('[{"amount":5},{"amount":7}]', encoding='utf-8')
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    runtime = scripted_runtime([
        ModelResponse('', tool_calls=(ToolCall('read', 'read_file', {'path': 'input.json'}),), usage=Usage(12, 4)),
        ModelResponse('', tool_calls=(ToolCall('write', 'write_file', {
            'path': 'summary.json', 'content': '{"count":2,"total":12}'
        }),), usage=Usage(12, 4)),
        ModelResponse('已读取 input.json 并写入 summary.json，保留原始文件。', usage=Usage(12, 4)),
    ])
    agent = Agent(fixture, runtime, permission_mode='acceptEdits', enable_memory=False,
                  enable_evolution=False, owns_runtime=False)
    try:
        answer = await agent.run_once('读取 input.json，统计条目数及amount总和，写入summary.json；不修改输入。')
        output = json.loads((fixture / 'summary.json').read_text())
        tools = [r['data'] for r in agent.session.records if r['kind'] == 'tool_result']
        unchanged = original == hashlib.sha256(source.read_bytes()).hexdigest()
        report = {'offline': True, 'quality_evidence': False, 'answer': answer,
                  'workspace': str(fixture), 'output': output, 'input_unchanged': unchanged,
                  'actual_file_reads': sum(t.get('name') == 'read_file' and t.get('status') == 'success' for t in tools),
                  'actual_file_writes': sum(t.get('name') == 'write_file' and t.get('status') == 'success' for t in tools)}
        report['passed'] = unchanged and output == {'count': 2, 'total': 12} and report['actual_file_reads'] > 0 and report['actual_file_writes'] > 0
        (fixture / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    finally:
        await agent.close()
        await runtime.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    print(json.dumps(asyncio.run(run_file_demo(parser.parse_args().project)), ensure_ascii=False, indent=2))

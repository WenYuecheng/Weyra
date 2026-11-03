import json
from benchmarks.polyglot.dataset import discover


def test_full_public_instructions_in_exact_upstream_order(tmp_path):
    task = tmp_path / 'python/exercises/practice/sample'
    (task / '.meta').mkdir(parents=True)
    (task / '.docs').mkdir()
    (task / '.meta/config.json').write_text(json.dumps({'files': {
        'solution': ['sample.py'], 'test': ['sample_test.py'], 'example': ['.meta/example.py']}}))
    for name, text in {'sample.py': 'pass', 'sample_test.py': 'SECRET TEST',
                       '.meta/example.py': 'SECRET ANSWER', '.docs/introduction.md': 'INTRO\n',
                       '.docs/instructions.md': 'BODY\n', '.docs/instructions.append.md': 'APPEND\n',
                       '.docs/instructions.other.md': 'NOT PUBLIC'}.items():
        (task / name).write_text(text)
    discovered = discover(tmp_path)[0]
    assert discovered.instruction == 'INTRO\n\n\nBODY\n\n\nAPPEND\n'
    assert discovered.files == {'sample.py': 'pass', 'INSTRUCTIONS.md': discovered.instruction}

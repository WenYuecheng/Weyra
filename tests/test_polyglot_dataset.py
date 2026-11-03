import json
from benchmarks.polyglot.dataset import discover, rank


def test_discovery_never_stages_answers_or_tests(tmp_path):
    p = tmp_path / "python/exercises/practice/x"
    (p / ".meta").mkdir(parents=True)
    (p / ".docs").mkdir()
    (p / ".meta/config.json").write_text(
        json.dumps(
            {
                "files": {
                    "solution": ["x.py"],
                    "test": ["x_test.py"],
                    "example": [".meta/example.py"],
                }
            }
        )
    )
    for n, t in [
        ("x.py", "pass"),
        ("x_test.py", "secret"),
        (".meta/example.py", "answer"),
        (".docs/instructions.md", "implement"),
    ]:
        (p / n).write_text(t)
    t = discover(tmp_path)[0]
    assert set(t.files) == {"x.py", "INSTRUCTIONS.md"}
    assert t.solutions == {"x.py": "answer"}
    assert "secret" not in str(t.files) and "answer" not in str(t.files)
    assert rank(t) == rank(t)

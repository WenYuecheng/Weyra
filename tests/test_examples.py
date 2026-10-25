import json
import subprocess
import sys
from pathlib import Path


def test_project_analysis_demo():
    result = subprocess.run(
        [sys.executable, "examples/project_analysis.py"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["child_state"] == "completed"
    assert report["actual_tool_results"] >= 3
    assert report["paid_model_calls"] == 0


def test_personalized_writing_demo():
    result = subprocess.run(
        [sys.executable, "examples/personalized_writing.py"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["updated_version"] == "1.0.1"
    assert report["rollback_version"] == "1.0.2"
    assert report["boundary_experience"]
    assert report["paid_model_calls"] == 0


def test_fixed_scenario_manifest_has_twenty_cases():
    index = json.loads(Path("examples/scenarios.json").read_text())
    cases = json.loads((Path("examples") / index["source"]).resolve().read_text())
    assert len(cases) == 20
    assert len({case["id"] for case in cases}) == 20
    assert sum(case["family"] == "project" for case in cases) == 10
    assert sum(case["family"] == "writing" for case in cases) == 10


def test_unified_demo_runs_both_scenarios(tmp_path):
    import asyncio
    from weyra.demos import run_demo

    async def run():
        analysis = await run_demo("analysis", project=tmp_path / "analysis")
        writing = await run_demo("writing", project=tmp_path / "writing")
        assert analysis["scenario"] == "analysis" and analysis["mode"] == "offline"
        assert analysis["child_state"] == "completed"
        assert analysis["actual_tool_results"] >= 3
        assert writing["updated_version"] == "1.0.1"
        assert writing["rollback_version"] == "1.0.2"
        assert "no quality claim" in writing["model"]

    asyncio.run(run())


def test_live_demo_requires_constructed_runtime():
    import asyncio
    import pytest
    from weyra.demos import run_demo

    with pytest.raises(ValueError, match="runtime"):
        asyncio.run(run_demo("analysis", offline=False))


def test_demo_keeps_inspectable_session_and_report(tmp_path):
    import asyncio
    from weyra.demos import run_demo

    report = asyncio.run(run_demo("analysis", project=tmp_path))
    folder = Path(report["fixture_project"])
    assert folder.is_relative_to(tmp_path)
    assert (folder / ".weyra/state.db").is_file()
    assert (
        json.loads((folder / ".weyra/demo-report.json").read_text())["session_id"]
        == report["session_id"]
    )


def test_writing_demo_uses_the_created_skill_identity(tmp_path, monkeypatch):
    import asyncio
    from weyra import demos
    original = demos._offline_runtime
    providers = []

    def renamed(scenario):
        runtime = original(scenario)
        provider = runtime.registry.get('maintenance')
        providers.append(provider)
        for response in provider.responses:
            data = json.loads(response.content)
            data['name'] = 'formal-reporting'
            if data['action'] == 'create':
                data['description'] = 'English reporting procedure'
                data['body'] = 'Give the conclusion first for all writing.'
            elif data['action'] == 'apply':
                data['edits'][0]['old'] = 'Give the conclusion first for all writing.'
            object.__setattr__(response, 'content', json.dumps(data, ensure_ascii=False))
        return runtime

    monkeypatch.setattr(demos, '_offline_runtime', renamed)
    report = asyncio.run(demos.run_demo('writing', project=tmp_path))
    assert report['updated_version'] == '1.0.1'
    assert report['rollback_version'] == '1.0.2'
    assert report['skill_name'] == 'formal-reporting'
    payload = json.loads(providers[0].requests[-1].messages[0].content)
    assert payload['target']['name'] == 'formal-reporting'


def test_writing_demo_evidence_contains_a_durable_scope_correction(tmp_path):
    import asyncio
    import sqlite3
    from weyra.demos import run_demo

    report = asyncio.run(run_demo('writing', project=tmp_path))
    connection = sqlite3.connect(Path(report['fixture_project']) / '.weyra/state.db')
    records = [json.loads(row[0]) for row in connection.execute("select data from events where kind='message' order by seq")]
    connection.close()
    users = [r['content'] for r in records if r.get('role') == 'user' and r.get('source') == 'user']
    assert '所有写作' in users[0]
    assert '维护为可复用技能' not in users[0]
    assert '长期' in users[-1] and '故事' in users[-1]

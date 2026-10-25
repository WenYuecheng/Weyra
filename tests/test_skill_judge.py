import asyncio
import json
from types import SimpleNamespace

import pytest

from weyra.contracts import ModelInfo, ModelResponse
from weyra.runtime_state import Budget
from weyra.skill_judge import SkillJudge


class FakeRuntime:
    def __init__(self, responses):
        self.routes = {"skill_judge": ModelInfo("openai", "gpt-5.6-luna", 8192, 512)}
        self.responses = list(responses)
        self.requests = []

    async def complete(self, request, context):
        self.requests.append(request)
        return self.responses.pop(0)


def judgment(winner="A", score_a=True, score_b=False):
    return json.dumps({
        "scores": {
            "A": {"correctness": score_a, "constraint_adherence": score_a,
                  "preservation": score_a, "clarity": score_a},
            "B": {"correctness": score_b, "constraint_adherence": score_b,
                  "preservation": score_b, "clarity": score_b},
        },
        "winner": winner,
        "evidence": {"A": "A evidence", "B": "B evidence"},
    })


def test_judge_uses_two_swapped_blind_calls_and_normalizes_agreement(tmp_path):
    runtime = FakeRuntime([ModelResponse(judgment("B", False, True)), ModelResponse(judgment("A"))])
    result = asyncio.run(SkillJudge(runtime, Budget()).compare(
        {"id": "case-1", "prompt": "do task"},
        {"output": "old output"}, {"output": "candidate output"},
    ))
    assert result["preferred"] == "candidate"
    assert result["model_approved"] is True
    assert result["scores"]["candidate"] == 4
    assert result["scores"]["old"] == 0
    assert len(runtime.requests) == 2
    first = runtime.requests[0].messages[0].content
    second = runtime.requests[1].messages[0].content
    assert '"A"' in first and '"B"' in first
    assert 'old output' in first and first.index('old output') < first.index('candidate output')
    assert 'candidate output' in second and second.index('candidate output') < second.index('old output')


def test_judge_rejects_swapped_winner_disagreement():
    runtime = FakeRuntime([ModelResponse(judgment()), ModelResponse(judgment("A"))])
    result = asyncio.run(SkillJudge(runtime, Budget()).compare({}, {"x": 1}, {"x": 2}))
    assert result["preferred"] == "inconsistent"
    assert result["model_approved"] is False


@pytest.mark.parametrize("content", [
    "not json",
    json.dumps({"scores": {}, "winner": "A", "evidence": {"A": "x", "B": "y"}}),
    json.dumps({"scores": {"A": {"correctness": 1}, "B": {}}, "winner": "A",
                "evidence": {"A": "x", "B": "y"}}),
])
def test_judge_rejects_malformed_or_non_boolean_schema(content):
    runtime = FakeRuntime([ModelResponse(content), ModelResponse(judgment("B"))])
    result = asyncio.run(SkillJudge(runtime, Budget()).compare({}, {}, {}))
    assert result["preferred"] == "inconsistent"
    assert result["model_approved"] is False


def test_judge_requires_explicit_luna_route():
    runtime = FakeRuntime([ModelResponse(judgment("B", False, True)), ModelResponse(judgment("A"))])
    runtime.routes = {"main": ModelInfo("openai", "gpt-5.6-luna", 8192, 512)}
    with pytest.raises(ValueError, match="skill_judge"):
        SkillJudge(runtime, Budget())


def test_gate_rejects_vacuous_no_artifact_validation():
    from weyra.skill_judge import ConfiguredSkillGate
    with pytest.raises(ValueError, match='observable output'):
        ConfiguredSkillGate(FakeRuntime([]), Budget(), {'cases': [
            {'id': 'vacuous', 'prompt': 'Just say done', 'files': {}}
        ]})


def test_json_artifacts_allow_whitespace_and_key_order_but_preserve_values():
    from weyra.skill_judge import artifact_text_equal
    assert artifact_text_equal('counts.json', '{"b": 2, "a": 1}', '{"a":1,"b":2}')
    assert not artifact_text_equal('counts.json', '{"a":2,"b":2}', '{"a":1,"b":2}')


def test_configured_gate_executes_real_skill_and_file_tools(tmp_path):
    from weyra.contracts import ToolCall
    from weyra.demos import scripted_runtime
    from weyra.skill_judge import ConfiguredSkillGate
    runtime = scripted_runtime([
        ModelResponse(tool_calls=(ToolCall('s', 'skill', {'name': 'demo'}),
                                  ToolCall('r', 'read_file', {'path': 'input.txt'}))),
        ModelResponse(tool_calls=(ToolCall('w', 'write_file', {'path': 'out.txt', 'content': 'valid'}),)),
        ModelResponse('done'),
    ])
    runtime.routes['skill_judge'] = ModelInfo('offline', 'gpt-5.6-luna', 200000, 1024)
    manifest = {'cases': [{'id': 'io', 'prompt': 'Read input.txt and write out.txt.',
                          'files': {'input.txt': 'original'}, 'expected_artifacts': {'out.txt': 'valid'}}]}
    gate = ConfiguredSkillGate(runtime, Budget(), manifest)
    snapshot = SimpleNamespace(name='demo', body='Read input.txt then write out.txt.', content_hash='abc')
    outcome = asyncio.run(gate._execute(snapshot, gate.cases[0]))
    assert outcome['passed'] and outcome['hard_failures'] == 0
    assert all(check['passed'] for check in outcome['hard_checks'])


def test_configured_gate_successes_require_model_quality_not_only_hard_checks():
    from weyra.skill_judge import ConfiguredSkillGate
    runtime = FakeRuntime([ModelResponse(judgment('B', False, True)),
                           ModelResponse(judgment('A'))])
    gate = ConfiguredSkillGate(runtime, Budget(), {'cases': [
        {'id': 'quality', 'prompt': 'Write clearly', 'files': {'in.txt': 'fact'},
         'expected_artifacts': {'out.txt': 'fact'}}]})

    async def execute(snapshot, case):
        return {'final': snapshot, 'artifacts': {'out.txt': 'fact'},
                'hard_checks': [{'name': 'integrity', 'passed': True}],
                'hard_failures': 0, 'passed': True}
    gate._execute = execute
    report = asyncio.run(gate('old', 'candidate'))
    assert report['old']['successes'] == 0
    assert report['candidate']['successes'] == 1
    assert report['model_approved'] is True


def test_reviewed_callback_requires_task_and_actual_output_bundles():
    from weyra.skill_judge import ReviewedCallbackGate

    runtime = FakeRuntime([])
    def callback(old, candidate):
        return {
            "old": {"successes": 0, "hard_failures": 0},
            "candidate": {"successes": 1, "hard_failures": 0},
        }
    gate = ReviewedCallbackGate(runtime, Budget(), callback)
    with pytest.raises(ValueError, match="old_artifacts"):
        asyncio.run(gate(object(), object()))


def test_reviewed_callback_preserves_host_counts_and_adds_judgment():
    from weyra.skill_judge import ReviewedCallbackGate

    runtime = FakeRuntime([ModelResponse(judgment("B", False, True)),
                           ModelResponse(judgment("A"))])
    def callback(old, candidate):
        return {
            "task": {"prompt": "Create the requested file."},
            "old_artifacts": {"result.txt": "incorrect"},
            "candidate_artifacts": {"result.txt": "correct"},
            "old": {"successes": 0, "hard_failures": 0},
            "candidate": {"successes": 1, "hard_failures": 0},
        }
    result = asyncio.run(ReviewedCallbackGate(runtime, Budget(), callback)(object(), object()))
    assert result["old"]["successes"] == 0
    assert result["candidate"]["successes"] == 1
    assert result["model_approved"] is True
    assert len(runtime.requests) == 2

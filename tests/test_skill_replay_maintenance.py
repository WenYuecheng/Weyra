import asyncio

from weyra.skills import SkillStore
from weyra.skill_evolution import EvolutionStore
from weyra.skill_replay import freeze_window


def setup(tmp_path):
    path = tmp_path / ".weyra/skills/demo/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: demo\nversion: 1.0.0\n---\nRead carefully.\n")
    skills = SkillStore(tmp_path, [])
    return EvolutionStore(skills), skills.load("demo")


def window(cases=()):
    return freeze_window({"user": "Explain facts", "feedback": "Always cite evidence",
                          "replay": {"complete": True, "turn_id": "t", "files": {}}}, cases)


async def proposal(_):
    return {"action": "apply", "name": "demo", "edits": [{"op": "append", "text": "Cite evidence."}],
            "knowledge_note": {"kind": "boundary", "text": "The user requires evidence citations."}}


class Gate:
    requires_model_review = True
    def __init__(self, *, saturated=False, approved=True):
        self.full, self.approved, self.calls = saturated, approved, []
    def saturated(self, old, context):
        return self.full
    async def evaluate(self, old, candidate, replay_window, **kwargs):
        self.calls.append((old, candidate, replay_window, kwargs))
        return {"old": {"successes": 0, "hard_failures": 0},
                "candidate": {"successes": 1, "hard_failures": 0},
                "model_approved": self.approved, "validation_policy": "bounded-replay-v1",
                "window_version": replay_window.version, "reason": "approved" if self.approved else "no_validation_gain"}


def validation():
    return [{"id": "heldout", "prompt": "Another factual request", "files": {},
             "output_mode": "text", "rubric": "Cite evidence"}]


def test_missing_validation_retains_candidate_and_knowledge_without_publication(tmp_path):
    store, old = setup(tmp_path)
    gate = Gate()
    result = asyncio.run(store.maintain({}, proposal, authorized=True, target_snapshot=old,
                                      validation_gate=gate, validation_context=window()))
    assert result["status"] == "incubating"
    assert result["reason"] == "insufficient_independent_validation"
    assert gate.calls == []
    assert old.path.read_text().endswith("Read carefully.\n")
    assert result["proposal_ref"]
    assert store.knowledge("demo")[0]["kind"] == "boundary"


def test_saturated_window_stops_before_spending_on_new_proposal(tmp_path):
    store, old = setup(tmp_path)
    async def forbidden(_):
        raise AssertionError("saturated windows must not propose")
    result = asyncio.run(store.maintain({}, forbidden, authorized=True, target_snapshot=old,
                                      validation_gate=Gate(saturated=True), validation_context=window(validation())))
    assert result["reason"] == "validation_saturated"
    assert store.skills.load("demo").content_hash == old.content_hash


def test_bounded_report_controls_publication_and_is_retained(tmp_path):
    store, old = setup(tmp_path)
    gate = Gate()
    context = window(validation())
    result = asyncio.run(store.maintain({}, proposal, authorized=True, target_snapshot=old,
                                      validation_gate=gate, validation_context=context))
    assert result["status"] == "updated"
    assert result["validation_report"]["window_version"] == context.version
    assert gate.calls[0][2] is context
    assert store.skills.load("demo").version == "1.0.1"


def test_rejected_valid_candidate_preserves_user_knowledge(tmp_path):
    store, old = setup(tmp_path)
    result = asyncio.run(store.maintain({}, proposal, authorized=True, target_snapshot=old,
                                      validation_gate=Gate(approved=False), validation_context=window(validation())))
    assert result["status"] == "rejected"
    assert result["reason"] == "no_validation_gain"
    assert store.knowledge("demo")[0]["text"] == "The user requires evidence citations."
    assert store.skills.load("demo").version == "1.0.0"


def test_online_proposal_and_replays_share_scope_without_validation_leak(tmp_path):
    from weyra.agent import Agent
    from weyra.contracts import ModelResponse
    from weyra.demos import scripted_runtime
    import json
    setup(tmp_path)
    runtime = scripted_runtime([ModelResponse(json.dumps(asyncio.run(proposal({}))))])
    calls = []
    complete = runtime.complete
    async def record(request, context, **kwargs):
        calls.append((request, context.budget_scope))
        return await complete(request, context, **kwargs)
    runtime.complete = record
    class OnlineGate(Gate):
        def freeze(self, evidence):
            return freeze_window(evidence, [{"id": "secret", "prompt": "SECRET_HELDOUT",
                "files": {}, "output_mode": "text", "rubric": "Cite evidence"}])
    async def run():
        agent = Agent(tmp_path, runtime, permission_mode="acceptEdits", enable_memory=False)
        gate = OnlineGate()
        agent.evolution_validation_gate = gate
        agent._schedule_evolution({"user": "Explain facts", "feedback": "Always cite evidence",
            "used_skill_ids": ["demo"], "replay": {"turn_id": "t", "complete": True, "files": {}}})
        await asyncio.gather(*agent._background)
        assert agent.session.latest("evolution_result")["status"] == "updated"
        assert calls[0][1]
        assert gate.calls[0][3]["context"].budget_scope == calls[0][1]
        assert "SECRET_HELDOUT" not in calls[0][0].messages[0].content
        assert "replay" not in json.loads(calls[0][0].messages[0].content)["evidence"]
        await agent.close()
    asyncio.run(run())

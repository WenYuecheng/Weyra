import tempfile
import unittest
from pathlib import Path
from weyra.knowledge import SkillStore
from weyra.skill_evolution import EvolutionStore


class EvolutionTests(unittest.TestCase):
    def test_permissions_patches_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "skills" / "demo"
            p.mkdir(parents=True)
            (p / "SKILL.md").write_text("---\nname: demo\nversion: 1.0.0\n---\nhello world")
            skills = SkillStore(d)
            e = EvolutionStore(skills)
            original = skills.load("demo")
            args = dict(
                name="demo",
                edits=[{"type": "replace", "old": "world", "new": "earth"}],
                base_hash=original.content_hash,
                event_id="event",
            )
            self.assertEqual(e.apply(**args)["status"], "rejected")
            self.assertEqual(e.apply(**args, authorized=True)["status"], "updated")
            current = skills.load("demo")
            self.assertEqual(current.body, "hello earth")
            self.assertEqual(
                e.apply(
                    "demo",
                    [{"type": "append", "text": "hello earth"}],
                    "two",
                    current.content_hash,
                    authorized=True,
                )["status"],
                "rejected",
            )
            self.assertEqual(e.rollback("demo", "1.0.0", authorized=True)["status"], "updated")
            self.assertEqual(skills.load("demo").body, "hello world")
            self.assertEqual(skills.load("demo").version, "1.0.2")

    def test_conflicts_do_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "skills" / "demo"
            p.mkdir(parents=True)
            file = p / "SKILL.md"
            file.write_text("---\nname: demo\n---\nhello world")
            skills = SkillStore(d)
            e = EvolutionStore(skills)
            original = file.read_text()
            snap = skills.load("demo")
            edits = [
                {"type": "replace", "old": "hello", "new": "a"},
                {"type": "replace", "old": "hello world", "new": "b"},
            ]
            self.assertEqual(
                e.apply("demo", edits, base_hash=snap.content_hash, authorized=True)["status"],
                "rejected",
            )
            self.assertEqual(file.read_text(), original)
            self.assertEqual(
                e.apply(
                    "demo",
                    [{"type": "replace", "old": "hello", "new": ""}],
                    base_hash="stale",
                    authorized=True,
                )["status"],
                "rejected",
            )
            self.assertEqual(file.read_text(), original)
            self.assertTrue(all(x["failure_kind"] == "failure_to_apply" for x in e.history("demo")))


class EvolutionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "skills" / "demo" / "SKILL.md"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            "---\nname: demo\nversion: 1.0.0\ndescription: useful\n---\nhello world"
        )
        self.skills = SkillStore(self.tmp.name)
        self.store = EvolutionStore(self.skills)

    def apply(self, edits, **kw):
        return self.store.apply(
            "demo", edits, base_hash=self.skills.load("demo").content_hash, authorized=True, **kw
        )

    def test_reject_invalid_edits(self):
        before = self.path.read_text()
        for edits in (
            [{"type": "replace", "old": "hello world", "new": ""}],
            [{"type": "replace", "old": "hello", "new": "hello"}],
            [{"type": "append", "text": "x"}] * 3,
            [{"type": "append", "text": "x" * 1001}],
            [{"type": "append", "text": "\n---\nname: evil"}],
        ):
            with self.subTest(edits=edits):
                self.assertEqual(self.apply(edits)["status"], "rejected")
                self.assertEqual(self.path.read_text(), before)

    def test_prepublication_failures(self):
        from unittest.mock import patch

        before = self.path.read_text()
        with patch("weyra.skill_evolution.atomic", side_effect=OSError("snapshot unavailable")):
            result = self.apply([{"type": "append", "text": "new"}])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.path.read_text(), before)
        with patch.object(self.store, "_record", side_effect=OSError("provenance unavailable")):
            result = self.apply([{"type": "append", "text": "new"}])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.path.read_text(), before)

    def test_postpublication_failure(self):
        from unittest.mock import patch
        from weyra.knowledge import atomic

        def failing(path, text):
            if Path(path).name == "1.0.1.md":
                raise OSError("auxiliary unavailable")
            return atomic(path, text)

        with patch("weyra.skill_evolution.atomic", side_effect=failing):
            result = self.apply([{"type": "append", "text": "new"}])
        self.assertEqual(result["status"], "updated")
        self.assertTrue(result["auxiliary_errors"])
        self.assertIn("new", self.path.read_text())

    def test_knowledge_dedup_and_corruption(self):
        for i in range(12):
            self.apply(
                [{"type": "append", "text": f"entry {i}!"}],
                knowledge_note="same" if i < 2 else str(i),
            )
        notes = self.store.knowledge("demo")
        self.assertEqual(len(notes), 10)
        self.assertEqual(
            len(notes), len({(item["event_id"], item["kind"], item["text"]) for item in notes})
        )
        (self.store._dir("demo") / "knowledge.json").write_text("{broken")
        self.assertEqual(self.store.knowledge("demo"), [])
        self.assertEqual(
            self.apply([{"type": "append", "text": "last"}], knowledge_note="safe")["status"],
            "updated",
        )

    def test_model_cannot_authorize_itself(self):
        import asyncio

        async def proposal(window):
            return {
                "name": "demo",
                "edits": [{"type": "append", "text": "new"}],
                "base_hash": self.skills.load("demo").content_hash,
                "authorized": True,
                "permission_mode": "bypassPermissions",
            }

        result = asyncio.run(self.store.maintain([], proposal))
        self.assertEqual(result["status"], "rejected")

    def test_hash_recheck(self):
        from unittest.mock import patch

        record = self.store._record

        def concurrent(name, event):
            record(name, event)
            if event["status"] == "prepared":
                self.path.write_text("external")

        with patch.object(self.store, "_record", side_effect=concurrent):
            result = self.apply([{"type": "append", "text": "new"}])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.path.read_text(), "external")

    def test_metadata_preserved_and_bounded(self):
        self.path.write_text(
            "---\nname: demo\nversion: 1.0.0\ncustom: preserve\ncontext: fork\n---\nhello world"
        )
        before = self.path.read_text()
        self.assertEqual(
            self.apply([{"type": "append", "text": "new"}], description="x" * 501)["status"],
            "rejected",
        )
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(
            self.apply([{"type": "append", "text": "new"}], description='a "quote"')["status"],
            "updated",
        )
        snapshot = self.skills.load("demo")
        self.assertEqual(snapshot.metadata["custom"], "preserve")
        self.assertEqual(snapshot.context, "fork")
        self.assertEqual(snapshot.description, 'a "quote"')

    def test_same_named_projects_have_distinct_history(self):
        other_root = Path(self.tmp.name) / "other"
        other_path = other_root / "skills" / "demo" / "SKILL.md"
        other_path.parent.mkdir(parents=True)
        other_path.write_text(self.path.read_text())
        other = EvolutionStore(SkillStore(other_root))
        self.apply([{"type": "append", "text": "first"}])
        self.assertEqual(other.history("demo"), [])
        self.assertNotEqual(self.store.root, other.root)

    def test_maintenance_persists_evidence_before_model_and_pins_hash(self):
        import asyncio
        import json

        snapshot = self.skills.load("demo")

        async def propose(window):
            records = [
                json.loads(line)
                for line in (self.store.root / "evidence.jsonl").read_text().splitlines()
            ]
            self.assertEqual(records[-1]["event_id"], "program-event")
            self.assertEqual(records[-1]["evidence"]["user"], "raw user")
            return {
                "action": "apply",
                "name": "demo",
                "base_hash": "model-forged",
                "event_id": "forged",
                "edits": [{"type": "append", "text": "durable method"}],
            }

        result = asyncio.run(
            self.store.maintain(
                {"user": "raw user"},
                propose,
                authorized=True,
                event_id="program-event",
                evidence={"user": "raw user"},
                target_snapshot=snapshot,
            )
        )
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["event_id"], "program-event")
        self.assertEqual(result["base_hash"], snapshot.content_hash)

    def test_maintenance_denied_has_no_model_or_evidence_write(self):
        import asyncio
        from unittest.mock import AsyncMock

        model = AsyncMock(return_value={"action": "discard"})
        result = asyncio.run(self.store.maintain({"user": "raw"}, model))
        self.assertEqual(result["status"], "rejected")
        model.assert_not_awaited()
        self.assertFalse((self.store.root / "evidence.jsonl").exists())

    def test_maintenance_rejects_target_switch(self):
        import asyncio

        async def propose(window):
            return {
                "action": "apply",
                "name": "other",
                "edits": [{"type": "append", "text": "new"}],
            }

        result = asyncio.run(
            self.store.maintain(
                {}, propose, authorized=True, target_snapshot=self.skills.load("demo")
            )
        )
        self.assertEqual(result["status"], "rejected")

    def test_maintenance_can_discard_without_name(self):
        import asyncio

        async def propose(window):
            return {"action": "discard", "reason": "insufficient evidence"}

        result = asyncio.run(self.store.maintain({}, propose, authorized=True))
        self.assertEqual(result["status"], "discarded")
        self.assertTrue(result["event_id"])

    def test_maintenance_legacy_hash_is_captured_before_model(self):
        import asyncio

        async def propose(window):
            self.path.write_text(self.path.read_text() + "\nexternal change")
            return {
                "action": "apply",
                "name": "demo",
                "base_hash": self.skills.load("demo").content_hash,
                "edits": [{"type": "append", "text": "model change"}],
            }

        result = asyncio.run(self.store.maintain({}, propose, authorized=True))
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("model change", self.path.read_text())

    def test_maintenance_evidence_failure_prevents_model_call(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        model = AsyncMock(return_value={"action": "discard"})
        with patch("weyra.skill_evolution.os.fsync", side_effect=OSError("disk unavailable")):
            result = asyncio.run(self.store.maintain({}, model, authorized=True))
        self.assertEqual(result["status"], "rejected")
        model.assert_not_awaited()

    def test_structured_knowledge_and_legacy_migration(self):
        import json

        path = self.store._dir("demo") / "knowledge.json"
        path.write_text(json.dumps(["legacy observation", {"kind": "invalid", "text": "skip"}]))
        legacy = self.store.knowledge("demo")
        self.assertEqual(legacy[0]["kind"], "observation")
        self.assertEqual(legacy[0]["skill_version"], "unknown")
        result = self.apply(
            [{"type": "append", "text": "new behavior"}],
            event_id="typed",
            knowledge_note={"kind": "boundary", "text": "Preserve exact filenames"},
        )
        self.assertEqual(result["status"], "updated")
        note = self.store.knowledge("demo")[-1]
        self.assertEqual(set(note), {"event_id", "kind", "text", "time", "skill_version"})
        self.assertEqual(note["event_id"], "typed")
        self.assertEqual(note["skill_version"], "1.0.1")
        self.assertIn("Preserve exact filenames", self.store.knowledge_prompt("demo"))
        self.assertLessEqual(len(self.store.knowledge_prompt("demo")), 2000)

    def test_static_rejection_records_application_failure_note(self):
        result = self.apply([{"type": "append", "text": "hello world"}])
        self.assertEqual(result["status"], "rejected")
        note = self.store.knowledge("demo")[-1]
        self.assertEqual(note["kind"], "rejected_update")
        self.assertIn("failure_to_apply", note["text"])
        self.assertIsNone(result["task_quality"])

    def test_invalid_structured_note_does_not_publish(self):
        before = self.path.read_text()
        result = self.apply(
            [{"type": "append", "text": "new"}],
            knowledge_note={"kind": "preference", "text": "not allowed"},
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.path.read_text(), before)

    def test_same_name_relocated_skill_has_separate_history(self):
        before = self.store._dir("demo")
        self.apply([{"type": "append", "text": "first"}])
        moved = self.path.parent.parent / "moved"
        self.path.parent.rename(moved)
        after = self.store._dir("demo")
        self.assertNotEqual(before, after)
        self.assertEqual(self.store.history("demo"), [])


def test_maintenance_protocol_examples_match_the_actual_patch_contract(tmp_path):
    import asyncio
    import json
    from weyra.online_skill_evolution import MAINTENANCE_INSTRUCTION
    examples = [json.loads(line) for line in MAINTENANCE_INSTRUCTION.splitlines() if line.startswith('{')]
    assert {item['action'] for item in examples} == {'create', 'apply', 'discard'}
    skills = SkillStore(tmp_path)
    store = EvolutionStore(skills)
    store.create('target-skill', 'exact old substring', permission_mode='acceptEdits')
    target = skills.load('target-skill')

    async def run():
        for proposal in examples:
            async def model(_):
                return proposal
            result = await store.maintain({'user': 'durable correction'}, model,
                                         permission_mode='acceptEdits',
                                         target_snapshot=target if proposal['action'] == 'apply' else None)
            assert result['status'] in {'updated', 'discarded'}
    asyncio.run(run())


def test_rejected_proposal_is_saved_before_validation(tmp_path):
    import asyncio
    import json
    from weyra.session import SessionStore
    session = SessionStore(project=tmp_path)
    skills = SkillStore(tmp_path)
    store = EvolutionStore(skills, session=session)
    proposal = {'action': 'apply', 'name': 'demo', 'edits': [{'op': 'unsupported'}]}
    store.create('demo', 'original', permission_mode='acceptEdits')
    original = skills.load('demo')

    async def model(_):
        return proposal
    result = asyncio.run(store.maintain({'user': 'permanent correction'}, model,
                                       permission_mode='acceptEdits', target_snapshot=original))
    assert result['status'] == 'rejected'
    event = next(r['data'] for r in session.records if r['kind'] == 'skill_proposal')
    artifact = json.loads(Path(event['proposal_ref']).read_text())
    assert artifact['proposal'] == proposal
    assert artifact['event_id'] == result['event_id']
    assert skills.load('demo').content_hash == original.content_hash
    session.close()


def test_proposal_checkpoint_failure_prevents_publication(tmp_path, monkeypatch):
    import asyncio
    import pytest
    from weyra.session import SessionStore
    session = SessionStore(project=tmp_path)
    skills = SkillStore(tmp_path)
    store = EvolutionStore(skills, session=session)
    store.create('demo', 'original', permission_mode='acceptEdits')
    original = skills.load('demo')
    append = session.append

    def fail(kind, *args, **kwargs):
        if kind == 'skill_proposal':
            raise OSError('disk full')
        return append(kind, *args, **kwargs)
    monkeypatch.setattr(session, 'append', fail)

    async def model(_):
        return {'action': 'apply', 'name': 'demo', 'edits': [{'op': 'replace', 'old': 'original', 'new': 'changed'}]}
    with pytest.raises(OSError, match='disk full'):
        asyncio.run(store.maintain({'user': 'durable correction'}, model,
                                  permission_mode='acceptEdits', target_snapshot=original))
    assert skills.load('demo').content_hash == original.content_hash
    session.close()


def test_truncated_maintenance_json_cannot_publish_a_skill(tmp_path):
    import asyncio
    import json
    from weyra.agent import Agent
    from weyra.contracts import ModelResponse
    from weyra.demos import scripted_runtime

    async def run():
        runtime = scripted_runtime([], maintenance=[ModelResponse(json.dumps({'action': 'create', 'name': 'unsafe', 'body': 'valid JSON but truncated generation', 'description': 'test'}), stop_reason='length')])
        agent = Agent(tmp_path, runtime, permission_mode='acceptEdits', enable_memory=False)
        agent._schedule_evolution({'user': 'Always retain this reusable method', 'feedback': 'Keep it permanently'})
        await asyncio.gather(*list(agent._background))
        assert 'unsafe' not in agent.skills.capture_catalog()
        assert any(r['kind'] == 'evolution_result' and r['data']['status'] == 'failed' for r in agent.session.records)
        await agent.close()
    asyncio.run(run())

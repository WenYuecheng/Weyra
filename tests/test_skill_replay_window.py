import asyncio

from weyra.contracts import ToolCall
from weyra.demos import scripted_runtime
from weyra.agent import Agent


def evidence(**overrides):
    return {"user": "Summarize the source", "assistant": "old reply",
            "feedback": "Always cite the supplied evidence",
            "replay": {"turn_id": "turn-1", "complete": True, "files": {},
                       "written_paths": [], "context": []}, **overrides}


def case(index, **extra):
    return {"id": f"case-{index}", "prompt": f"Independent task {index}",
            "files": {}, "output_mode": "text", "rubric": "Use supplied evidence", **extra}


def test_window_has_fixed_roles_and_does_not_grow_with_history():
    from weyra.skill_replay import freeze_window
    window = freeze_window(evidence(), [case(i) for i in range(100)])
    assert len(window.development) == 1
    assert len(window.validation) == 2
    assert len(window.cases) <= 4
    assert window.development[0]["prompt"] == "Summarize the source"
    assert all(c["split"] == "validation" for c in window.validation)


def test_window_deduplicates_original_and_same_interaction_from_validation():
    from weyra.skill_replay import freeze_window
    window = freeze_window(evidence(), [
        case(0, prompt="Summarize the source"), case(1, source_window_id="turn-1"),
        case(2, split="development"), case(3)])
    assert [c["id"] for c in window.validation] == ["case-3"]
    assert len(window.development) == 2


def test_window_is_frozen_before_proposal_and_changes_identity_with_rules():
    from weyra.skill_replay import freeze_window
    source = [case(0)]
    window = freeze_window(evidence(), source)
    version = window.version
    source[0]["rubric"] = "different"
    window.cases[0]["prompt"] = "mutated copy"
    assert window.version == version
    assert window.development[0]["prompt"] == "Summarize the source"
    assert freeze_window(evidence(), source).version != version


def test_no_independent_validation_is_explicitly_insufficient():
    from weyra.skill_replay import freeze_window
    assert freeze_window(evidence(), []).reason == "insufficient_independent_validation"
    assert freeze_window(evidence(replay=None), [case(0)]).reason == "missing_replay_snapshot"


def test_capture_preserves_first_file_preimage_and_absent_output(tmp_path):
    from weyra.skill_replay import ReplayCapture
    capture = ReplayCapture("turn-1", [])
    capture.file("input.txt", b"before")
    capture.file("input.txt", b"after", write=True)
    capture.file("new.txt", None, write=True)
    snapshot = capture.snapshot()
    assert snapshot["files"] == {"input.txt": "before", "new.txt": None}
    assert snapshot["written_paths"] == ["input.txt", "new.txt"]
    window = __import__('weyra.skill_replay', fromlist=['freeze_window']).freeze_window(
        evidence(replay=snapshot), [case(0)])
    development = window.development[0]
    assert development["files"] == {"input.txt": "before"}
    assert development["hard_constraints"]["writable_input_paths"] == ["input.txt"]


def test_capture_limits_data_and_marks_unsupported_tools():
    from weyra.skill_replay import ReplayCapture
    capture = ReplayCapture("turn-1", [])
    capture.file("binary.bin", b"\xff")
    assert not capture.snapshot()["complete"]
    other = ReplayCapture("turn-2", [])
    other.tool("shell")
    assert other.snapshot()["reason"] == "unsupported_tool:shell"
    large = ReplayCapture("turn-3", [])
    large.file("large.txt", b"x" * 70000)
    assert not large.snapshot()["complete"]


def test_search_and_directory_views_cannot_claim_complete_replay():
    from weyra.skill_replay import ReplayCapture
    for name in ("list_files", "grep_search"):
        capture = ReplayCapture("turn", [])
        capture.tool(name)
        assert capture.snapshot()["reason"] == "unsupported_tool:" + name


def test_effective_instructions_are_frozen_and_part_of_window_identity():
    from weyra.skill_replay import ReplayCapture, freeze_window
    capture = ReplayCapture("turn", [], system_instructions="Always answer in Chinese")
    snapshot = capture.snapshot()
    window = freeze_window(evidence(replay=snapshot), [case(0)])
    assert window.development[0]["system_instructions"] == "Always answer in Chinese"
    snapshot["system_instructions"] = "Always answer in English"
    assert freeze_window(evidence(replay=snapshot), [case(0)]).version != window.version


def test_lifecycle_retains_project_instructions(tmp_path):
    from weyra.contracts import ModelResponse
    async def run():
        (tmp_path / "WEYRA.md").write_text("Use Chinese in every answer")
        agent = Agent(tmp_path, scripted_runtime([ModelResponse("done")]), enable_memory=False)
        agent.enable_evolution = True
        await agent.run_once("hi")
        assert "Use Chinese in every answer" in agent._pending_window["replay"]["system_instructions"]
        await agent.close()
    asyncio.run(run())


def test_real_file_tools_capture_inputs_before_modification(tmp_path):
    from weyra.skill_replay import ReplayCapture
    from weyra.runtime_state import RunContext
    async def run():
        agent = Agent(tmp_path, scripted_runtime([]), enable_memory=False, enable_evolution=False)
        ctx = RunContext(tmp_path, agent.session, agent.budget, "bypassPermissions")
        capture = ReplayCapture(ctx.id, [])
        ctx.services["replay_capture"] = capture
        (tmp_path / "in.txt").write_text("before")
        await agent.tools.execute(ToolCall("r", "read_file", {"path": "in.txt"}), ctx)
        await agent.tools.execute(ToolCall("w", "write_file", {"path": "in.txt", "content": "after"}), ctx)
        assert capture.snapshot()["files"]["in.txt"] == "before"
        assert capture.snapshot()["written_paths"] == ["in.txt"]
        await agent.close()
    asyncio.run(run())


def test_replay_context_does_not_include_task_answer_or_skill_tool_messages():
    from weyra.skill_replay import ReplayCapture
    from weyra.contracts import Message
    messages = [Message("user", "Previous question"), Message("assistant", "Previous context"),
                Message("tool", "stale skill body", source="skill")]
    snapshot = ReplayCapture("turn-1", messages).snapshot()
    assert len(snapshot["context"]) == 2
    assert all(m["role"] in {"user", "assistant"} for m in snapshot["context"])


def test_lifecycle_persists_this_turn_snapshot(tmp_path):
    from weyra.contracts import ModelResponse
    runtime = scripted_runtime([
        ModelResponse(tool_calls=(ToolCall("r", "read_file", {"path": "in.txt"}),)),
        ModelResponse(tool_calls=(ToolCall("w", "write_file", {"path": "in.txt", "content": "after"}),)),
        ModelResponse("done")])
    async def run():
        (tmp_path / "in.txt").write_text("before")
        agent = Agent(tmp_path, runtime, permission_mode="bypassPermissions", enable_memory=False)
        agent.enable_evolution = True
        await agent.run_once("Change in.txt")
        replay = agent._pending_window["replay"]
        assert replay["complete"]
        assert replay["files"]["in.txt"] == "before"
        assert agent.session.latest("pending_window")["replay"] == replay
        await agent.close()
    asyncio.run(run())


def test_replay_turn_does_not_reset_shared_maintenance_scope(tmp_path):
    from weyra.contracts import ModelResponse
    async def run():
        agent = Agent(tmp_path, scripted_runtime([ModelResponse("done")]), enable_memory=False)
        agent.budget.set_scope_limit("maintenance", 12345)
        agent._delegation_task_id = "maintenance"
        await agent.run_once("hi")
        assert agent.budget._scopes["maintenance"]["limit"] == 12345
        await agent.close()
    asyncio.run(run())

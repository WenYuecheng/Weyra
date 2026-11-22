"""Checks for the fresh repair experiment without live requests or Docker."""

import asyncio

from types import SimpleNamespace

import pytest

from benchmarks.polyglot_full import glm_campaign as campaign


from weyra.contracts import ModelInfo, ModelResponse, ToolCall

from weyra.core.model_runtime import ModelRuntime

from weyra.llm.providers import ProviderRegistry, ScriptedProvider


def test_contract_repair_keeps_original_interface_and_all_failure_review():
    task = SimpleNamespace(
        instruction="Implement the public interface.", editable=("answer.py",),
        files={"answer.py": "def required_interface(value):\n    pass\n"},
        solutions={"answer.py": "PRIVATE_REFERENCE"},
        verifier_files={"secret_test.py": "PRIVATE_TEST"},
    )
    prompt = campaign.fresh_repair_prompt(
        task, {"answer.py": b"def renamed(value): return 1"}, "Expected: 2, received: 1",
        include_contract=True, review="Audit all reported failures and affected invariants.",
    )
    assert "required_interface" in prompt
    assert "def renamed" in prompt
    assert "Expected: 2, received: 1" in prompt
    assert "Audit all reported failures" in prompt
    assert "PRIVATE_REFERENCE" not in prompt
    assert "PRIVATE_TEST" not in prompt


@pytest.mark.parametrize("arm", ["B", "R"])
def test_repair_context_and_artifact(tmp_path, monkeypatch, arm):
    class Workspace:
        def create(self, files):
            self.files = {name: value.encode() for name, value in files.items()}

        def inspect(self):
            return {}

        def worker(self, operation, args):
            if operation == "write_file":
                self.files[args["path"]] = args["content"].encode()
            return {"content": "tool output"}

        def export(self, paths):
            return {path: self.files[path] for path in paths}

        def close(self):
            pass

    path = "solution.py"
    responses = [
        ModelResponse(
            tool_calls=(ToolCall("first", "write_file", {"path": path, "content": "answer = 1"}),)
        ),
        ModelResponse("stale first turn commentary"),
        ModelResponse(
            tool_calls=(ToolCall("second", "write_file", {"path": path, "content": "answer = 2"}),)
        ),
        ModelResponse("saved second edit"),
    ]
    provider = ScriptedProvider(responses)
    provider.budget_denial = None
    registry = ProviderRegistry()
    registry.register("offline", provider)
    runtime = ModelRuntime(registry, {"main": ModelInfo("offline", "fake", 32768, 4096)})

    def verify(task, image, files, location, expected):
        assert expected == ["frozen_case"]
        passed = files[path] == b"answer = 2"
        return ("pass" if passed else "fail"), {"case_ids": expected}

    monkeypatch.setattr(campaign, "Sandbox", lambda image: Workspace())
    monkeypatch.setattr(campaign, "isolation", lambda inspect: {})
    monkeypatch.setattr(campaign, "runtime_for", lambda *args: (runtime, provider))
    monkeypatch.setattr(campaign, "verify_task", verify)
    monkeypatch.setattr(campaign, "feedback", lambda *args: "Tests failed.")
    task = SimpleNamespace(
        instruction="Set answer to 2.",
        files={path: "answer = 0"},
        editable=(path,),
        solutions={path: "private reference name"},
        verifier_files={"secret_test.py": "private test data"},
    )
    with campaign.Ledger(tmp_path / "usage.db", 48_000) as ledger:
        outcome = asyncio.run(
            campaign.solve(
                task,
                arm,
                campaign.BASE_SKILL,
                ["frozen_case"],
                "image",
                tmp_path / "attempt",
                ledger,
                None,
                f"python/toy/{arm}",
            )
        )
    assert outcome["status"] == "pass"
    assert [row["verdict"] for row in outcome["rounds"]] == ["fail", "pass"]
    assert (tmp_path / "attempt/2/implementation" / path).read_text() == "answer = 2"
    first, _, repair, _ = provider.requests
    assert first.system == repair.system
    assert first.tools == repair.tools
    repair_text = " ".join(message.content for message in repair.messages)
    assert "Tests failed." in repair_text
    assert "private reference name" not in repair_text
    assert "private test data" not in repair_text
    if arm == "R":
        assert "Set answer to 2." in repair_text
        assert "answer = 1" in repair_text
        assert "answer = 0" not in repair_text
        assert "stale first turn commentary" not in repair_text
    else:
        assert "stale first turn commentary" in repair_text


@pytest.mark.parametrize("stop_reason", ["length", "max_tokens"])
def test_baseline_executes_complete_tool_calls_at_output_limit(tmp_path, stop_reason):
    from weyra.coding.agent import CodingAgent
    from weyra.llm.providers import _validate_pairs

    saved = {}

    class Workspace:
        def worker(self, operation, args):
            assert operation == "write_file"
            saved[args["path"]] = args["content"]
            return {"content": "File saved"}

    provider = ScriptedProvider([
        ModelResponse(
            tool_calls=(ToolCall("save", "write_file", {
                "path": "answer.py", "content": "answer = 42",
            }),),
            stop_reason=stop_reason,
        ),
        ModelResponse("Saved."),
    ])
    registry = ProviderRegistry()
    registry.register("offline", provider)
    runtime = ModelRuntime(registry, {"main": ModelInfo("offline", "fake", 32768, 4096)})

    async def run():
        agent = CodingAgent(tmp_path, runtime, permission_mode="acceptEdits",
                            context_policy={"headroom_tokens": 4096, "summary_max_tokens": 2048},
                            enable_memory=False, enable_evolution=False)
        try:
            task = SimpleNamespace(files={"answer.py": "answer = 0"}, editable=("answer.py",))
            campaign.register_tools(agent, Workspace(), task)
            await campaign.minimal_turn(agent, runtime, "Save answer = 42.", [], steps=2)
        finally:
            await agent.close()

    asyncio.run(run())

    assert saved == {"answer.py": "answer = 42"}
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1].role == "tool"
    _validate_pairs(provider.requests[1])


@pytest.mark.parametrize("arm", ["A", "R"])
@pytest.mark.parametrize("feedback_mode", ["labels", "test_output"])
@pytest.mark.parametrize("repair_context", ["fresh", "contract", "history"])
def test_whole_submission_saves_and_repairs_in_two_requests(tmp_path, monkeypatch, arm, feedback_mode, repair_context):
    from dataclasses import replace
    from benchmarks.polyglot_full import glm_full, glm_whole

    with monkeypatch.context() as patch:
        for target, names in ((glm_full, ("PROFILE", "TASK_TOKENS", "TASK_CALLS", "WEYRA_SKILL")),
                              (campaign, ("SYSTEM", "TOOL_NAMES"))):
            for name in names:
                patch.setattr(target, name, getattr(target, name))
        glm_whole.configure()
        glm_full.PROFILE = replace(glm_full.PROFILE, feedback_mode=feedback_mode,
                                  repair_context=repair_context, repair_review="Audit every failure.")
        responses = [
            ModelResponse(tool_calls=(ToolCall(str(i), "write_file", {
                "path": "answer.py", "content": f"answer = {i}",
            }),)) for i in (1, 2)
        ]
        provider = ScriptedProvider(responses)
        provider.budget_denial = None
        registry = ProviderRegistry()
        registry.register("offline", provider)
        runtime = ModelRuntime(registry, {"main": ModelInfo("offline", "fake", 32768, 8192)})
        saved = {"answer.py": b"answer = 0"}

        class Workspace:
            def create(self, files):
                pass
            def inspect(self):
                return {}
            def worker(self, operation, args):
                if operation == "write_file":
                    saved[args["path"]] = args["content"].encode()
                return {"content": "saved"}
            def export(self, paths):
                return dict(saved)
            def close(self):
                pass

        def verify(task, image, files, folder, expected):
            return ("pass" if files["answer.py"] == b"answer = 2" else "fail"), {"case_ids": expected}

        patch.setattr(campaign, "Sandbox", lambda _: Workspace())
        patch.setattr(campaign, "isolation", lambda _: {})
        patch.setattr(campaign, "runtime_for", lambda *args: (runtime, provider))
        patch.setattr(campaign, "verify_task", verify)
        patch.setattr(campaign, "feedback", lambda *args: "Tests failed.")
        patch.setattr(campaign, "test_output_feedback", lambda *args: "Expected: 2, received: 1")
        task = SimpleNamespace(instruction="Set answer to 2.", editable=("answer.py",),
                               files={"answer.py": "answer = 0"})
        with campaign.Ledger(tmp_path / "usage.db", 65536) as ledger:
            result = asyncio.run(campaign.solve(task, arm, campaign.BASE_SKILL, ["case"],
                                "image", tmp_path / "attempt", ledger, None, arm,
                                settings=glm_full.PROFILE))
        assert result["status"] == "pass"
        assert [r["verdict"] for r in result["rounds"]] == ["fail", "pass"]
        assert len(provider.requests) == 2
        assert all(len(r.tools) == 1 and r.tools[0]["name"] == "write_file"
                   for r in provider.requests)
        expected_feedback = "Expected: 2, received: 1" if feedback_mode == "test_output" else "Tests failed."
        assert expected_feedback in " ".join(m.content for m in provider.requests[1].messages)
        repair_text = " ".join(m.content for m in provider.requests[1].messages)
        assert ("Audit every failure." in repair_text) is (arm == "R")
        if arm == "R" and repair_context == "contract":
            assert "Original editable starter" in repair_text
            assert "answer = 0" in repair_text

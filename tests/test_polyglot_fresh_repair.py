"""Checks for the fresh repair experiment without live requests or Docker."""

import asyncio

from types import SimpleNamespace

import pytest

from benchmarks.polyglot_full import glm_campaign as campaign


from weyra.contracts import ModelInfo, ModelResponse, ToolCall

from weyra.core.model_runtime import ModelRuntime

from weyra.llm.providers import ProviderRegistry, ScriptedProvider


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

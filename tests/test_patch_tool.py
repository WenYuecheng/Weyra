import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from weyra.contracts import ToolCall
from weyra.tool_runtime import ToolRuntime
from weyra.tools import install_builtin_tools


class Session:
    def __init__(self):
        self.events = []

    def append(self, kind, data, checkpoint=False):
        self.events.append((kind, data))


def setup(tmp_path, *, enabled=True, mode="acceptEdits", confirm=None, deny_rules=()):
    runtime = ToolRuntime(
        tmp_path, Session(), permission_mode=mode, confirm=confirm, deny_rules=deny_rules
    )
    runtime.apply_patch_enabled = enabled
    install_builtin_tools(runtime, None)
    ctx = SimpleNamespace(
        id="turn",
        permission_mode=mode,
        services={},
        read_versions={},
        check_cancelled=lambda: None,
        emit=None,
    )
    return runtime, ctx


def version(ctx, path, content):
    path.write_bytes(content)
    ctx.read_versions[str(path)] = hashlib.sha256(content).hexdigest()


async def execute(runtime, ctx, body):
    assert "apply_patch" in runtime.specs, "optional patch tool is not installed"
    return await runtime.execute(
        ToolCall("patch", "apply_patch", {"input": "*** Begin Patch\n" + body + "*** End Patch\n"}),
        ctx,
    )


def test_opt_in_registration_does_not_change_default_tools(tmp_path):
    runtime = ToolRuntime(tmp_path, Session())
    install_builtin_tools(runtime, None)
    assert "apply_patch" not in runtime.specs
    runtime, _ = setup(tmp_path)
    assert "apply_patch" in runtime.specs
    assert runtime.specs["apply_patch"].permission == "write"
    assert runtime.specs["apply_patch"].execution_mode == "exclusive"


def test_multi_file_and_repeated_paths_simulate_in_order(tmp_path):
    async def run():
        runtime, ctx = setup(tmp_path)
        version(ctx, tmp_path / "old", b"old\r\n")
        version(ctx, tmp_path / "remove", b"delete\n")
        result = await execute(
            runtime,
            ctx,
            "*** Add File: 新.txt\n+你好\n*** Update File: 新.txt\n@@\n-你好\n+世界\n"
            "*** Update File: old\n*** Move to: moved\n@@\n-old\n+new\n"
            "*** Delete File: remove\n",
        )
        assert result.status == "success"
        assert result.effect == "completed"
        assert (tmp_path / "新.txt").read_text() == "世界\n"
        assert (tmp_path / "moved").read_bytes() == b"new\r\n"
        assert not (tmp_path / "old").exists() and not (tmp_path / "remove").exists()
        assert len(result.structured["completed"]) == 4
        assert result.structured["failed"] == result.structured["unknown"] == []
        assert "新.txt" in result.structured["_display"]["diff"]
        assert ctx.read_versions[str(tmp_path / "moved")] == hashlib.sha256(b"new\r\n").hexdigest()
        assert str(tmp_path / "old") not in ctx.read_versions

    asyncio.run(run())


@pytest.mark.parametrize(
    "tail",
    [
        "*** Update File: existing\n@@\n-missing\n+new\n",
        "*** Delete File: unread\n",
        "*** Add File: ../escape\n+unsafe\n",
        "*** Add File: .weyra/secret\n+unsafe\n",
        "*** Add File: private/secret\n+unsafe\n",
        "*** Update File: existing\n*** Move to: private/moved\n",
        "*** Add File: existing\n+overwrite\n",
        "*** Update File: existing\n*** Move to: unread\n",
    ],
)
def test_whole_patch_preflight_rejects_without_target_writes(tmp_path, tail):
    async def run():
        runtime, ctx = setup(tmp_path)
        version(ctx, tmp_path / "existing", b"old\n")
        (tmp_path / "unread").write_text("unread")
        runtime.private_paths.add(tmp_path / "private")
        result = await execute(runtime, ctx, "*** Add File: first\n+first\n" + tail)
        assert result.status in {"error", "denied"}
        assert result.effect == "none"
        assert not (tmp_path / "first").exists()
        assert (tmp_path / "existing").read_bytes() == b"old\n"
        assert (tmp_path / "unread").read_text() == "unread"
        assert len(result.structured["failed"]) == 1
        assert len(result.structured["skipped"]) == 1

    asyncio.run(run())


def test_every_endpoint_uses_runtime_policy_and_no_writes_on_denial(tmp_path):
    async def run():
        seen = []

        def deny(spec, args):
            seen.append((spec.name, args.get("path")))
            return spec.name == "write_file" and args.get("path") == "denied"

        runtime, ctx = setup(tmp_path, deny_rules=(deny,))
        version(ctx, tmp_path / "old", b"old\n")
        result = await execute(
            runtime, ctx, "*** Add File: first\n+first\n*** Update File: old\n*** Move to: denied\n"
        )
        assert result.status == "denied" and result.effect == "none"
        assert not (tmp_path / "first").exists()
        assert ("write_file", "old") in seen and ("write_file", "denied") in seen

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["dontAsk", "plan"])
def test_outer_permission_gate_denies_patch(tmp_path, mode):
    async def run():
        runtime, ctx = setup(tmp_path, mode=mode)
        result = await execute(runtime, ctx, "*** Add File: first\n+first\n")
        assert result.status == "denied"
        assert not (tmp_path / "first").exists()

    asyncio.run(run())


def test_preflight_approvals_recheck_all_versions_before_first_write(tmp_path):
    async def run():
        def confirm(spec, args, ctx):
            if args.get("path") == "second":
                (tmp_path / "old").write_text("external\n")
            return True

        runtime, ctx = setup(tmp_path, mode="default", confirm=confirm)
        version(ctx, tmp_path / "old", b"old\n")
        result = await execute(
            runtime, ctx, "*** Update File: old\n@@\n-old\n+new\n*** Add File: second\n+second\n"
        )
        assert result.status == "error" and result.effect == "none"
        assert (tmp_path / "old").read_text() == "external\n"
        assert not (tmp_path / "second").exists()

    asyncio.run(run())


def test_mid_commit_failure_stops_with_completed_unknown_and_skipped(tmp_path, monkeypatch):
    async def run():
        runtime, ctx = setup(tmp_path)
        original = Path.write_bytes

        def fail(path, content):
            if path == tmp_path / "second":
                raise OSError("disk full")
            return original(path, content)

        monkeypatch.setattr(Path, "write_bytes", fail)
        result = await execute(
            runtime,
            ctx,
            "*** Add File: first\n+first\n"
            "*** Add File: second\n+second\n*** Add File: third\n+third\n",
        )
        assert result.status == "error" and result.effect == "unknown"
        assert [op["path"] for op in result.structured["completed"]] == ["first"]
        assert [op["path"] for op in result.structured["unknown"]] == ["second"]
        assert [op["path"] for op in result.structured["skipped"]] == ["third"]
        assert (tmp_path / "first").exists() and not (tmp_path / "third").exists()

    asyncio.run(run())


def test_partial_move_reports_destination_completed_and_source_unknown(tmp_path, monkeypatch):
    async def run():
        runtime, ctx = setup(tmp_path)
        version(ctx, tmp_path / "source", b"old\n")
        original = Path.unlink

        def fail(path, *args, **kwargs):
            if path == tmp_path / "source":
                raise OSError("cannot unlink")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail)
        result = await execute(
            runtime, ctx, "*** Update File: source\n*** Move to: dest\n*** Add File: next\n+next\n"
        )
        assert result.status == "error" and result.effect == "unknown"
        operation = result.structured["unknown"][0]
        assert [step["status"] for step in operation["steps"]] == ["completed", "unknown"]
        assert (tmp_path / "dest").read_bytes() == b"old\n"
        assert (tmp_path / "source").read_bytes() == b"old\n"
        assert not (tmp_path / "next").exists()

    asyncio.run(run())


def test_commit_rechecks_versions_and_permissions_between_operations(tmp_path, monkeypatch):
    async def run(mode_change):
        runtime, ctx = setup(tmp_path)
        version(ctx, tmp_path / "second", b"old\n")
        original = Path.write_bytes

        def change(path, content):
            value = original(path, content)
            if path == tmp_path / "first":
                if mode_change:
                    ctx.permission_mode = "dontAsk"
                else:
                    original(tmp_path / "second", b"external\n")
            return value

        with monkeypatch.context() as patch:
            patch.setattr(Path, "write_bytes", change)
            result = await execute(
                runtime,
                ctx,
                "*** Add File: first\n+first\n"
                "*** Update File: second\n@@\n-old\n+new\n"
                "*** Add File: third\n+third\n",
            )
        assert len(result.structured["completed"]) == 1
        assert len(result.structured["failed"]) == 1
        assert len(result.structured["skipped"]) == 1
        assert not (tmp_path / "third").exists()
        assert (tmp_path / "second").read_bytes() == (b"old\n" if mode_change else b"external\n")

    asyncio.run(run(False))
    (tmp_path / "first").unlink()
    asyncio.run(run(True))


def test_cancellation_between_operations_preserves_completed_metadata(tmp_path, monkeypatch):
    async def run():
        runtime, ctx = setup(tmp_path)
        cancelled = False

        def check():
            if cancelled:
                raise asyncio.CancelledError

        ctx.check_cancelled = check
        original = Path.write_bytes

        def change(path, content):
            nonlocal cancelled
            value = original(path, content)
            if path == tmp_path / "first":
                cancelled = True
            return value

        monkeypatch.setattr(Path, "write_bytes", change)
        result = await execute(
            runtime, ctx, "*** Add File: first\n+first\n*** Add File: second\n+second\n"
        )
        assert result.status == "cancelled" and result.effect == "completed"
        assert len(result.structured["completed"]) == 1
        assert len(result.structured["skipped"]) == 1
        assert not (tmp_path / "second").exists()

    asyncio.run(run())


def test_move_into_source_child_rejected_before_any_writes(tmp_path):
    async def run():
        runtime, ctx = setup(tmp_path)
        version(ctx, tmp_path / "source", b"old\n")
        result = await execute(
            runtime,
            ctx,
            "*** Add File: first\n+first\n*** Update File: source\n*** Move to: source/child\n",
        )
        assert result.status == "error" and result.effect == "none"
        assert not (tmp_path / "first").exists()
        assert (tmp_path / "source").read_bytes() == b"old\n"

    asyncio.run(run())


def test_patch_specific_policy_revocation_during_endpoint_approval(tmp_path):
    async def run():
        def confirm(spec, args, ctx):
            if spec.name == "write_file":
                runtime.deny_rules = (lambda spec, args: spec.name == "apply_patch",)
            return True

        runtime, ctx = setup(tmp_path, mode="default", confirm=confirm)
        result = await execute(runtime, ctx, "*** Add File: first\n+first\n")
        assert result.status == "denied" and result.effect == "none"
        assert not (tmp_path / "first").exists()

    asyncio.run(run())


def test_symlink_swap_during_approval_rejects_all_targets(tmp_path):
    async def run():
        def confirm(spec, args, ctx):
            if args.get("path") == "link/file":
                (tmp_path / "link").unlink()
                (tmp_path / "link").symlink_to(tmp_path / "other", target_is_directory=True)
            return True

        runtime, ctx = setup(tmp_path, mode="default", confirm=confirm)
        (tmp_path / "safe").mkdir()
        (tmp_path / "other").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "safe", target_is_directory=True)
        result = await execute(
            runtime, ctx, "*** Add File: first\n+first\n*** Add File: link/file\n+content\n"
        )
        assert result.status == "denied" and result.effect == "none"
        assert not (tmp_path / "first").exists()
        assert not (tmp_path / "safe/file").exists()
        assert not (tmp_path / "other/file").exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        "*** Add File: parent\n+file\n*** Add File: parent/child\n+child\n",
        "*** Add File: parent/child\n+child\n*** Add File: parent\n+file\n",
    ],
)
def test_simulated_file_directory_collisions_reject_zero_writes(tmp_path, body):
    async def run():
        runtime, ctx = setup(tmp_path)
        result = await execute(runtime, ctx, body)
        assert result.status == "error" and result.effect == "none"
        assert not (tmp_path / "parent").exists()

    asyncio.run(run())


def test_task_cancellation_yield_preserves_persisted_partial_result(tmp_path, monkeypatch):
    async def run():
        runtime, ctx = setup(tmp_path)
        original = Path.write_bytes
        task = None

        def change(path, content):
            value = original(path, content)
            if path == tmp_path / "first":
                task.cancel()
            return value

        monkeypatch.setattr(Path, "write_bytes", change)
        task = asyncio.create_task(
            execute(runtime, ctx, "*** Add File: first\n+first\n*** Add File: second\n+second\n")
        )
        with pytest.raises(asyncio.CancelledError):
            await task
        result = [data for kind, data in runtime.session.events if kind == "tool_result"][-1]
        assert result["status"] == "cancelled"
        assert result["effect"] == "completed"
        assert len(result["structured"]["completed"]) == 1
        assert len(result["structured"]["skipped"]) == 1
        assert not (tmp_path / "second").exists()

    asyncio.run(run())


def test_empty_and_noop_changes_allow_absent_display_diff(tmp_path):
    async def run():
        runtime, ctx = setup(tmp_path)
        version(ctx, tmp_path / "same", b"same\n")
        result = await execute(
            runtime, ctx, "*** Add File: empty\n*** Update File: same\n@@\n-same\n+same\n"
        )
        assert result.status == "success"
        assert len(result.structured["completed"]) == 2
        assert result.structured["_display"]["diff"] == ""
        assert (tmp_path / "empty").read_bytes() == b""

    asyncio.run(run())


def test_fixed_task_patch_and_edit_modes_match_and_failed_patch_recovers(tmp_path):
    """A deterministic transport comparison, not a model/tool-success benchmark."""

    async def run():
        counts = {}
        for enabled in (False, True):
            project = tmp_path / str(enabled)
            project.mkdir()
            runtime, ctx = setup(project, enabled=enabled)
            for name in ("a", "b"):
                version(ctx, project / name, b"old\n")
            if enabled:
                rejected = await execute(
                    runtime,
                    ctx,
                    "*** Update File: a\n@@\n-old\n+new\n*** Update File: b\n@@\n-wrong\n+new\n",
                )
                assert rejected.status == "error" and rejected.effect == "none"
                assert (project / "a").read_bytes() == b"old\n"
                result = await execute(
                    runtime,
                    ctx,
                    "*** Update File: a\n@@\n-old\n+new\n*** Update File: b\n@@\n-old\n+new\n",
                )
                assert result.status == "success"
            else:
                for name in ("a", "b"):
                    result = await runtime.execute(
                        ToolCall(
                            name, "edit_file", {"path": name, "old_text": "old", "new_text": "new"}
                        ),
                        ctx,
                    )
                    assert result.status == "success"
            counts[enabled] = sum(
                kind == "tool_result" and data["status"] == "success"
                for kind, data in runtime.session.events
            )
            assert [(project / name).read_bytes() for name in ("a", "b")] == [b"new\n", b"new\n"]
        assert counts == {False: 2, True: 1}

    asyncio.run(run())

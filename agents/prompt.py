"""Checkpoint prompt pinned to DeepSeek Harness d743267388641bc76f17c45ce8b4c231aed1d32c."""

SUMMARY_OPEN_TAG = "<compacted-summary>"

SUMMARY_CLOSE_TAG = "</compacted-summary>"

COMPACTION_INSTRUCTION = 'You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.\n\nOutput EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.\n\n## Primary Request and Intent\n- [the user\'s original and evolving goals; quote verbatim where the exact wording matters]\n\n## Key Technical Concepts\n- [technologies, frameworks, patterns, and conventions in play]\n\n## Files and Code\n- [exact path: why it matters, key changes or snippets]\n\n## Errors and Fixes\n- [error: how it was resolved, plus any related user feedback]\n\n## Pending Jobs\n- [explicitly requested work not yet completed]\n\n## Current Work\n- [precisely what was in progress at this checkpoint]\n\n## Next Step\n- [the single next action, directly in line with the most recent request, or "(none)"]\n\n## Critical Context\n- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]\n\nRules:\n- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.\n- Capture user feedback and explicit instructions faithfully, especially corrections.\n- Do NOT mention this summarization request or that the context was compacted.\n- Output only the checkpoint text: do not call any tool or take any other action.\n- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure.'

CHECKPOINT_PREAMBLE = "This is an automatically generated checkpoint condensing an earlier span of the conversation to free up context. Treat the captured context as established background and build on it without restating it. Continue the task directly from the messages that follow, without acknowledging this checkpoint."


async def environment_snapshot(project):
    """Append-only current facts; never folded back into the stable system prefix."""
    import asyncio
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    result = {
        "project": str(Path(project).resolve()),
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "status",
        "--short",
        "--branch",
        "--",
        ".",
    ]
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=project,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}},
        )
        output, _ = await asyncio.wait_for(process.communicate(), 2)
        if process.returncode == 0:
            result["git_status"] = output.decode("utf-8", "replace")[:8192]
    except (FileNotFoundError, asyncio.TimeoutError):
        result["git_status"] = "unavailable"
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
    return result

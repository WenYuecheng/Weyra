# Runtime update implementation plan

Goal: apply the reviewed P0 architecture changes while preserving current Python layers.

Architecture: candidate model admission uses a detached context policy, compresses through the current runtime, and commits the new route only after capacity checks. Step tool admission retains registration identities; permission checks still run at execution time. Existing worktree changes are retained.

- [x] Add failing tests for failed/successful destination admission and registration replacement after declaration.
- [x] Implement admission and bind the step tool set across request retries and execution.
- [x] Run focused and full regression tests plus lint and package checks.
- [x] Update architecture/API/configuration, project resume and evidence ledger with actual implementation scope and results.

Deferred: general projection registry, persistent goal policy changes, patch tool, PTC and desktop capabilities.

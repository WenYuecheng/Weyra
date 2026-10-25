"""DeepSeek-Harness 八段式检查点结构化压缩模板 (Pinned DSH Compaction Templates)。

架构定位与设计哲学:
    本模块沉淀自 DeepSeek-Harness 核心仓锁定的工业级长文本压缩提示词模板（Pinned Commit `d743267`）。
    与具体项目的事实、业务逻辑完全解耦，独立维护在核心内核层。
    在大模型 Agent 长程任务（Long-horizon tasks）中，上下文膨胀是导致延迟激增、首字缓慢乃至
    物理窗口爆满的主要根因。本模板通过形式化约束，迫使压缩模型严格按照八大正交维度提炼核心检查点，
    确保在丢弃海量过程性工具输出的同时，关键决策、技术事实、已修改文件与下一步目标 100% 无损传递。

八大核心结构化分段体系 (Eight-Section Checkpoint Architecture):
    1. Primary Request and Intent: 用户原始诉求与演进目标，关键指令原样逐字摘录；
    2. Key Technical Concepts: 任务涉及的核心技术栈、开源框架、架构模式与工程规范；
    3. Files and Code: 精确文件路径清单、变更理由、关键函数代码片段；
    4. Errors and Fixes: 遇到的报错异常、修复方案及用户提出的明确纠正；
    5. Pending Jobs: 用户显式要求但尚未完结的任务清单；
    6. Current Work: 产生当前检查点时刻正在执行的具体操作；
    7. Next Step: 紧接当前检查点的单一步骤下一步行动建议；
    8. Critical Context: 关键决策理由、限制条件、用户偏好与未决开放问题。
"""

SUMMARY_OPEN_TAG = "<compacted-summary>"

SUMMARY_CLOSE_TAG = "</compacted-summary>"


COMPACTION_INSTRUCTION = 'You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.\n\nOutput EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.\n\n## Primary Request and Intent\n- [the user\'s original and evolving goals; quote verbatim where the exact wording matters]\n\n## Key Technical Concepts\n- [technologies, frameworks, patterns, and conventions in play]\n\n## Files and Code\n- [exact path: why it matters, key changes or snippets]\n\n## Errors and Fixes\n- [error: how it was resolved, plus any related user feedback]\n\n## Pending Jobs\n- [explicitly requested work not yet completed]\n\n## Current Work\n- [precisely what was in progress at this checkpoint]\n\n## Next Step\n- [the single next action, directly in line with the most recent request, or "(none)"]\n\n## Critical Context\n- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]\n\nRules:\n- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.\n- Capture user feedback and explicit instructions faithfully, especially corrections.\n- Do NOT mention this summarization request or that the context was compacted.\n- Output only the checkpoint text: do not call any tool or take any other action.\n- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure.'

CHECKPOINT_PREAMBLE = "This is an automatically generated checkpoint condensing an earlier span of the conversation to free up context. Treat the captured context as established background and build on it without restating it. Continue the task directly from the messages that follow, without acknowledging this checkpoint."



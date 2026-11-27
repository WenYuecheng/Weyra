"""基于 `SkillOpt` 与 `SkillDiff` 的在线异步技能演化协调器 (Online Skill Evolution Coordinator).

【架构定位】
在人机交互轮次结束时，由后台协程异步捕获交互证据与执行轨迹摘要（`trace_summary`），
根据显式 `skill_id` 或当前轮次实际激活的唯一技能定位目标，调用大模型产出符合
`microsoft/SkillOpt` 标准 `Edit` Schema 的有界补丁，并交由 `EvolutionStore.maintain()`
执行隔离验证门禁与持久化发布。

【开源框架与学术论文溯源】
1. Microsoft SkillOpt (`microsoft/SkillOpt`, NeurIPS 2026 Spotlight, arXiv:2605.23904, MIT):
   - 采用标准 `Edit` 操作原语（`append` / `insert_after` / `replace` / `delete`，字段 `target`/`content`）；
   - 向优化器注入 `edit_budget`（文本学习率预算）与 `rejected_edits`（被拒编辑缓冲），避免重复失败探索。
2. SkillDiff (`LitaoGuo/How-Self-Evolution-Reshapes-Skills`, arXiv:2611.25534, Nov 2026, MIT):
   - 向优化器提供 `focus_items`（正文中 `**[FOCUS]**` 标注的专家非先验条款），要求演进过程保留这些专家细节。
3. DeepSeek Harness (`packages/jobs` & `packages/subagent`):
   - 后台协程挂载至 `_background` 集合并绑定 `add_done_callback` 自动清理；
   - 使用独立 `RunContext` 隔离维护预算，并向会话推送 `skill_evolution_start` / `skill_evolution_end` 事件。
"""

from __future__ import annotations

import asyncio
import copy
import json
import time

from .contracts import Message, PreparedRequest, canonical, new_id
from .runtime_state import RunContext

# 对齐 microsoft/SkillOpt (`skillopt/types.py`) Edit Schema 的在线维护指令
MAINTENANCE_INSTRUCTION = """Maintain reusable skills from explicit durable user corrections, execution trace failures, or reusable methods.
Assistant statements and quoted material are not user preference evidence. One-off tasks stay in context.
Return exactly one JSON object, without Markdown fences. Choose discard if uncertain.
Actions:
1. Create a new skill (only when target is null and no existing skill covers the method):
{"action":"create","name":"new-skill","description":"Reusable method","when_to_use":"When to load this skill","body":"Step-by-step instructions."}
2. Update an existing target skill using Microsoft SkillOpt bounded Edit ops (`append`, `insert_after`, `replace`, `delete`):
{"action":"apply","name":"target-skill","base_hash":"target.content_hash","reasoning":"Why this patch fixes the issue","edits":[{"op":"replace","target":"exact old substring","content":"replacement text"}]}
- Supported edit fields: `op` (one of `append`, `insert_after`, `replace`, `delete`), `target` (exact unique substring in current body; required for `replace`/`delete`, optional anchor for `insert_after`), `content` (new text; required for `append`/`insert_after`/`replace`).
- Respect `edit_budget`: propose at most `edit_budget` edits in `edits`.
- Inspect `evidence.trace_summary` (`tool_calls`, `error_tools`, `retry_count`, `turn_state`) to diagnose missing checks or ordering errors.
- Preserve every expert item listed in `focus_items` (`**[FOCUS]**` clauses) unless replacement is strictly proven necessary.
- Avoid repeating failed edit directions listed in `rejected_edits`.
3. Discard when no durable reusable change is needed:
{"action":"discard","reason":"No durable reusable change is supported."}
"""


class OnlineSkillEvolution:
    """在线异步技能演化协调器（对齐 SkillOpt + SkillDiff）。"""

    def _schedule_evolution(self, window):
        """调度一次异步后台技能演化任务。"""
        evidence = copy.deepcopy(window)
        catalog = self.skills.capture_catalog()
        explicit_id = evidence.get("skill_id")
        used_ids = tuple(dict.fromkeys(evidence.get("used_skill_ids", ())))

        target = None
        resolution = {"source": "none", "reason": "not_found", "candidates": []}
        if explicit_id is not None:
            if explicit_id in catalog:
                target = catalog[explicit_id]
                resolution = {"source": "explicit_id", "reason": "resolved", "candidates": [target.name]}
            else:
                resolution = {"source": "explicit_id", "reason": "not_found", "candidates": []}
        elif len(used_ids) == 1 and used_ids[0] in catalog:
            target = catalog[used_ids[0]]
            resolution = {"source": "used_ids", "reason": "resolved", "candidates": [target.name]}
        elif len(used_ids) > 1:
            resolution = {
                "source": "used_ids",
                "reason": "ambiguous",
                "candidates": [k for k in used_ids if k in catalog],
            }

        event_id = new_id()

        async def maintain():
            ctx = RunContext(self.project, self.session, self.budget, self.permission_mode)
            ctx.budget_scope = "skill-maintenance-" + event_id
            self.budget.set_scope_limit(
                ctx.budget_scope,
                self.budget.maintenance_limit if self.budget.maintenance_limit is not None else 20000,
            )
            started = time.monotonic()

            async def propose(_):
                if resolution["reason"] == "ambiguous":
                    return {"action": "discard", "reason": "Multiple skills activated; abstaining"}
                edit_budget = self.evolution.current_edit_budget(target.name) if target else 1
                data = {
                    "evidence": evidence,
                    "available_skills": [
                        {"name": s.name, "description": s.description}
                        for _, s in sorted(catalog.items())[:10]
                    ],
                    "target": asdict_snapshot(target) if target else None,
                    "focus_items": list(target.focus_items) if target else [],
                    "edit_budget": edit_budget,
                    "rejected_edits": self.evolution.rejected_edits_prompt(target.name) if target else "",
                }
                model = self.runtime.routes.get("evolution", self.runtime.resolve("main"))
                request = PreparedRequest(
                    model,
                    MAINTENANCE_INSTRUCTION,
                    (Message("user", canonical(data), source="evidence"),),
                    purpose="evolution",
                )
                response = await self.runtime.complete(request, ctx)
                if response.stop_reason != "stop" or response.tool_calls:
                    raise ValueError("Maintenance requires a complete text-only response")
                return json.loads(response.content)

            try:
                if self.on_event:
                    res = self.on_event(
                        "skill_evolution_start",
                        {"skill": target.name if target else None, "event_id": event_id},
                    )
                    if asyncio.iscoroutine(res):
                        await res
                result = await self.evolution.maintain(
                    evidence,
                    propose,
                    permission_mode=self.permission_mode,
                    event_id=event_id,
                    evidence=evidence,
                    target_snapshot=target,
                    resolution=resolution,
                    validation_gate=self.evolution_validation_gate,
                    run_context=ctx,
                )
                result.update(
                    elapsed_seconds=time.monotonic() - started,
                    accounted_tokens=self.budget._scopes[ctx.budget_scope]["tokens"],
                )
                self.session.append("evolution_result", result, True)
                if self.on_event:
                    res = self.on_event("skill_evolution_end", result)
                    if asyncio.iscoroutine(res):
                        await res
            except asyncio.CancelledError:
                raise
            except Exception as error:
                err_data = {"status": "failed", "error": type(error).__name__}
                self.session.append("evolution_result", err_data, True)
                if self.on_event:
                    res = self.on_event("skill_evolution_end", err_data)
                    if asyncio.iscoroutine(res):
                        await res
            finally:
                await ctx.close()

        task = asyncio.create_task(maintain())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task


def asdict_snapshot(snapshot):
    """将 SkillSnapshot 数据类实例转换为只读字典表示。"""
    if snapshot is None:
        return None
    return {
        "name": snapshot.name,
        "body": snapshot.body,
        "content_hash": snapshot.content_hash,
        "version": snapshot.version,
        "description": snapshot.description,
        "when_to_use": snapshot.when_to_use,
    }

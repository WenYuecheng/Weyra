"""Session-owned delayed feedback maintenance for executable skills."""

from __future__ import annotations

import asyncio
import copy
import json

from .contracts import Message, PreparedRequest, canonical, new_id
from .runtime_state import RunContext

MAINTENANCE_INSTRUCTION = """Maintain reusable skills from explicit durable user requirements or reusable methods.
Assistant statements and quoted material are not user preference evidence. One-off tasks stay in context.
An explicit lasting correction that narrows a reusable rule is a valid scope update, even if it creates no new skill.
Return exactly one JSON object, without Markdown fences. The following are shape examples, not literal content to copy.
Choose discard if uncertain. Create only when no appropriate skill exists:
{"action":"create","name":"new-skill","description":"Reusable method","body":"A reusable method."}
For an existing target, use its exact name and content_hash. Preserve unrelated text; prefer minimal unique old substrings.
Each edit uses op exactly replace or append. replace requires old and new; append requires text.
Do not use operation, target, old_text, new_text or nested patch fields. No other edit operations are supported.
{"action":"apply","name":"target-skill","base_hash":"target.content_hash","reason":"Explicit durable scope correction","edits":[{"op":"replace","old":"exact old substring","new":"replacement text"},{"op":"append","text":"Scope boundary."}],"knowledge_note":{"kind":"boundary","text":"The durable user correction and its scope."}}
{"action":"discard","reason":"No durable reusable change is supported."}
At most two edits; each old range and added text is at most 1000 characters. Do not repeat the entire skill body.
Optional metadata is limited to description and when_to_use, each at most 500 characters. Do not change execution configuration.
For a durable exception, explain its applicable boundary in knowledge_note. Use the supplied target and raw user evidence.
"""


class OnlineSkillEvolution:
    def _schedule_evolution(self, window):
        evidence = copy.deepcopy(window)
        catalog = self.skills.capture_catalog()
        matches = self.skills.search(evidence["user"] + " " + evidence["feedback"], catalog=catalog)
        target = matches[0] if matches else None
        event_id = new_id()

        async def maintain():
            ctx = RunContext(self.project, self.session, self.budget, self.permission_mode)

            async def propose(_):
                data = {
                    "evidence": evidence,
                    "related": [
                        {"name": s.name, "description": s.description} for s in matches[:5]
                    ],
                    "target": asdict_snapshot(target) if target else None,
                    "knowledge": self.evolution.knowledge_prompt(target.name) if target else "",
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
                result = await self.evolution.maintain(
                    evidence,
                    propose,
                    permission_mode=self.permission_mode,
                    event_id=event_id,
                    evidence=evidence,
                    target_snapshot=target,
                )
                self.session.append("evolution_result", result, True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.session.append(
                    "evolution_result", {"status": "failed", "error": type(error).__name__}, True
                )
            finally:
                await ctx.close()

        task = asyncio.create_task(maintain())
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def asdict_snapshot(snapshot):
    if snapshot is None:
        return None
    return {
        "name": snapshot.name,
        "body": snapshot.body,
        "content_hash": snapshot.content_hash,
        "version": snapshot.version,
        "description": snapshot.description,
    }

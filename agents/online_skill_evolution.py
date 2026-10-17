"""Session-owned delayed feedback maintenance for executable skills."""

from __future__ import annotations

import asyncio
import copy
import json

from .contracts import Message, PreparedRequest, canonical, new_id
from .runtime_state import RunContext


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
                instruction = (
                    "Maintain reusable skills only from explicit durable user requirements or reusable methods. "
                    "Assistant statements are not user evidence. One-off requests and quoted content do not establish preferences. "
                    "Return JSON: action discard/create/apply, name, reason. create includes body and description; "
                    "apply includes base_hash from target.content_hash, edits (max2 replace/append with old/new/text), "
                    "optional description and when_to_use and knowledge_note {kind,text}. "
                    "When uncertain discard. Do not change execution metadata. Bound each edit to 1000 characters."
                )
                model = self.runtime.routes.get("evolution", self.runtime.resolve("main"))
                request = PreparedRequest(
                    model,
                    instruction,
                    (Message("user", canonical(data), source="evidence"),),
                    purpose="evolution",
                )
                response = await self.runtime.complete(request, ctx)
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

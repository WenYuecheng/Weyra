"""会话作用域内的异步延迟反馈技能自进化与持续维护引擎。

架构定位与设计哲学:
    本模块负责在人机交互的生命周期中实现“边交互边进化（Continual Learning in Interaction）”。
    在传统 Agent 架构中，规则提示词或技能往往是静态写死的，面对用户反复提出的边界纠正
    （如“正式报告先写结论，但故事叙述除外”）无法自我修正，或者因盲目全量改写而导致灾难性遗忘。
    本模块解耦了在线实时交互响应与离线异步技能进化，通过后台协程（Background Worker）在不阻塞
    用户对话的前提下，捕获交互视窗（Interaction Window）并提取经验规则。

学术创新与开源借鉴溯源:
    1. SkillOpt (arXiv:2605.23904, 2026):
       - 借鉴其“有界无梯度反馈优化”理论：拒绝让模型肆意重写整篇技能文档，强制施加硬性语法限制
         （At most two edits, ≤1000 chars per edit, exact old substring replace or append）。
       - 采用差异化补丁结构（action in {"create", "apply", "discard"}），将宽泛模糊的用户吐槽提炼为
         精确的边界范围修正（Scope Correction），最大程度保护非相关上下文的稳定性。
    2. WikiSkill (arXiv:2608.27454, 2026):
       - 严格贯彻“核心指令（Body）与领域知识（Knowledge Note）解耦”的范式：演化过程仅修改指令边界，
         将具体的纠错案例与反例边界持久化到专属的知识附注中，避免提示词膨胀。
    3. DeepSeek-Harness (packages/jobs & packages/subagent):
       - 后台协程生命周期闭环：使用专属的 `_background` 任务集合托管进化任务，挂载 `add_done_callback`
         自动清理完成的任务，并严格通过独立隔离的 RunContext 确保资源安全。
       - 状态机事件上报与持久化：在演化启动和结束时向 SessionStore 提交检查点（checkpoint=True），
         并通过 EventBus 向 UI/TUI 实时推送 `skill_evolution_start` 与 `skill_evolution_end` 事件。
"""

from __future__ import annotations

import asyncio
import copy
import json

from .contracts import Message, PreparedRequest, canonical, new_id
from .runtime_state import RunContext

# 基于 WikiSkill 与 SkillOpt 规范的形式化技能演化提示词。
# 严格禁止模型输出自由格式 Markdown，强制要求输出结构化单 JSON 对象。
MAINTENANCE_INSTRUCTION = """Maintain reusable skills from explicit durable user requirements or reusable methods.
Assistant statements and quoted material are not user preference evidence. One-off tasks stay in context.
An explicit lasting correction that narrows a reusable rule is a valid scope update, even if it creates no new skill.
Return exactly one JSON object, without Markdown fences. The following are shape examples, not literal content to copy.
Choose discard if uncertain. Create only when no appropriate skill exists:
{"action":"create","name":"new-skill","description":"Reusable method","body":"A reusable method."}
For an existing target, use its exact name and content_hash. Preserve unrelated text; prefer minimal unique old substrings.
Each edit uses op exactly replace or append. replace requires old and new; append requires text.
To delete an obsolete rule, use replace with an empty new string:
Deletion edit: {"op":"replace","old":"exact obsolete rule","new":""}
To insert after an anchor, replace that unique anchor with itself followed by the addition.
For a scope correction, replace only the overbroad clause; preserve unrelated rules and document its boundary.
Do not use operation, target, old_text, new_text or nested patch fields. No other edit operations are supported.
{"action":"apply","name":"target-skill","base_hash":"target.content_hash","reason":"Explicit durable scope correction","edits":[{"op":"replace","old":"exact old substring","new":"replacement text"},{"op":"append","text":"Scope boundary."}],"knowledge_note":{"kind":"boundary","text":"The durable user correction and its scope."}}
{"action":"discard","reason":"No durable reusable change is supported."}
At most two edits; each old range and added text is at most 1000 characters. Do not repeat the entire skill body.
Optional metadata is limited to description and when_to_use, each at most 500 characters. Do not change execution configuration.
For a durable exception, explain its applicable boundary in knowledge_note. Use the supplied target and raw user evidence.
"""


class OnlineSkillEvolution:
    """在线异步技能演化协调器（基于 SkillOpt 架构）。

    协调交互证据提取、技能目标消歧、LLM 结构化补丁提议、隔离沙箱回归验证以及动态生效全流程。
    """

    def _schedule_evolution(self, window):
        """调度一次异步后台技能演化任务。

        执行步骤:
            1. 证据深拷贝与目录快照：对传入的用户反馈交互视窗做 deepcopy，捕获当前 SkillStore 快照。
            2. 四级目标消歧解析：根据用户指令和显式/隐式上下文确定目标技能（或标记创建/丢弃）。
            3. 后台维护协程派发：创建后台任务 `maintain()`，并挂载至 `_background` 集合中防止被 GC 回收。

        Args:
            window: 包含 "user", "feedback", 可选 "skill_id", "used_skill_ids" 的证据字典。
        """
        evidence = copy.deepcopy(window)
        catalog = self.skills.capture_catalog()
        resolved = self.skills.resolve_target(
            evidence["user"] + " " + evidence["feedback"],
            explicit_id=evidence.get("skill_id"),
            used_ids=evidence.get("used_skill_ids", ()), catalog=catalog,
        )
        matches, target = resolved.candidates, resolved.target
        resolution = {"source": resolved.source, "reason": resolved.reason,
                      "candidates": [s.name for s in matches]}

        event_id = new_id()

        async def maintain():
            """异步演化工作流闭环实现。"""
            ctx = RunContext(self.project, self.session, self.budget, self.permission_mode)

            async def propose(_):
                """向演化专用大模型发起结构化推演请求。"""
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
                # 触发演化启动事件（通知前端或 TUI 渲染思考状态）
                if self.on_event:
                    res = self.on_event("skill_evolution_start", {"skill": target.name if target else None, "event_id": event_id})
                    if asyncio.iscoroutine(res):
                        await res
                # 进入底层通用演化核心：执行语法校验、文本补丁应用与双盲门禁验证
                result = await self.evolution.maintain(
                    evidence,
                    propose,
                    permission_mode=self.permission_mode,
                    event_id=event_id,
                    evidence=evidence,
                    target_snapshot=target,
                    resolution=resolution,
                    validation_gate=self.evolution_validation_gate,
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
                self.session.append(
                    "evolution_result", err_data, True
                )
                if self.on_event:
                    res = self.on_event("skill_evolution_end", err_data)
                    if asyncio.iscoroutine(res):
                        await res
            finally:
                await ctx.close()

        task = asyncio.create_task(maintain())
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def asdict_snapshot(snapshot):
    """将 SkillSnapshot 数据类实例转换为安全的只读字典形式。"""
    if snapshot is None:
        return None
    return {
        "name": snapshot.name,
        "body": snapshot.body,
        "content_hash": snapshot.content_hash,
        "version": snapshot.version,
        "description": snapshot.description,
    }

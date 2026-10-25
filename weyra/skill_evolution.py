"""有界审计、知识解耦与自主演化技能存储引擎 (Bounded Skill Evolution & Knowledge Store)。

【架构定位】
本模块负责 Weyra 智能体在长周期任务探索中的技能（Skill）自我迭代与自我修正。
系统通过捕获执行反馈与失败用例，生成微调补丁并进行封闭沙箱评测，从而实现智能体技能库的
终身持续学习（Lifelong Continual Learning）。

【开源与学术论文借鉴体系】
1. 借鉴 WikiSkill (arXiv:2608.27454, 2026) 的指令与知识分层解耦机制 (Explicit Instruction vs Knowledge Separation)：
   - 严禁将任务偶发观测或长篇报错日志直接污染至核心可执行提示词正文中；
   - 将环境经验与特定边界提炼为轻量结构化知识条目（`knowledge_note`, 限制在 300 字符内，最多保留 10 条），
     通过 `knowledge.json` 独立存储并在检索时按需拼接；
2. 借鉴 SkillOpt (arXiv:2605.23904, 2026) 的有界补丁与闭环沙箱验证范式 (Bounded Patching & Closed-Loop Validation)：
   - 严格限定单次演化补丁规模：最多 1~2 处编辑，单次替换或追加长度硬性受限于 1,000 字符内，杜绝模型幻觉全量覆写；
   - 强制沙箱隔离验证门禁 (`validation_gate`)：候选补丁发布前必须在隔离环境中重放基线用例；
   - 零退化门禁指标：候选技能必须取得严格大于旧版本的成功数 (`candidate.successes > old.successes`)
     且不增加任何硬失败 (`candidate.hard_failures <= old.hard_failures`)，否则绝对拒绝合入；
3. 单调语义化版本升级与可回滚历史快照 (Monotonic SemVer & Atomic Rollback)：
   - 每次成功演化单调自增修订版本号（如 1.0.0 -> 1.0.1）；
   - 写入前保存 `<version>.md` 快照与 `provenance.jsonl` 审计追溯链条，支持版本回滚；
4. 崩溃恢复自治状态机 (`recover`)：
   - 若系统在 `prepared` 阶段突发中断，恢复流程严格保持幂等，仅对已落盘完成的技能生成镜像，杜绝意外发布未受控的中间代码。
"""

from __future__ import annotations
import asyncio
from dataclasses import replace
from types import MappingProxyType
import json
import os
from datetime import datetime, timezone
import threading
import uuid
from pathlib import Path
from .knowledge import atomic, sha, reject_symlinks
from .runtime_state import BudgetExceeded


class EvolutionStore:
    """技能演化存储与维护管理器 (Evolution Store)。

    管理项目技能的持久化审计目录、历史快照、外部知识库及受控补丁应用流程。
    """

    def __init__(self, skills, state_dir=None, session=None):
        """初始化技能演化存储与审计管理器。

        【架构定位】
        负责绑定核心技能集合，确定演化快照与元数据持久化根路径，初始化审计并发控制
        锁，并对系统异常崩溃留下的中间态执行幂等恢复。

        【核心不变性与安全约束】
        1. 路径穿越与符号链接防护：强制经由 `reject_symlinks` 校验目标目录真实性；
        2. 并发隔离：线程级重入锁 `_lock` 保障文件与审计写入的强一致性，`_async_locks`
           针对各个技能提供异步细粒度排他调度；
        3. 启动即恢复：实例化时无条件触发 `self.recover()`，保证系统状态不残留悬空状态。

        Args:
            skills: SkillStore 技能库管理器实例。
            state_dir: 可选的自定义状态持久化根目录；若为空则默认使用 `.weyra/skill-evolution`。
            session: 可选的当前会话实例，用于审计事件的双写与镜像投递。
        """
        self.skills = skills
        self.root = Path(state_dir) if state_dir else skills.root / ".weyra" / "skill-evolution"
        reject_symlinks(self.root)
        self.root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._async_locks = {}
        self.session = session
        self._session_events = {
            self._event_key(record.get("data", {}))
            for record in getattr(session, "records", ())
            if record.get("kind") == "skill_evolution"
        }
        self.legacy_roots = [] if state_dir else [skills.root / ".weyra" / "evolution"]
        self.recovery_results = []
        self.auxiliary_errors = []
        self.recover()

    def _key(self, name):
        """计算技能唯一的内容寻址与目录隔离 SHA-256 哈希键。

        【架构定位】
        根据技能的绝对文件系统路径与技能名称计算哈希散列，作为演化数据落盘的子目录名称。

        Args:
            name: 技能唯一名称。

        Returns:
            str: 64 位十六进制 SHA-256 散列字符串。
        """
        try:
            path = self.skills.load(name).path
        except (KeyError, ValueError):
            path = self.skills.root / ".weyra" / "skills" / name / "SKILL.md"
        return sha(str(path.resolve()) + "\0" + name)

    def _dir(self, name):
        """解析并安全创建指定技能的演化快照与审计落盘目录。

        【架构定位】
        根据 `_key(name)` 定位子目录，校验符号链接合法性后自动补齐层级目录。

        Args:
            name: 技能唯一名称。

        Returns:
            Path: 指向该技能演化目录的已解析绝对路径。
        """
        p = self.root / self._key(name)
        reject_symlinks(p)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _event_key(event):
        """生成演化审计事件的不可变四元组指纹，用于会话日志去重。

        Args:
            event: 包含 event_id、status 等字段的事件字典。

        Returns:
            tuple: (event_id, status, skill_path, recovered) 四元组。
        """
        return (
            event.get("event_id"),
            event.get("status"),
            event.get("skill_path"),
            event.get("recovered", False),
        )

    def _mirror_event(self, event):
        """将演化审计事件原子镜像投递至持久化会话日志中。

        【架构定位】
        打通技能演化引擎与 Weyra 会话树状态机。所有演化状态跃迁均通过 Checkpoint 记录
        至会话事件流中，确保可观测性与持久化统一。

        Args:
            event: 结构化演化事件字典。
        """
        key = self._event_key(event)
        if self.session is not None and key not in self._session_events:
            self.session.append("skill_evolution", dict(event), checkpoint=True)
            self._session_events.add(key)

    def _record_in(self, folder, event):
        """向目标目录下的 `provenance.jsonl` 原子追加写入单条结构化审计事件。

        【核心不变性与安全约束】
        1. 符号链接防卫：严禁向重定向软链接写入；
        2. 掉电安全保障：写入后显式执行 `flush()` 与 `os.fsync()`，保证落盘绝对一致；
        3. 会话联动：同步触发 `_mirror_event`。

        Args:
            folder: 目标技能的演化持久化目录路径。
            event: 结构化审计事件字典。
        """
        path = folder / "provenance.jsonl"
        reject_symlinks(path)
        separator = ""
        if path.exists() and path.stat().st_size:
            with path.open("rb") as previous:
                previous.seek(-1, os.SEEK_END)
                if previous.read(1) != b"\n":
                    separator = "\n"
        with path.open("a", encoding="utf-8") as output:
            output.write(separator + json.dumps(event, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        self._mirror_event(event)

    def _record(self, name, event):
        """向指定名称技能的演化目录记录单条结构化审计事件的便捷封装。

        Args:
            name: 技能唯一名称。
            event: 结构化审计事件字典。
        """
        self._record_in(self._dir(name), event)

    def _read_records(self, path):
        """从指定的 `provenance.jsonl` 文件中安全解析并反序列化所有审计事件。

        【容错设计】
        遇到截断或损坏的不完整 JSONL 行记录时不崩溃退出，记录辅助性警告信息并跳过。

        Args:
            path: 指向 `provenance.jsonl` 的文件系统路径。

        Returns:
            list[dict]: 成功反序列化的事件字典列表。
        """
        if not path.exists():
            return []
        reject_symlinks(path)
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    records.append(event)
            except ValueError:
                self.auxiliary_errors.append(
                    "Incomplete skill evolution audit record: " + str(path)
                )
        return records

    def history(self, name):
        """聚合查询指定技能在历史遗留与当前演化目录下的全量演化审计轨迹。

        Args:
            name: 技能唯一名称。

        Returns:
            list[dict]: 按时间先后顺序聚合的历史事件列表。
        """
        key = self._key(name)
        records = []
        for root in [*self.legacy_roots, self.root]:
            records.extend(self._read_records(root / key / "provenance.jsonl"))
        return records

    def recover(self):
        """Finalize an already published prepared edit; never publish during recovery."""
        with self._lock:
            for root in [*self.legacy_roots, self.root]:
                if not root.exists():
                    continue
                try:
                    reject_symlinks(root)
                    paths = sorted(root.glob("*/provenance.jsonl"))
                except (OSError, ValueError) as error:
                    self.auxiliary_errors.append(str(error))
                    continue
                for path in paths:
                    try:
                        records = self._read_records(path)
                        for event in records:
                            try:
                                self._mirror_event(event)
                            except Exception as error:
                                self.auxiliary_errors.append(str(error))
                        terminal = {
                            event.get("event_id")
                            for event in records
                            if event.get("status")
                            in {"updated", "rejected", "discarded", "interrupted", "conflict"}
                        }
                        pending = {
                            event.get("event_id"): event
                            for event in records
                            if event.get("status") == "prepared"
                            and event.get("event_id") not in terminal
                        }
                        for event in pending.values():
                            result = self._recover_prepared(path.parent, event)
                            self.recovery_results.append(result)
                    except (OSError, ValueError, TypeError) as error:
                        self.auxiliary_errors.append(str(error))
        return list(self.recovery_results)

    def _recover_prepared(self, folder, event):
        """对突发中断遗留的处于 `prepared` 状态的演化提议执行确定性故障恢复。

        【架构定位】
        自治状态机核心恢复逻辑：系统崩溃或断电重启后，审计日志中可能存在已记录为 `prepared`
        但尚未确认落盘的中间事件。本方法依据实际文件系统内容与哈希指纹严格核对，执行幂等恢复。

        【核心不变性与安全约束】
        1. 绝不隐式发布（Never Publish During Recovery）：若磁盘上的实际技能文件并未落盘为新版本，
           绝不在恢复阶段执行覆盖发布，而是标记为 `interrupted` 保持现场；
        2. 严格哈希校验：只有当当前磁盘文件哈希与 `content_hash` 严格一致时，才确认已落盘成功，
           补全快照备份并将状态标记为 `updated`；
        3. 冲突防御：若当前内容既非 `base_hash` 亦非 `content_hash`，则判定为外部并发修改冲突，
           标记为 `conflict` 并保留现场。

        Args:
            folder: 目标技能的演化目录路径。
            event: 待恢复的 `prepared` 审计事件字典。

        Returns:
            dict: 恢复终态事件记录字典（状态为 updated / interrupted / conflict 等）。
        """
        result = {**event, "recovered": True, "auxiliary_errors": []}
        try:
            recorded_path = event.get("skill_path")
            if not isinstance(recorded_path, str) or not Path(recorded_path).is_absolute():
                result.update(
                    status="interrupted",
                    reason="Prepared audit lacks an exact skill_path; publication was not inferred",
                )
            else:
                skill_path = Path(recorded_path)
                reject_symlinks(skill_path)
                allowed = [Path(directory).resolve() for directory in self.skills.skill_dirs]
                if skill_path.name != "SKILL.md" or not any(
                    skill_path.resolve().is_relative_to(directory) for directory in allowed
                ):
                    raise ValueError("Prepared skill path is outside configured skill directories")
                expected_key = sha(str(skill_path.resolve()) + "\0" + event.get("name", ""))
                if folder.name != expected_key:
                    raise ValueError("Prepared skill path does not match audit identity")
                current = skill_path.read_text(encoding="utf-8") if skill_path.exists() else None
                current_hash = sha(current) if current is not None else None
                if current_hash == event.get("content_hash") and current is not None:
                    version = event.get("version", "")
                    if len(version.split(".")) != 3 or not all(
                        part.isdigit() for part in version.split(".")
                    ):
                        raise ValueError("Prepared version is invalid")
                    snapshot = folder / (version + ".md")
                    if (
                        event.get("snapshot_path")
                        and Path(event["snapshot_path"]).resolve() != snapshot.resolve()
                    ):
                        raise ValueError("Prepared snapshot path does not match audit identity")
                    if event.get("snapshot_hash", current_hash) != current_hash:
                        raise ValueError("Prepared snapshot hash does not match published skill")
                    result["snapshot_path"] = str(snapshot.resolve())
                    result["status"] = "updated"
                    try:
                        atomic(snapshot, current)
                        if event.get("knowledge_note"):
                            self._save_note_at(
                                folder, event["knowledge_note"], event["event_id"], version
                            )
                    except Exception as error:
                        result["status"] = "recovery_pending"
                        result["auxiliary_errors"].append(str(error))
                elif current is None or current_hash == event.get("base_hash"):
                    result.update(
                        status="interrupted",
                        reason="Prepared edit was not published; original file was preserved",
                    )
                else:
                    result.update(
                        status="conflict",
                        reason="Skill content changed after preparation; file was preserved",
                    )
        except (OSError, ValueError, TypeError) as error:
            result.update(status="conflict", reason="Prepared edit could not be safely recovered")
            result["auxiliary_errors"].append(str(error))
        try:
            self._record_in(folder, result)
        except Exception as error:
            result["auxiliary_errors"].append(str(error))
        return result

    def _completed(self, name, event_id):
        """查询指定技能与 event_id 是否已成功完成更新（幂等防重放检查）。

        Args:
            name: 技能唯一名称。
            event_id: 演化提议唯一标识符。

        Returns:
            dict | None: 若已完成更新则返回历史事件字典，否则返回 None。
        """
        return next(
            (
                dict(event)
                for event in reversed(self.history(name))
                if event.get("event_id") == event_id and event.get("status") == "updated"
            ),
            None,
        )

    def _reject(self, name, event_id, reason, record=True, knowledge=False):
        """记录演化提议被拒绝事件，并可选沉淀拒绝原因至外部解耦知识库。

        【架构定位】
        当补丁因权限不足、校验冲突、门禁未达标或语法非法时触发。可将失败模式沉淀为
        `rejected_update` 知识条目，供模型在后续演化生成中作为负反馈提示词参考。

        Args:
            name: 技能唯一名称。
            event_id: 演化提议唯一标识符。
            reason: 拒绝或失败的具体原因描述。
            record: 是否向 `provenance.jsonl` 持久化该拒绝审计记录。
            knowledge: 是否将本次失败原因沉淀至 `knowledge.json` 中。

        Returns:
            dict: 拒绝状态字典。
        """
        result = {
            "status": "rejected",
            "event_id": event_id,
            "reason": reason,
            "failure_kind": "failure_to_apply",
            "task_quality": None,
        }
        try:
            if record:
                self._record(name, result)
            if knowledge:
                try:
                    version = self.skills.load(name).version
                except (KeyError, ValueError):
                    version = "unknown"
                self._save_note(
                    name,
                    {"kind": "rejected_update", "text": "failure_to_apply: " + reason[:270]},
                    event_id,
                    version,
                )
        except Exception as error:
            result["auxiliary_errors"] = [str(error)]
        return result

    def _note(self, note, event_id, version):
        """规整化并严格校验单条经验知识条目（遵循 WikiSkill 解耦机制）。

        【核心不变性与安全约束】
        1. 类别白名单：kind 必须为 `observation`、`boundary` 或 `rejected_update` 之一；
        2. 字符长度硬约束：文本有效内容必须非空且严格不超过 300 字符，杜绝无限制日志膨胀。

        Args:
            note: 字符串或字典形式的知识内容。
            event_id: 产生该知识条目的演化事件 ID。
            version: 关联的技能版本号。

        Returns:
            dict: 规整化后的标准知识记录字典。

        Raises:
            ValueError: 当知识类别无效或字符超长时抛出。
        """
        if isinstance(note, str):
            note = {"kind": "observation", "text": note}
        if not isinstance(note, dict) or note.get("kind") not in {
            "observation",
            "boundary",
            "rejected_update",
        }:
            raise ValueError("Invalid knowledge kind")
        text = note.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ValueError("Knowledge text must contain 1 to 300 characters")
        return {
            "event_id": event_id,
            "kind": note["kind"],
            "text": text,
            "time": datetime.now(timezone.utc).isoformat(),
            "skill_version": version,
        }

    def knowledge(self, name):
        """查询指定技能关联的最新解耦知识经验列表（最多 10 条）。

        Args:
            name: 技能唯一名称。

        Returns:
            list[dict]: 结构化知识条目列表。
        """
        return self._knowledge_at(self._dir(name))

    def _knowledge_at(self, folder):
        """从指定目录下的 `knowledge.json` 安全反序列化读取并去重知识列表。

        【容量约束】
        严格保留最近 10 条有效知识条目，遵循滑窗淘汰机制。

        Args:
            folder: 目标技能演化目录。

        Returns:
            list[dict]: 归一化的最新知识列表（上限 10 条）。
        """
        path = folder / "knowledge.json"
        try:
            data = json.loads(path.read_text()) if path.exists() else []
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        records = []
        seen = set()
        for item in data:
            try:
                if isinstance(item, str):
                    record = self._note(item, "legacy:" + sha(item), "unknown")
                    record["time"] = "unknown"
                else:
                    record = self._note(
                        item, item.get("event_id", "unknown"), item.get("skill_version", "unknown")
                    )
                    record["time"] = item.get("time", "unknown")
                key = (record["event_id"], record["kind"], record["text"])
                if key not in seen:
                    records.append(record)
                    seen.add(key)
            except (ValueError, AttributeError, TypeError):
                continue
        return records[-10:]

    def _save_note(self, name, note, event_id, version):
        """向指定名称技能的知识库安全持久化单条新知识经验。

        Args:
            name: 技能唯一名称。
            note: 字符串或字典形式的知识内容。
            event_id: 关联的演化事件 ID。
            version: 关联的技能版本号。
        """
        return self._save_note_at(self._dir(name), note, event_id, version)

    def _save_note_at(self, folder, note, event_id, version):
        """向目标目录的 `knowledge.json` 原子合并并持久化最新知识记录。

        【核心不变性与安全约束】
        以 (event_id, kind, text) 为唯一键去重，保留最新 10 条并经由 `atomic()` 原子落盘。

        Args:
            folder: 目标技能演化目录。
            note: 待保存的知识内容。
            event_id: 关联演化事件 ID。
            version: 关联技能版本号。
        """
        record = self._note(note, event_id, version)
        records = self._knowledge_at(folder)
        key = (record["event_id"], record["kind"], record["text"])
        records = [
            item for item in records if (item["event_id"], item["kind"], item["text"]) != key
        ]
        records.append(record)
        atomic(folder / "knowledge.json", json.dumps(records[-10:], ensure_ascii=False))

    def knowledge_prompt(self, name):
        """将指定技能的外部解耦知识格式化为注入模型的提示词切片。

        【字符预算】
        格式化后的知识提示词片段严格硬性截断于 2,000 字符以内，防止上下文膨胀。

        Args:
            name: 技能唯一名称。

        Returns:
            str: 格式化后的知识经验提示词文本（<= 2,000 字符）。
        """
        return "\n".join(
            f"[{item['kind']}; version {item['skill_version']}] {item['text']}"
            for item in self.knowledge(name)
        )[:2000]

    def apply(
        self,
        name,
        edits,
        event_id=None,
        base_hash=None,
        description=None,
        when_to_use=None,
        reason="",
        permission_mode="default",
        authorized=False,
        knowledge_note=None,
        _prepare_only=False,
        _validation="unvalidated",
        _expected_resources=None,
    ):
        """应用受限的结构化补丁，更新指定技能并完成持久化与版本单调升级 (Apply Bounded Text Patch)。

        【SkillOpt / WikiSkill 契约检查】
        1. 权限闸门：仅允许在 acceptEdits 或 bypassPermissions 权限模式下生效，或持有显式授权；
        2. 补丁规模限制：强制 1 到 2 处编辑，单次 replace/append 不得超出 1,000 字符；
        3. 唯一锚点匹配：replace 操作匹配的旧文本片段在原技能正文中必须严格唯一出现一次；
        4. 语义版本自增：成功应用后语义版本末尾 patch 位自增 1，原文件备份为 `<version>.md`；
        5. 知识解耦落盘：若附带 knowledge_note，将其解耦写入 `knowledge.json` 而非主提示词正文。
        """
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            completed = self._completed(name, event_id)
            if completed:
                return completed
            try:
                snap = self.skills.load(name)
                if _expected_resources is not None and snap.resources != _expected_resources:
                    raise ValueError("Resource drift during validation")
                if base_hash != snap.content_hash:
                    raise ValueError("Base hash conflict")
                if not 1 <= len(edits) <= 2:
                    raise ValueError("One or two edits required")
                spans = []
                appends = []
                for edit in edits:
                    kind = edit.get("type", edit.get("op"))
                    if kind == "replace":
                        old = edit.get("old", "")
                        new = edit.get("new", "")
                        if (
                            not old
                            or len(old) > 1000
                            or len(new) > 1000
                            or snap.body.count(old) != 1
                        ):
                            raise ValueError("Replace requires a unique bounded old range")
                        start = snap.body.index(old)
                        spans.append((start, start + len(old), new))
                    elif kind == "append":
                        text = edit.get("text", edit.get("new", ""))
                        if not text or len(text) > 1000 or text in snap.body or text in appends:
                            raise ValueError("Duplicate or invalid append")
                        appends.append(text)
                    else:
                        raise ValueError("Unsupported edit")
                spans.sort()
                if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
                    raise ValueError("Overlapping edits")
                body = snap.body
                for start, end, new in reversed(spans):
                    body = body[:start] + new + body[end:]
                if appends:
                    body += "\n" + "\n".join(appends)
                return self._publish(
                    snap, body, event_id, description, when_to_use, reason, knowledge_note,
                    _prepare_only, _validation
                )
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as e:
                return self._reject(name, event_id, str(e), knowledge=True)

    def _publish(
        self,
        snap,
        body,
        event_id,
        description=None,
        when_to_use=None,
        reason="",
        knowledge_note=None,
        prepare_only=False,
        validation="unvalidated",
    ):
        """执行单调语义版本号自增、快照备份、审计记录与文件原子落盘发布全流程。

        【架构定位】
        技能持久化发布事务核心：完成 文本补丁后的正文校验、语义版本递增、快照归档与原子双写。

        【核心不变性与安全约束】
        1. 正文完整性：正文必须包含实质内容，严禁正文行中出现独立的 `---` 分界符以防破坏 Frontmatter 解析；
        2. 元数据预算：`description` 与 `when_to_use` 字符数上限硬性约束为 500 字符以内；
        3. 单调版本自增：语义化版本（X.Y.Z）末尾 patch 字段严格单调加 1（例如 1.0.0 -> 1.0.1）；
        4. 并发与幂等性：落盘前后通过 SHA-256 哈希双重比对原技能文件，杜绝并发竞争覆盖；
        5. 事务原子落盘：先保存旧版本快照 `<version>.md`，发布 `prepared` 审计事件，使用 `atomic()` 原子
           覆写目标技能文件，随后保存新版本快照并投递 `updated` 终态事件。

        Args:
            snap: 当前技能的只读快照对象 SkillSnapshot。
            body: 更新后的新技能正文文本。
            event_id: 本次发布的唯一演化事件标识符。
            description: 可选的新功能描述；若为 None 则沿用快照值。
            when_to_use: 可选的新触发时机描述；若为 None 则沿用快照值。
            reason: 触发本次演化发布的理由说明。
            knowledge_note: 可选的附带外部解耦知识条目。
            prepare_only: 若为 True 则仅在内存中组装新快照对象并返回，不执行物理落盘。
            validation: 门禁验证状态标记（如 "validated" 或 "unvalidated"）。

        Returns:
            dict | SkillSnapshot: 物理落盘时返回更新后的终态审计事件字典；若为 prepare_only 则返回替换后的快照对象。

        Raises:
            ValueError: 当正文损坏、元数据超长、语义版本非法或检测到外部并发修改时抛出。
        """
        if not body.strip() or any(line.strip() == "---" for line in body.splitlines()):
            raise ValueError("Invalid body/frontmatter delimiter")
        description = snap.description if description is None else description
        when_to_use = snap.when_to_use if when_to_use is None else when_to_use
        if (
            body == snap.body
            and description == snap.description
            and when_to_use == snap.when_to_use
        ):
            raise ValueError("No effective change")
        if len(description) > 500 or len(when_to_use) > 500:
            raise ValueError("Metadata exceeds 500 characters")
        if knowledge_note is not None:
            self._note(knowledge_note, event_id, snap.version)
        parts = snap.version.split(".")
        if len(parts) != 3 or not all(x.isdigit() for x in parts):
            raise ValueError("Invalid semantic version")
        version = ".".join(parts[:2] + [str(int(parts[2]) + 1)])
        metadata = {
            **snap.metadata,
            "name": snap.name,
            "version": version,
            "description": description,
            "when_to_use": when_to_use,
        }
        if snap.context:
            metadata["context"] = snap.context
        raw = (
            "---\n"
            + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k, v in metadata.items())
            + "\n---\n"
            + body
        )
        for relative_path in snap.resources:
            self.skills.read_resource(snap, relative_path)
        if prepare_only:
            return replace(snap, body=body, version=version, description=description,
                           when_to_use=when_to_use, metadata=MappingProxyType(metadata), content_hash=sha(raw))
        folder = self._dir(snap.name)
        old = snap.path.read_text(encoding="utf-8")
        if sha(old) != snap.content_hash:
            raise ValueError("Concurrent modification")
        atomic(folder / (snap.version + ".md"), old)
        event = {
            "status": "prepared",
            "validation": validation,
            "name": snap.name,
            "event_id": event_id,
            "version": version,
            "base_hash": snap.content_hash,
            "base_snapshot_path": str((folder / (snap.version + ".md")).resolve()),
            "snapshot_path": str((folder / (version + ".md")).resolve()),
            "snapshot_hash": sha(raw),
            "skill_path": str(snap.path.resolve()),
            "knowledge_note": knowledge_note,
            "content_hash": sha(raw),
            "reason": reason,
        }
        self._record(snap.name, event)
        if sha(snap.path.read_text(encoding="utf-8")) != snap.content_hash:
            raise ValueError("Concurrent modification")
        atomic(snap.path, raw)
        result = {**event, "status": "updated", "auxiliary_errors": []}
        try:
            atomic(folder / (version + ".md"), raw)
            if knowledge_note:
                self._save_note(snap.name, knowledge_note, event_id, version)
            self._record(snap.name, result)
        except Exception as e:
            result["auxiliary_errors"].append(str(e))
        return result

    def rollback(self, name, version, event_id=None, permission_mode="default", authorized=False):
        """基于历史快照实现指定技能的可追溯版本回滚。

        【架构定位】
        在线演化安全防护网：当新版技能在生产环境中发生劣化或被用户撤销时，直接读取历史
        归档的 `<version>.md` 纯文本快照，解析 Frontmatter 后生成递增的发布版本并落盘。

        【核心不变性与安全约束】
        1. 权限拦截：必须具备 acceptEdits/bypassPermissions 或显式授权；
        2. 幂等性守卫：若该 event_id 已经处于 `updated` 完成态，则直接返回；
        3. 版本有效性校验：回滚目标版本必须由合法的数字与点组成，且对应历史快照必须客观存在。

        Args:
            name: 待回滚的技能唯一名称。
            version: 目标回滚版本的版本号字符串（如 "1.0.0"）。
            event_id: 可选的演化事件唯一 ID。
            permission_mode: 权限模式字符串。
            authorized: 是否持有显式授权布尔标记。

        Returns:
            dict: 回滚发布终态事件记录字典。
        """
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            completed = self._completed(name, event_id)
            if completed:
                return completed
            try:
                if not version or any(c not in "0123456789." for c in version):
                    raise ValueError("Invalid version")
                from .knowledge import parse_markdown

                metadata, body = parse_markdown((self._dir(name) / (version + ".md")).read_text())
                return self._publish(
                    self.skills.load(name),
                    body,
                    event_id,
                    metadata.get("description", ""),
                    metadata.get("when_to_use", ""),
                    "rollback " + version,
                )
            except (ValueError, KeyError, OSError, RuntimeError) as e:
                return self._reject(name, event_id, str(e), knowledge=True)

    def discard(self, name, event_id=None, reason=""):
        """显式丢弃或中止无价值的技能演化提议，并写入持久化审计轨迹。

        Args:
            name: 技能唯一名称。
            event_id: 可选的演化事件唯一 ID。
            reason: 丢弃或废弃的具体原因。

        Returns:
            dict: 包含 status="discarded" 的审计事件字典。
        """
        result = {"status": "discarded", "event_id": event_id or uuid.uuid4().hex, "reason": reason}
        try:
            self._record(name, result)
        except Exception as error:
            result["auxiliary_errors"] = [str(error)]
        return result

    def create(
        self,
        name,
        body,
        description="",
        when_to_use="",
        permission_mode="default",
        authorized=False,
        event_id=None,
    ):
        r"""全新自研技能的安全受控创建与初始版本 (1.0.0) 审计落盘。

        【架构定位】
        支持智能体从零构建并持久化全新自研工具技能（Novel Skill Creation）。

        【核心不变性与安全约束】
        1. 命名安全与沙箱隔离：技能名严禁包含路径跳转字符（`.`, `..`, `/`, `\`）；
        2. 防篡改冲突检查：目标技能文件必须不存在，且严禁与已加载的全局技能目录发生同名命名碰撞；
        3. 单调初始版本：初始版本强绑定为 "1.0.0"，生成初始快照 `1.0.0.md`，并在演化审计中落盘
           `prepared` 与 `updated` 完整生命周期事件。

        Args:
            name: 新建技能的英文标识名称。
            body: 新建技能的主提示词指令正文。
            description: 技能的功能摘要说明（<= 500 字符）。
            when_to_use: 技能调用的启发式时机描述（<= 500 字符）。
            permission_mode: 当前运行环境权限模式。
            authorized: 是否持有显式授权。
            event_id: 可选的事件唯一 ID。

        Returns:
            dict: 创建完成的终态审计事件字典。
        """
        event_id = event_id or uuid.uuid4().hex
        with self._lock:
            if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
                return self._reject(name, event_id, "Permission required", record=False)
            completed = self._completed(name, event_id)
            if completed:
                return completed
            if (
                not name
                or name in (".", "..")
                or "/" in name
                or "\\" in name
                or not body.strip()
                or any(x.strip() == "---" for x in body.splitlines())
                or len(description) > 500
                or len(when_to_use) > 500
            ):
                return self._reject(name, event_id, "Invalid new skill")
            path = self.skills.root / ".weyra" / "skills" / name / "SKILL.md"
            try:
                reject_symlinks(path)
            except ValueError as error:
                return self._reject(name, event_id, str(error))
            if path.exists() or name in self.skills.capture_catalog():
                return self._reject(name, event_id, "Skill exists")
            raw = (
                "---\n"
                + "\n".join(
                    k + ": " + json.dumps(v, ensure_ascii=False)
                    for k, v in dict(
                        name=name, version="1.0.0", description=description, when_to_use=when_to_use
                    ).items()
                )
                + "\n---\n"
                + body
            )
            event = {
                "status": "prepared",
                "validation": "unvalidated",
                "event_id": event_id,
                "name": name,
                "version": "1.0.0",
                "content_hash": sha(raw),
                "base_hash": None,
                "skill_path": str(path.resolve()),
                "snapshot_path": str((self._dir(name) / "1.0.0.md").resolve()),
                "snapshot_hash": sha(raw),
            }
            try:
                atomic(self._dir(name) / "1.0.0.md", raw)
                self._record(name, event)
                atomic(path, raw)
            except (OSError, ValueError, RuntimeError) as error:
                return self._reject(name, event_id, str(error))
            result = {**event, "status": "updated", "auxiliary_errors": []}
            try:
                self._record(name, result)
            except Exception as e:
                result["auxiliary_errors"].append(str(e))
            return result

    async def maintain(
        self,
        window,
        model_call,
        permission_mode="default",
        authorized=False,
        event_id=None,
        evidence=None,
        target_snapshot=None,
        validation_gate=None,
        resolution=None,
    ):
        """记录执行证据并调度大模型生成补丁，经沙箱验证门禁后原子更新技能 (Closed-Loop Skill Maintenance)。

        【SkillOpt 闭环演化全流程】
        1. 证据固化：将用户交互窗口与执行证据追加写入 `evidence.jsonl`，确保审计追溯；
        2. 补丁提议生成：调用 `model_call` 获取提议并持久化至 `proposals/<hash>.json`；
        3. 目标基线锁定：严格以调用前程序锁定的快照为基准（杜绝并发篡改或模型捏造基线哈希）；
        4. 隔离验证门禁 (`validation_gate`)：
           对基线技能与候选技能执行双盲隔离执行测试。验证门禁必须返回严格改善的客观分数：
           `candidate.successes > old.successes` 且 `candidate.hard_failures <= old.hard_failures`；
        5. 原子发布：只有通过门禁检验的补丁才允许升级生效，否则触发拒绝合入。
        """
        event_id = event_id or uuid.uuid4().hex
        if permission_mode not in ("acceptEdits", "bypassPermissions") and not authorized:
            return {
                "status": "rejected",
                "event_id": event_id,
                "reason": "Permission required",
                "failure_kind": "failure_to_apply",
                "task_quality": None,
            }
        # Capture all available bases before awaiting the model. A proposal may select a
        # catalog entry, but cannot supply the comparison hash used for publication.
        catalog = self.skills.capture_catalog() if target_snapshot is None else None
        frozen_window = json.loads(json.dumps(window, ensure_ascii=False))
        frozen_evidence = json.loads(
            json.dumps(evidence if evidence is not None else window, ensure_ascii=False)
        )
        source_record = {
            "event_id": event_id,
            "resolution": resolution,
            "evidence": frozen_evidence,
            "target": None
            if target_snapshot is None
            else {
                "name": target_snapshot.name,
                "path": str(target_snapshot.path),
                "version": target_snapshot.version,
                "content_hash": target_snapshot.content_hash,
            },
        }
        evidence_path = self.root / "evidence.jsonl"
        try:
            reject_symlinks(evidence_path)
            with self._lock, evidence_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(source_record, ensure_ascii=False) + "\n")
                output.flush()

                os.fsync(output.fileno())
        except (OSError, ValueError) as error:
            return {
                "status": "rejected",
                "event_id": event_id,
                "reason": str(error),
                "failure_kind": "failure_to_apply",
                "task_quality": None,
            }
        proposal = await model_call(frozen_window)
        if not proposal:
            proposal = {"action": "discard"}
        proposal_text = json.dumps(
            {"event_id": event_id, "proposal": proposal, "target": source_record["target"]},
            ensure_ascii=False,
            sort_keys=True,
        )
        proposal_path = (
            self.root / "proposals" / (sha(str(event_id)) + "-" + sha(proposal_text) + ".json")
        )
        atomic(proposal_path, proposal_text)
        if self.session is not None:
            self.session.append(
                "skill_proposal",
                {
                    "event_id": event_id,
                    "proposal_ref": str(proposal_path),
                    "proposal_hash": sha(proposal_text),
                },
                checkpoint=True,
            )
        if not isinstance(proposal, dict):
            return self._reject("_maintenance", event_id, "Invalid model proposal")
        proposal = dict(proposal)
        action = proposal.pop("action", "apply")
        name = proposal.pop("name", None)
        for field in ("event_id", "authorized", "permission_mode", "base_hash", "content_hash",
                      "_prepare_only", "_validation", "_expected_resources"):
            proposal.pop(field, None)
        if action == "discard":
            return self.discard(
                name or "_maintenance", event_id=event_id, reason=proposal.get("reason", "")
            )
        if not isinstance(name, str) or not name:
            return self._reject("_maintenance", event_id, "A skill name is required")
        if resolution is not None and action in ('apply', 'create'):
            if resolution.get('reason') == 'ambiguous' or (
                resolution.get('source') in ('explicit_id', 'used_ids') and resolution.get('reason') != 'resolved'
            ):
                return self._reject(name, event_id, 'Unresolved skill target; abstaining')
            if action == 'create' and resolution.get('candidates'):
                return self._reject(name, event_id, 'Related skill exists; creation requires a new method')
        if action == "apply":
            if target_snapshot is not None and name != target_snapshot.name:
                return self._reject(name, event_id, "Proposal target differs from pinned skill")
            target = target_snapshot if target_snapshot is not None else catalog.get(name)
            if target is None:
                return self._reject(name, event_id, "No program-pinned target")
            proposal["base_hash"] = target.content_hash
            name = target.name
        elif action == "create":
            if name in self.skills.capture_catalog():
                return self._reject(name, event_id, "Create requires a new skill")
            proposal.pop("reason", None)
            proposal.pop("knowledge_note", None)
        else:
            return self._reject(name, event_id, "Unknown maintenance action")
        proposal.update(
            name=name, event_id=event_id, permission_mode=permission_mode, authorized=authorized
        )
        async with self._async_locks.setdefault(name, asyncio.Lock()):
            try:
                if action == "create":
                    if validation_gate is not None:
                        return self._reject(name, event_id, "Gated creation requires a comparison baseline; unsupported")
                    return self.create(**proposal)
                if validation_gate is not None:
                    candidate = self.apply(**proposal, _prepare_only=True, _expected_resources=target.resources)
                    if isinstance(candidate, dict):
                        return candidate
                    try:
                        scores = await validation_gate(target, candidate)
                        old_score, new_score = scores['old'], scores['candidate']
                        if getattr(validation_gate, 'requires_model_review', False) and scores.get('model_approved') is not True:
                            return self._reject(name, event_id, 'Independent model review did not approve candidate')
                        for score in (old_score, new_score):
                            if any(type(score[k]) is not int or score[k] < 0 for k in ('successes', 'hard_failures')):
                                raise ValueError('Invalid validation metrics')
                        if not (new_score['successes'] > old_score['successes'] and
                                new_score['hard_failures'] <= old_score['hard_failures']):
                            return self._reject(name, event_id, 'Validation requires strict improvement without additional hard failures')
                    except BudgetExceeded:
                        raise
                    except Exception as error:
                        return self._reject(name, event_id, 'Validation failed: ' + type(error).__name__)
                    if self.skills.load(name).resources != target.resources:
                        return self._reject(name, event_id, 'Resource drift during validation')
                    return self.apply(**proposal, _validation='validated', _expected_resources=target.resources)
                return self.apply(**proposal)
            except (TypeError, ValueError) as error:
                return self._reject(name, event_id, str(error))

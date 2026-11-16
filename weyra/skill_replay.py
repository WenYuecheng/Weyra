"""有界用户交互回放证据采集与窗口冻结引擎 (Bounded User Replay Evidence Engine)。

【架构定位】(Architectural Role)
    位于 `weyra` 技能演进与质量门禁层，负责从前台用户交互中非侵入式捕获首访问文件前像（Preimages）
    与上下文线索，并在提议技能更新前冻结最多 2 个开发集用例与 2 个独立验证集用例（2+2 有界窗口）。
    本模块不触发任何模型调用，杜绝膨胀的测试用例库，保持极低常数级资源占用。

【开源设计思想与架构血统】(Design Pedigree)
    1. 融合 AutoSkill 与 SkillEvo 的有界回放验证哲学（Bounded Replay Verification）：
       前台真实交互作为开发集证据（Development Split），而宿主清单提供独立的验证集证据（Validation Split），
       原始交互 ID 与用户任务绝不可作为验证集，杜绝过拟合与数据泄漏；
    2. 继承确定性哈希序列化：通过 `_json` 与 `_hash` 保证跨机器与运行时的指纹完全可再现；
    3. 安全沙箱隔离：排查任何 `.weyra` 内部路径、绝对路径或 `.env` 凭据文件，防止敏感数据泄漏至回放环境。

【核心不变性与安全约束】(Invariants & Safety)
    - 严格容量边界：单文件 ≤64KB，快照总上限 ≤128KB，文件数 ≤8，上下文长度 ≤8000 字符；
    - 允许工具白名单收敛：仅对 `REPLAY_TOOLS`（`skill`, `read_file`, `write_file`, `edit_file`）建立文件前像捕获，其余工具视为不支持回放；
    - 不可变窗口快照：`ReplayWindow` 返回独立深拷贝字典，防止并发维护操作就地篡改窗口数据。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath

from weyra.llm.providers import rendered_message_content

MAX_FILE_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 128 * 1024
MAX_FILES = 8
MAX_CONTEXT_CHARS = 8000

# 搜索与列表操作触及整个目录空间，文件首次访问前像无法完整还原该全集。
# 在该环境能被完全保真重现前，保留经验记录但不触发自动升级。
REPLAY_TOOLS = frozenset({"skill", "read_file", "write_file", "edit_file"})


def _json(value) -> str:
    """确定性序列化回放证据标识（排序键与紧凑分隔符）。

    Args:
        value: 待序列化的数据对象。

    Returns:
        str: 确定性 JSON 字符串。
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    """计算确定性 SHA-256 哈希值，消除字典插入顺序对哈希的影响。

    Args:
        value: 待哈希的数据对象。

    Returns:
        str: 64 位十六进制哈希指纹。
    """
    return hashlib.sha256(_json(value).encode()).hexdigest()


class ReplayCapture:
    """在文件工具锁保护下捕获首次访问文件前像的采集器 (Replay Evidence Capture)。

    【架构定位】
    前台轮次执行期间的观察者，静默收集文件初始状态、写入动作与上下文摘要。
    """

    def __init__(self, turn_id: str, messages: list, *, system_instructions: str = ""):
        """初始化回放证据采集器。

        Args:
            turn_id: 所属轮次唯一标识。
            messages: 原始消息流序列。
            system_instructions: 系统静态指令文本。
        """
        self.turn_id = turn_id
        self.files = {}
        self.written = []
        self.reason = ""
        self.size = 0
        self.system_instructions = system_instructions
        self.context = [
            {
                "role": m.role,
                "content": rendered_message_content(m),
                "source": m.source,
                "source_kind": m.source_kind,
            }
            for m in messages
            if m.role in {"user", "assistant"}
            and isinstance(m.content, str)
            and not getattr(m, "tool_calls", ())
        ]
        if (
            len(self.context) > 8
            or sum(len(m["content"]) for m in self.context) > MAX_CONTEXT_CHARS
        ):
            self.reason = "context_exceeds_replay_limit"
            self.context = []
        if len(system_instructions) > 16000:
            self.reason = self.reason or "instructions_exceed_replay_limit"
            self.system_instructions = ""

    def tool(self, name: str) -> None:
        """观察工具调用能力，若不在白名单内则标记不支持回放。

        Args:
            name: 工具名称。
        """
        if name not in REPLAY_TOOLS:
            self.reason = self.reason or "unsupported_tool:" + name

    def file(self, relative: str, raw: bytes | None, *, write: bool = False) -> None:
        """记录文件的初始字节内容或不存在状态，绝不记录后序覆盖输出。

        Args:
            relative: 相对文件路径。
            raw: 初始字节内容（若文件原本不存在则为 None）。
            write: 是否发生了写入动作。
        """
        path = PurePosixPath(relative)
        if ".weyra" in path.parts:
            return
        if path.is_absolute() or ".." in path.parts or path.name.startswith(".env"):
            self.reason = self.reason or "unsupported_input_path"
            return
        if relative not in self.files:
            if len(self.files) >= MAX_FILES or (
                raw is not None
                and (len(raw) > MAX_FILE_BYTES or self.size + len(raw) > MAX_SNAPSHOT_BYTES)
            ):
                self.reason = self.reason or "files_exceed_replay_limit"
                return
            try:
                content = raw.decode("utf-8") if raw is not None else None
            except UnicodeError:
                self.reason = self.reason or "non_text_input"
                return
            self.files[relative] = content
            self.size += len(raw) if raw is not None else 0
        if write and relative not in self.written:
            self.written.append(relative)

    def snapshot(self) -> dict:
        """生成适合持久化存储到 SessionStore 的独立有界快照字典。

        Returns:
            dict: 序列化安全的回放快照字典。
        """
        return json.loads(
            _json({
                "turn_id": self.turn_id,
                "complete": not self.reason,
                "reason": self.reason,
                "files": self.files,
                "written_paths": self.written,
                "context": self.context,
                "system_instructions": self.system_instructions,
            })
        )


@dataclass(frozen=True)
class ReplayWindow:
    """不可变回放评测窗口实体 (Immutable Replay Evaluation Window)。

    【架构定位】
    持有开发集与独立验证集样本用例，调用方仅获取用例深拷贝，绝不暴露内部可变状态。

    Attributes:
        version: 窗口内容的确定性哈希版本指纹。
        payload: 紧凑 JSON 格式的用例集合文本。
        reason: 窗口不可用原因标记（为空表示就绪）。
    """

    version: str
    payload: str
    reason: str = ""

    @property
    def cases(self) -> list[dict]:
        """获取窗口包含的所有测试用例的新生独立列表。"""
        return json.loads(self.payload)

    @property
    def development(self) -> list[dict]:
        """获取开发集证据用例列表（仅用于探索优化，排除在晋升准入计算之外）。"""
        return [case for case in self.cases if case["split"] == "development"]

    @property
    def validation(self) -> list[dict]:
        """获取宿主精选的独立验证集用例列表（决定技能是否能正式发布）。"""
        return [case for case in self.cases if case["split"] == "validation"]


def freeze_window(evidence: dict, manifest_cases: list[dict]) -> ReplayWindow:
    """在提议技能演进前冻结不超过 2 个开发集用例与不超过 2 个独立验证集用例。

    【核心不变性与安全约束】
    1. 原始提问与其交互轮次 ID 严禁充当验证集证据，防止自测过拟合；
    2. 严格去重：相同提问或相同文件集的用例坚决去重；
    3. 严控规模：开发集上限 2 个，验证集上限 2 个（2+2 窗口），杜绝用例库无限膨胀。

    Args:
        evidence: 当前交互收集到的回放证据字典。
        manifest_cases: 宿主清单提供的预置验证用例列表。

    Returns:
        ReplayWindow: 冻结生成的不可变回放评测窗口对象。

    Raises:
        ValueError: 当用例数据集切分类型未知时抛出。
    """
    snapshot = evidence.get("replay")
    reason = ""
    development, validation = [], []
    user = str(evidence.get("user", ""))
    if not isinstance(snapshot, dict):
        reason = "missing_replay_snapshot"
    elif not snapshot.get("complete"):
        reason = snapshot.get("reason") or "incomplete_replay_snapshot"
    elif not user.strip() or len(user) > MAX_CONTEXT_CHARS:
        reason = "invalid_replay_prompt"
    else:
        files = {p: text for p, text in snapshot.get("files", {}).items() if text is not None}
        written = list(snapshot.get("written_paths", []))
        context = snapshot.get("context", [])
        prompt = user
        if context:
            prompt = (
                "Prior conversation for reference:\n"
                + _json(context)
                + "\n\nCurrent user task:\n"
                + user
            )
        development.append({
            "id": "turn-" + str(snapshot.get("turn_id", "unknown")),
            "source_window_id": snapshot.get("turn_id"),
            "prompt": prompt,
            "files": files,
            "split": "development",
            "rubric": str(evidence.get("feedback", "")),
            "system_instructions": snapshot.get("system_instructions", ""),
            "output_mode": "files" if written else "text",
            "expected_artifacts": {},
            "hard_constraints": {
                "protected_input_paths": sorted(set(files) - set(written)),
                "writable_input_paths": sorted(set(files) & set(written)),
                "required_output_paths": written,
                "allowed_output_paths": written,
            },
        })
    original_id = snapshot.get("turn_id") if isinstance(snapshot, dict) else None
    original_files = {
        p: t
        for p, t in (snapshot if isinstance(snapshot, dict) else {}).get("files", {}).items()
        if t is not None
    }
    seen_ids, seen_tasks = set(), set()
    for source in manifest_cases:
        case = json.loads(_json(source))
        identity = _hash({
            "prompt": case.get("prompt", "").strip(),
            "files": case.get("files", {}),
        })
        if (
            case.get("id") in seen_ids
            or identity in seen_tasks
            or (original_id and case.get("source_window_id") == original_id)
            or (
                case.get("prompt", "").strip() == user.strip()
                and case.get("files", {}) == original_files
            )
        ):
            continue
        seen_ids.add(case.get("id"))
        seen_tasks.add(identity)
        split = case.get("split", "validation")
        if split not in {"development", "mutate_dev", "validation", "promotion_test"}:
            raise ValueError("Unknown replay sample split")
        role = "development" if split in {"development", "mutate_dev"} else "validation"
        destination = development if role == "development" else validation
        if len(destination) < 2:
            case["split"] = role
            destination.append(case)
        if len(development) == 2 and len(validation) == 2:
            break
    cases = development + validation
    if not validation:
        reason = reason or "insufficient_independent_validation"
    return ReplayWindow(_hash(cases), _json(cases), reason)

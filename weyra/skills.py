"""多级技能目录发现、渐进式披露与开源标准调用引擎 (Skill Catalog, Progressive Disclosure & Standard Invocation).

【架构定位】
本模块负责 Weyra 智能体在多层级工作区中的技能（`SKILL.md`）发现、不可变快照生成、
辅助静态资源 SHA-256 防篡改校验、标准 `<available_skills>` / `<skill_content>` 渲染以及
用户显式 `/<skill-name>` 手势解析。

【开源框架与学术论文溯源】
1. DeepSeek Harness (`packages/skill/skill`, `packages/skill/skill-filesystem`, `packages/skill/tool-skill`):
   - 多根优先级发现与同层冲突检测（`project` > `.agents` > `user` > `legacy`）；
   - `model_invocable`（`disable-model-invocation`）与 `user_invocable`（`user-invocable`）双面调用策略控制；
   - 标准 `<available_skills>` 目录披露与 `<skill_content>` 正文/资源信封渲染（`renderSkillContent`）；
   - 用户显式 `/<skill-name>` 手势正则解析（`SKILL_GESTURE_RE`, `extract_user_invoked_skills`）。
2. Pi Agent (`packages/coding-agent/src/core/skills.ts`) & Agent Skills Standard (`agentskills.io`):
   - 支持跨工具共享目录 `.agents/skills/`、kebab-case 命名校验与渐进式披露摘要格式。
3. Hermes Agent (`agent/skill_utils.py`, `tools/skills_tool.py`):
   - 支持按分类子目录递归发现 `SKILL.md`，跳过 `references/`、`templates/`、`assets/`、`scripts/`
     辅助资源目录及 `.git`/`.venv`/`node_modules` 等排除目录，并通过 `file_path` 按需读取附属资源。
4. SkillDiff (`LitaoGuo/How-Self-Evolution-Reshapes-Skills`, arXiv:2611.25534, MIT):
   - 调用 `weyra.skilldiff_vendor.items.split_items` 提取正文中由 `**[FOCUS]**` 标注的专家非先验条目
     （`focus_items`），防止后续无外部验证的改写将专家关键条款向模型先验坍缩。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from .frontmatter import parse_markdown, rank, reject_symlinks, sha
from .skilldiff_vendor.items import split_items
from .skilldiff_vendor.skilldiff import BODY_TYPES, MARK

# DeepSeek Harness (`packages/skill/skill/src/index.ts`) 标准命名与手势正则
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILL_GESTURE_RE = re.compile(r"(^|\s)/([a-z0-9]+(?:-[a-z0-9]+)*)(?=\s|$)")

# Hermes Agent (`agent/skill_utils.py`) 渐进式披露附属目录与扫描排除目录
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))
EXCLUDED_SKILL_DIRS = frozenset(
    (
        ".git",
        ".github",
        ".hub",
        ".archive",
        ".locks",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    )
)


def is_skill_name(name: str) -> bool:
    """校验技能标识是否符合 DeepSeek Harness / Agent Skills 标准 kebab-case 命名。"""
    return bool(isinstance(name, str) and SKILL_NAME_RE.match(name))


def _parse_bool_flag(value: object, default: bool = False) -> bool:
    """解析 frontmatter 中的布尔调用策略标记（如 disable-model-invocation / user-invocable）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return default


def _parse_list_field(value: object) -> tuple[str, ...]:
    """解析 frontmatter 中的列表字段（支持 JSON 数组、列表或逗号分隔字符串）。"""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        try:
            parsed = json.loads(stripped)
            value = parsed if isinstance(parsed, (list, tuple)) else re.split(r"[,|，]", stripped)
        except (ValueError, TypeError):
            value = re.split(r"[,|，]", stripped)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def extract_focus_items(raw_markdown: str) -> tuple[str, ...]:
    """利用 SkillDiff (`arXiv:2611.25534`) 原子条目拆分器提取正文中标注 `**[FOCUS]**` 的专家条款。"""
    items = []
    for item in split_items(raw_markdown):
        if item["type"] not in BODY_TYPES:
            continue
        text = item["text"]
        if MARK not in text:
            continue
        cleaned = text.replace(MARK, "").strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned:
            items.append(cleaned)
    return tuple(dict.fromkeys(items))


def escape_xml_text(value: str) -> str:
    """转义嵌入 `<available_skills>` 与 `<skill_content>` 中的文本（对齐 DSH `escapeText`）。"""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_xml_attr(value: str) -> str:
    """转义 XML 属性值（对齐 DSH `escapeAttr`）。"""
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def extract_user_invoked_skills(text: str) -> list[str]:
    """从直接用户输入中提取去重的 `/<skill-name>` 显式调用手势（对齐 DSH `invokedSkillNames`）。"""
    names: list[str] = []
    if not isinstance(text, str) or not text:
        return names
    for match in SKILL_GESTURE_RE.finditer(text):
        skill_name = match.group(2)
        if skill_name and skill_name not in names:
            names.append(skill_name)
    return names


def format_available_skills(skills: Sequence[SkillSnapshot], *, max_desc_chars: int = 500) -> str:
    """将模型可调用技能列表渲染为 DeepSeek Harness / Agent Skills 标准 `<available_skills>` 摘要块。

    遵循渐进式披露原则：仅暴露名称、摘要描述与适用条件，严禁在此注入技能正文，
    引导模型在任务匹配时显式调用 `skill` 工具加载完整指令。
    """
    visible = [s for s in skills if getattr(s, "model_invocable", True)]
    if not visible:
        return ""
    lines = [
        "<system-reminder>",
        "A skill is a reusable set of task-specific instructions. The following skills are available in this session:",
        "",
        "<available_skills>",
    ]
    for skill in visible:
        desc = re.sub(r"\s+", " ", str(getattr(skill, "description", "") or "")).strip()
        if len(desc) > max_desc_chars:
            desc = desc[: max_desc_chars - 3] + "..."
        when = re.sub(r"\s+", " ", str(getattr(skill, "when_to_use", "") or "")).strip()
        suffix = f" (when: {escape_xml_text(when)})" if when else ""
        lines.append(f"- `{skill.name}`: {escape_xml_text(desc)}{suffix} [{skill.version}]")
    lines.extend(
        [
            "</available_skills>",
            "",
            "If the user names a skill, or the task clearly matches a skill's description, call the `skill` tool with the exact skill name before taking task actions.",
            "This catalog contains summaries only; do not infer or follow a skill's instructions until it has been loaded.",
            "</system-reminder>",
        ]
    )
    return "\n".join(lines)


def render_skill_content(snapshot: SkillSnapshot) -> str:
    """渲染标准 `<skill_content>` 信封（对齐 DeepSeek Harness `renderSkillContent` 与 Hermes 渐进资源提示）。"""
    resource_lines = [
        f"Base directory for this skill: {escape_xml_text(str(snapshot.path.parent))}",
    ]
    if snapshot.resources:
        listed = ", ".join(sorted(snapshot.resources))
        resource_lines.append(f"Bundled resource files: {escape_xml_text(listed)}")
        resource_lines.append(
            "Load referenced resources only as needed via `skill(name=..., file_path=...)` or `skill_resource`."
        )
    else:
        resource_lines.append("Load referenced resources only as needed.")
    return "\n".join(
        [
            f'<skill_content name="{escape_xml_attr(snapshot.name)}">',
            "<skill_resources>",
            *resource_lines,
            "</skill_resources>",
            "",
            "<skill_instructions>",
            snapshot.body,
            "</skill_instructions>",
            "</skill_content>",
        ]
    )


def _is_excluded_skill_path(skill_md: Path, root: Path) -> bool:
    """判断 `SKILL.md` 是否位于排除目录或父技能的附属资源子目录内（对齐 Hermes `is_excluded_skill_path`）。"""
    try:
        rel_parts = skill_md.relative_to(root).parts
    except ValueError:
        rel_parts = skill_md.parts
    if any(part in EXCLUDED_SKILL_DIRS for part in rel_parts):
        return True
    for idx in range(1, len(rel_parts) - 1):
        if rel_parts[idx] in SKILL_SUPPORT_DIRS and (root.joinpath(*rel_parts[:idx], "SKILL.md")).is_file():
            return True
    return False


@dataclass(frozen=True)
class SkillSnapshot:
    """不可变技能快照，包含正文、调用策略、`[FOCUS]` 专家条目及附属资源 SHA-256 指纹。"""

    name: str
    path: Path
    version: str
    description: str
    when_to_use: str
    body: str
    content_hash: str
    resources: Mapping[str, str]
    context: str = ""
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    model_invocable: bool = True
    user_invocable: bool = True
    focus_items: tuple[str, ...] = ()


class SkillStore:
    """多层级技能目录发现与资源安全校验仓库 (Skill Store)。"""

    def __init__(self, root, skill_dirs=None):
        """初始化技能搜索根目录（优先级对齐 DeepSeek Harness + Pi `.agents/skills` + Hermes）。

        Args:
            root: 工程根目录。
            skill_dirs: 可选的额外技能搜索目录列表。
        """
        self.root = Path(root).resolve()
        self.project_skills = self.root / ".weyra" / "skills"
        self.skill_dirs = [self.project_skills, self.root / ".agents" / "skills"]
        if skill_dirs is not None:
            self.skill_dirs += [Path(x).resolve() for x in skill_dirs]
        else:
            self.skill_dirs += [
                Path.home() / ".weyra" / "skills",
                Path.home() / ".agents" / "skills",
            ]
        # 向下兼容读取历史 `<root>/skills/` 目录
        self.skill_dirs.append(self.root / "skills")

    def _snapshot(self, p: Path) -> SkillSnapshot:
        """从指定的 `SKILL.md` 解析生成不可变技能快照对象。"""
        reject_symlinks(p)
        raw = p.read_text(encoding="utf-8")
        m, body = parse_markdown(raw)
        resources = {
            f.relative_to(p.parent).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(p.parent.rglob("*"))
            if f.is_file() and f != p and not f.is_symlink()
        }
        disable_model = _parse_bool_flag(m.get("disable-model-invocation", m.get("disable_model_invocation")), False)
        user_invocable = _parse_bool_flag(m.get("user-invocable", m.get("user_invocable")), True)
        return SkillSnapshot(
            str(m.get("name", p.parent.name)),
            p.resolve(),
            str(m.get("version", "1.0.0")),
            str(m.get("description", "")),
            str(m.get("when_to_use", m.get("when-to-use", ""))),
            body,
            sha(raw),
            MappingProxyType(resources),
            str(m.get("context", "")),
            MappingProxyType(m),
            model_invocable=not disable_model,
            user_invocable=user_invocable,
            focus_items=extract_focus_items(raw),
        )

    def capture_catalog(self):
        """按根目录优先级递归扫描所有 `SKILL.md`，返回只读不可变技能目录。

        约束（对齐 DSH / Hermes）:
            1. 跳过排除目录（`.git`/`.venv`/`node_modules`）及父技能附属目录（`references/` 等）；
            2. 同一搜索根目录内若出现同名技能冲突立即抛出 `ValueError`；
            3. 跨根目录去重采用高优先级先入为主（`setdefault`）；
            4. 返回 `MappingProxyType` 防止外部篡改。
        """
        result = {}
        seen_dirs: set[Path] = set()
        for directory in self.skill_dirs:
            resolved_dir = directory.resolve()
            if resolved_dir in seen_dirs or not directory.exists():
                continue
            seen_dirs.add(resolved_dir)
            local_names: set[str] = set()
            for p in sorted(directory.rglob("SKILL.md")):
                if _is_excluded_skill_path(p, directory):
                    continue
                snapshot = self._snapshot(p)
                if snapshot.name in local_names:
                    raise ValueError("Duplicate skill name")
                local_names.add(snapshot.name)
                result.setdefault(snapshot.name, snapshot)
        return MappingProxyType(result)

    def load(self, name: str, catalog=None) -> SkillSnapshot:
        """按名称从目录精准加载技能快照。"""
        return (catalog if catalog is not None else self.capture_catalog())[name]

    def search(self, query: str, limit: int = 5, catalog=None, *, include_hidden: bool = False) -> list[SkillSnapshot]:
        """对可用技能执行 BM25 检索（元数据优先匹配，不足 `limit` 时补充匹配正文）。

        默认过滤 `disable-model-invocation: true`（`model_invocable=False`）的技能。
        """
        catalog = catalog if catalog is not None else self.capture_catalog()
        pool = {
            k: s for k, s in catalog.items()
            if include_hidden or getattr(s, "model_invocable", True)
        }
        if limit <= 0 or not pool:
            return []

        def _meta_text(skill: SkillSnapshot) -> str:
            aliases = " ".join(_parse_list_field(skill.metadata.get("aliases", ())))
            return " ".join(
                part for part in (skill.name, aliases, skill.when_to_use, skill.description) if part
            )

        selected = rank(query, [(k, _meta_text(s)) for k, s in pool.items()], limit)
        if len(selected) < limit:
            seen = set(selected)
            fallback = rank(
                query,
                [(k, _meta_text(s) + " " + s.body) for k, s in pool.items() if k not in seen],
                limit - len(selected),
            )
            selected.extend(fallback)
        return [pool[k] for k in selected]

    def read_resource(self, snapshot: SkillSnapshot, relative_path: str) -> str:
        """安全读取并校验技能快照绑定的附属静态资源（符号链接防御 + 沙箱边界 + SHA-256 防漂移）。"""
        reject_symlinks(snapshot.path.parent / relative_path)
        p = (snapshot.path.parent / relative_path).resolve()
        if not p.is_relative_to(snapshot.path.parent.resolve()) or relative_path not in snapshot.resources:
            raise ValueError("Resource outside snapshot")
        raw = p.read_bytes()
        if hashlib.sha256(raw).hexdigest() != snapshot.resources[relative_path]:
            raise ValueError("Resource drift detected")
        return raw.decode("utf-8")

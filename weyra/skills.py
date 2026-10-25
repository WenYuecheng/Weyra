"""多级技能目录管理、安全资源绑定与歧义安全熔断引擎 (Skill Catalog & Ambiguity-Safe Resolution Engine)。

【架构定位】
本模块负责 Weyra 智能体在工作区内技能（Skills）的发现、不可变快照生成、辅助静态资源哈希校验
以及多层级候选技能目标决议。通过引入分级决议流水线与歧义主动弃权（Ambiguity Rejection）机制，
确保在多候选场景下不发生误加载或越权修改。

【开源与工程设计借鉴】
1. 借鉴 Hermes Agent (`skills/`) 的分层技能目录发现架构：
   - 项目专属技能目录（`.weyra/skills/`）优先于用户全局目录（`~/.weyra/skills/`）及历史路径；
   - 目录内包含标准 `SKILL.md`（YAML frontmatter 元数据 + 可执行 Markdown 正文）与关联静态资源；
2. 四级目标决议与歧义安全弃权机制 (4-Tier Resolution & Safe Abstention)：
   - Level 1: 显式 ID 准确定位 (`explicit_id`)；
   - Level 2: 轮次成功使用记录定位 (`used_ids`)；
   - Level 3: 精确全称或中英文别名匹配 (`exact_alias`, 支持中文字符正则分词)；
   - Level 4: 语义/词法相关度排序检索 (`lexical`)；
   - 歧义熔断守则：当匹配到的候选目标多于 1 个时，系统主动返回 `reason='ambiguous', target=None` 弃权执行，
     防止将针对特定技能的改进补丁错误应用到无关技能上；
3. 静态资源加密哈希锁定 (Cryptographic Resource Binding)：
   - 技能依赖的外部辅助脚本或配置文件通过 SHA-256 强哈希固化在快照中，运行时若检测到磁盘文件篡改（Resource Drift）立即拦截。
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from .frontmatter import parse_markdown, rank, reject_symlinks, sha


@dataclass(frozen=True)
class SkillSnapshot:
    """不可变技能快照，包含正文、元数据及资源 SHA-256 指纹。"""
    name: str
    path: Path
    version: str
    description: str
    when_to_use: str
    body: str
    content_hash: str
    resources: Mapping[str, str]
    context: str = ""
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class SkillResolution:
    """技能目标决议结果，记录匹配到的唯一目标或多个候选歧义集合。"""
    target: SkillSnapshot | None
    candidates: tuple[SkillSnapshot, ...]
    reason: str
    source: str


class SkillStore:
    """项目与用户级技能目录管理仓库 (Skill Store)。"""

    def __init__(self, root, skill_dirs=None):
        """初始化多层级技能目录存储管理器。

        Args:
            root: 工程根目录。
            skill_dirs: 可选的额外技能搜索目录列表（默认包含项目级与用户级目录）。
        """
        self.root = Path(root).resolve()
        self.project_skills = self.root / '.weyra' / 'skills'
        self.skill_dirs = [self.project_skills]
        self.skill_dirs += [Path(x).resolve() for x in skill_dirs] if skill_dirs is not None else [Path.home() / '.weyra' / 'skills']
        # Read old Weyra locations without moving or overwriting user files.
        self.skill_dirs.append(self.root / 'skills')

    def _snapshot(self, p):
        """从指定的 SKILL.md 路径解析生成不可变的技能快照对象。

        Args:
            p: 目标 SKILL.md 文件路径。

        Returns:
            SkillSnapshot: 包含元数据、正文、资源哈希的不可变快照。
        """
        reject_symlinks(p)
        raw = p.read_text(encoding="utf-8")
        m, body = parse_markdown(raw)
        resources = {
            str(f.relative_to(p.parent)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(p.parent.rglob("*"))
            if f.is_file() and f != p and not f.is_symlink()
        }
        return SkillSnapshot(
            m.get("name", p.parent.name),
            p.resolve(),
            m.get("version", "1.0.0"),
            m.get("description", ""),
            m.get("when_to_use", m.get("when-to-use", "")),
            body,
            sha(raw),
            MappingProxyType(resources),
            m.get("context", ""),
            MappingProxyType(m),
        )

    def capture_catalog(self):
        """Return immutable snapshots, rejecting ambiguous duplicate skill names."""
        result = {}
        for directory in self.skill_dirs:
            local_names = set()
            for p in sorted(directory.glob("*/SKILL.md")):
                snapshot = self._snapshot(p)
                if snapshot.name in local_names:
                    raise ValueError("Duplicate skill name")
                local_names.add(snapshot.name)
                result.setdefault(snapshot.name, snapshot)
        return MappingProxyType(result)

    def load(self, name, catalog=None):
        """从技能目录中按名称精准加载技能快照。

        Args:
            name: 技能名称。
            catalog: 可选的已缓存目录字典。

        Returns:
            SkillSnapshot: 技能快照对象。

        Raises:
            KeyError: 技能不存在。
        """
        return (catalog if catalog is not None else self.capture_catalog())[name]

    def search(self, query, limit=5, catalog=None):
        """基于词法相关度从技能目录中搜索候选技能。

        Args:
            query: 查询字符串。
            limit: 最大返回结果数。
            catalog: 可选的预取目录字典。

        Returns:
            list[SkillSnapshot]: 匹配的技能快照列表。
        """
        catalog = catalog if catalog is not None else self.capture_catalog()
        return [
            catalog[k]
            for k in rank(
                query,
                [
                    (k, s.name + " " + s.description + " " + s.when_to_use + " " + s.body)
                    for k, s in catalog.items()
                ],
                limit,
            )
        ]

    def resolve_target(self, query: str, explicit_id=None, used_ids=(), catalog=None) -> SkillResolution:
        """执行四级级联技能目标决议，多候选歧义时主动安全弃权 (Cascaded Target Resolution)。

        【决议优先级序列】
        1. explicit_id：调用方显式指定的技能标识；
        2. used_ids：关联交互轮次中实际成功使用的技能集合；
        3. exact_alias：按全称或元数据别名（aliases）精确比对；
        4. lexical：按语义与词法相关度从目录中召回候选。

        【安全约束】
        若匹配到多于 1 个候选技能，决议器强制返回 target=None 与 reason='ambiguous'，
        杜绝在目标不确定时发生误匹配或错误演化。
        """
        catalog = catalog if catalog is not None else self.capture_catalog()
        def result(items, source):
            """组装统一的技能决议结果对象。"""
            items = tuple(items)
            return SkillResolution(items[0] if len(items) == 1 else None, items,
                                   'resolved' if len(items) == 1 else 'ambiguous' if items else 'not_found', source)
        if explicit_id is not None:
            return result([catalog[explicit_id]] if explicit_id in catalog else [], 'explicit_id')
        used = tuple(dict.fromkeys(used_ids))
        if used:
            return result([catalog[k] for k in used if k in catalog], 'used_ids')
        text = query.casefold().strip()
        exact = []
        for skill in catalog.values():
            aliases = skill.metadata.get('aliases', ())
            if isinstance(aliases, str):
                # The lightweight frontmatter reader preserves unquoted JSON lists.
                try:
                    import json
                    aliases = json.loads(aliases)
                except (ValueError, TypeError):
                    aliases = re.split(r'[,|，]', aliases)
            if not isinstance(aliases, (list, tuple)):
                aliases = ()
            names = [skill.name, *aliases]
            if any(isinstance(n, str) and n.strip() and (
                text == n.casefold().strip() or (re.search(r'[\u3400-\u9fff]', n) and n.casefold().strip() in text) or re.search(r'(?<![\w-])' + re.escape(n.casefold().strip()) + r'(?![\w-])', text)
            ) for n in names):
                exact.append(skill)
        if exact:
            return result(exact, 'exact_alias')
        return result(self.search(query, limit=len(catalog), catalog=catalog), 'lexical')

    def read_resource(self, snapshot, relative_path):
        """Read a pinned resource only if its location and bytes still match."""
        reject_symlinks(snapshot.path.parent / relative_path)
        p = (snapshot.path.parent / relative_path).resolve()
        if (
            not p.is_relative_to(snapshot.path.parent.resolve())
            or relative_path not in snapshot.resources
        ):
            raise ValueError("Resource outside snapshot")
        raw = p.read_bytes()
        if hashlib.sha256(raw).hexdigest() != snapshot.resources[relative_path]:
            raise ValueError("Resource drift detected")
        return raw.decode("utf-8")

"""SkillDiff 专家非先验知识原位标注开源复用包 (Vendored MIT Modules from `How-Self-Evolution-Reshapes-Skills`).

【架构定位】
本子包原样移植自开源研究项目 `LitaoGuo/How-Self-Evolution-Reshapes-Skills`（MIT License，
commit `db2cdbde29e752ebc9cc81817f17400b64056c40`），用于解决多轮技能演进中模型不断向自身先验坍缩、
悄然删掉原始专家非先验知识的问题。

【开源框架与学术论文溯源】
1. 论文: *How Feedback-Free Self-Evolution Reshapes Agent Skills: A Systematic Study*
   (arXiv:2611.25534, Nov 2026)
2. 仓库: https://github.com/LitaoGuo/How-Self-Evolution-Reshapes-Skills (MIT License)
3. 复用模块映射:
   - `items.py`     <- `experiments/items.py` (`split_document`, `split_items`)
   - `skilldiff.py` <- `experiments/skilldiff.py` (`MARK = "**[FOCUS]**"`, `NOTE`, `BODY_TYPES`, `mark`)
"""

from .items import split_document, split_items
from .skilldiff import BODY_TYPES, MARK, NOTE, mark

__all__ = [
    "BODY_TYPES",
    "MARK",
    "NOTE",
    "mark",
    "split_document",
    "split_items",
]

"""Microsoft SkillOpt 核心演进引擎开源复用包 (Vendored MIT Modules from `microsoft/SkillOpt`).

【架构定位】
本子包原样移植自 Microsoft Research 开源项目 `microsoft/SkillOpt`（MIT License，
commit `dc0b5e654cafa1073a2819086151c1e0d13012f0`），为 Weyra 提供经过顶会实证检验、
零额外外部依赖的文本空间技能优化原语与门控核心。

【开源框架与学术论文溯源】
1. 论文: *SkillOpt: Executive Strategy for Self-Evolving Agent Skills*
   (NeurIPS 2026 Spotlight, arXiv:2605.23904)
2. 仓库: https://github.com/microsoft/SkillOpt (MIT License)
3. 复用模块映射:
   - `types.py`     <- `skillopt/types.py` (`Edit`, `Patch`, `RolloutResult`) & `skillopt_sleep/types.py` (`EditRecord`)
   - `skill.py`     <- `skillopt/optimizer/skill.py` (`apply_edit`, `apply_patch`, `apply_patch_with_report`, 保护区间)
   - `scheduler.py` <- `skillopt/optimizer/scheduler.py` (`Constant/Linear/Cosine/AutonomousScheduler`, `build_scheduler`)
   - `gate.py`      <- `skillopt/evaluation/gate.py` (`GateResult`, `select_gate_score`, `evaluate_gate`)
   - `memory.py`    <- `skillopt_sleep/memory.py` (`LEARNED_START/END`, `apply_edits_detailed`, `ensure_skill_scaffold`)
"""

from .gate import GateAction, GateMetric, GateResult, compute_semantic_density, evaluate_gate, select_gate_score
from .memory import (
    LEARNED_END,
    LEARNED_START,
    apply_edits,
    apply_edits_detailed,
    current_learned_lines,
    ensure_skill_scaffold,
    extract_learned,
    set_learned,
)
from .scheduler import (
    AutonomousScheduler,
    ConstantScheduler,
    CosineScheduler,
    LinearScheduler,
    LRScheduler,
    build_scheduler,
)
from .skill import (
    APPENDIX_END,
    APPENDIX_START,
    SLOW_UPDATE_END,
    SLOW_UPDATE_START,
    apply_edit,
    apply_patch,
    apply_patch_with_report,
)
from .types import Edit, EditOp, EditRecord, Patch, RolloutResult

__all__ = [
    "APPENDIX_END",
    "APPENDIX_START",
    "AutonomousScheduler",
    "ConstantScheduler",
    "CosineScheduler",
    "Edit",
    "EditOp",
    "EditRecord",
    "GateAction",
    "GateMetric",
    "GateResult",
    "LEARNED_END",
    "LEARNED_START",
    "LRScheduler",
    "LinearScheduler",
    "Patch",
    "RolloutResult",
    "SLOW_UPDATE_END",
    "SLOW_UPDATE_START",
    "apply_edit",
    "apply_edits",
    "apply_edits_detailed",
    "apply_patch",
    "apply_patch_with_report",
    "build_scheduler",
    "compute_semantic_density",
    "current_learned_lines",
    "ensure_skill_scaffold",
    "evaluate_gate",
    "extract_learned",
    "select_gate_score",
    "set_learned",
]

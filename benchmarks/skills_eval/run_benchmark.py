"""DeepSeek 官方实网多域技能基准与在线有界演进评测 (18 题 × 3 组配对消融 + 实时演进实验)。

【评测设计与论文/框架依据】
1. SkillsBench (arXiv:2602.12670) & SOPBench (EMNLP 2026):
   - 构建 3 个企业后端运维与交付合规域（Audit Ledger / Incident Triage / Release Manifest）共 18 项确定性任务；
   - 对比 `No-Skill (1x)`、`Matched-Budget Retry (2x)` 与 `Weyra Skill (1x)`，采用零 LLM Judge 调用的确定性校验判分。
2. DeepSeek Harness + Pi (`agentskills.io`) + Hermes Agent 技能标准接口:
   - 在 `Weyra Skill (1x)` 中通过 `<available_skills>` 摘要索引并发暴露 3 项业务技能 + 1 项干扰技能 `prose-style-guide`，且 User Prompt 不提及技能名；
   - 测试模型基于渐进式披露自动选择 `skill` 工具与盲选准确率（零干扰误触发）。
3. Microsoft SkillOpt (NeurIPS 2026 Spotlight, arXiv:2605.23904) + SkillDiff (arXiv:2611.25534):
   - 实测 3 项实时演进协议：(a) SkillDiff `**[FOCUS]**` 专家条款保护校验与 `rejected_edits.jsonl` 持久化；
     (b) 2 个独立验证样本驱动的 `SkillOpt` 有界补丁晋升（`1.0.0 -> 1.0.1`，确定性门禁 `0` Judge 调用）；
     (c) `SkillOpt` `__null_baseline__` 新建技能增益门禁（`SkillValidationGate.supports_null_baseline=True`）。
4. 客观轨迹与技能采纳统计 (`weyra/trace_audit.py`):
   - 对全部 54 次任务产出的 SQLite `state.db` 调用只读 `?mode=ro` `audit_session_db`，统计洁净完成率、错误循环与技能采纳漏斗。

运行方式:
    .venv/bin/python benchmarks/skills_eval/run_benchmark.py
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import math
from pathlib import Path
import random
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weyra.agent import Agent  # noqa: E402
from weyra.config import load_config  # noqa: E402
from weyra.contracts import Message, PreparedRequest, canonical  # noqa: E402
from weyra.core.state import Budget, BudgetExceeded, RunContext  # noqa: E402
from weyra.online_skill_evolution import MAINTENANCE_INSTRUCTION, asdict_snapshot  # noqa: E402
from weyra.session import SessionStore  # noqa: E402
from weyra.skill_evolution import EvolutionStore  # noqa: E402
from weyra.skill_judge import SkillValidationGate  # noqa: E402
from weyra.skills import SkillStore  # noqa: E402
from weyra.trace_audit import audit_session_db  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent

SKILLS_SPEC = {
    "audit-ledger-sop": {
        "description": "Audit ledger JSON extraction protocol from workspace records",
        "when_to_use": "When inspecting records/ files to produce audit_report.json",
        "focus": [
            "Preserve all input files in records/ untouched; write only audit_report.json.",
            "Include top-level keys schema_version, status, summary, and items.",
        ],
        "body": """## Protected Rules
- **[FOCUS]** Preserve all input files in records/ untouched; write only audit_report.json.
- **[FOCUS]** Include top-level keys schema_version, status, summary, and items.

## Procedure
1. Read every file inside `records/` using `read_file`.
2. Exclude records where `status=ARCHIVED` or `status=IGNORED`.
3. Keep only `status=ACTIVE` or `status=CRITICAL`, sorted ascending by record ID (`source`).
4. Set `schema_version` to `"sop-v2"`, `status` to `"ok"`, `summary` to `"kept=<N>"`, and each element of `items` to `{"source": "<exact ID>", "severity": "<lowercase severity>"}`.
5. Write `audit_report.json` with `write_file` and re-read it to verify exact JSON formatting.
""",
    },
    "incident-triage-sop": {
        "description": "Incident alert triage and team escalation routing SOP",
        "when_to_use": "When triaging alerts/ logs into triage_plan.json escalation queues",
        "focus": [
            "Preserve all input files in alerts/ untouched; write only triage_plan.json.",
            "Never include RESOLVED status or P3/INFO alerts in escalations.",
        ],
        "body": """## Protected Rules
- **[FOCUS]** Preserve all input files in alerts/ untouched; write only triage_plan.json.
- **[FOCUS]** Never include RESOLVED status or P3/INFO alerts in escalations.

## Procedure
1. Read every file inside `alerts/` using `read_file`.
2. Filter out any alert where `status=RESOLVED` or `priority` is `P3` or `INFO`.
3. Keep only `P0`, `P1`, or `P2` non-resolved alerts. Map `action`: `P0` or `P1` -> `"immediate"`, `P2` -> `"queued"`.
4. Map `owner_team` by service prefix: `auth-*` -> `"security"`, `pay-*` -> `"billing"`, `api-*` -> `"platform"`.
5. Sort `escalations` by priority severity (`P0` before `P1` before `P2`), then `incident_id` ascending.
6. Set `protocol` to `"triage-v3"`, `escalated_count` to the integer count of kept escalations, and each item to `{"incident_id": "<ID>", "priority": "<P0|P1|P2>", "action": "<immediate|queued>", "owner_team": "<security|billing|platform>"}`.
7. Write `triage_plan.json` with `write_file` and re-read it to verify.
""",
    },
    "release-manifest-sop": {
        "description": "Component release readiness verification and manifest builder",
        "when_to_use": "When validating components/ checks to generate release_manifest.json",
        "focus": [
            "Preserve all input files in components/ untouched; write only release_manifest.json.",
            "Set release_ready to true only when blocked is empty and approved is non-empty.",
        ],
        "body": """## Protected Rules
- **[FOCUS]** Preserve all input files in components/ untouched; write only release_manifest.json.
- **[FOCUS]** Set release_ready to true only when blocked is empty and approved is non-empty.

## Procedure
1. Read every file inside `components/` using `read_file`.
2. For each component entry `<package> | version=<ver> | risk=<RISK> | gate=<GATE>`:
   - If `gate=PASS`, append `{"package": "<package>", "version": "<ver>", "risk": "<lowercase risk>"}` to `approved`.
   - If `gate=FAIL` or `gate=HOLD`, append `"<package>"` to `blocked`.
3. Sort `approved` ascending by `package` and `blocked` ascending alphabetically.
4. Set `manifest_version` to `"rel-v1"`, `release_ready` to `true` iff `len(blocked) == 0 and len(approved) > 0` (else `false`).
5. Write `release_manifest.json` with `write_file` and re-read it to verify.
""",
    },
    "prose-style-guide": {
        "description": "Editorial prose style and release newsletter writing helper",
        "when_to_use": "When drafting release blog announcements, customer emails, or Markdown essays",
        "focus": ["Keep customer-facing prose concise and free of internal trace IDs."],
        "body": "## Writing Guidelines\n- **[FOCUS]** Keep customer-facing prose concise and free of internal trace IDs.\nUse active voice and clear headings for customer announcements.\n",
    },
}


TASKS = [
    # --- Domain 1: audit-ledger-sop (6 tasks) ---
    {
        "id": "audit-1-basic-filter",
        "domain": "audit-ledger",
        "target_skill": "audit-ledger-sop",
        "output_file": "audit_report.json",
        "files": {"records/a.txt": "ID-101 | status=ACTIVE | severity=HIGH\nID-102 | status=ARCHIVED | severity=LOW\n"},
        "prompt": "Read `records/a.txt` and generate `audit_report.json`. Leave all files in `records/` unchanged.",
        "expected": {"schema_version": "sop-v2", "status": "ok", "summary": "kept=1", "items": [{"source": "ID-101", "severity": "high"}]},
    },
    {
        "id": "audit-2-multi-file-sort",
        "domain": "audit-ledger",
        "target_skill": "audit-ledger-sop",
        "output_file": "audit_report.json",
        "files": {
            "records/b1.txt": "ID-205 | status=CRITICAL | severity=CRITICAL\n",
            "records/b2.txt": "ID-201 | status=ACTIVE | severity=MEDIUM\nID-203 | status=IGNORED | severity=HIGH\n",
        },
        "prompt": "Read `records/b1.txt` and `records/b2.txt`, then write `audit_report.json` without modifying `records/`.",
        "expected": {
            "schema_version": "sop-v2",
            "status": "ok",
            "summary": "kept=2",
            "items": [{"source": "ID-201", "severity": "medium"}, {"source": "ID-205", "severity": "critical"}],
        },
    },
    {
        "id": "audit-3-all-excluded",
        "domain": "audit-ledger",
        "target_skill": "audit-ledger-sop",
        "output_file": "audit_report.json",
        "files": {"records/c.txt": "ID-301 | status=ARCHIVED | severity=LOW\nID-302 | status=IGNORED | severity=MEDIUM\n"},
        "prompt": "Inspect `records/c.txt` and produce `audit_report.json`. Keep `records/c.txt` intact.",
        "expected": {"schema_version": "sop-v2", "status": "ok", "summary": "kept=0", "items": []},
    },
    {
        "id": "audit-4-critical-priority",
        "domain": "audit-ledger",
        "target_skill": "audit-ledger-sop",
        "output_file": "audit_report.json",
        "files": {"records/d.txt": "ID-409 | status=CRITICAL | severity=HIGH\nID-402 | status=ACTIVE | severity=LOW\nID-405 | status=ARCHIVED | severity=CRITICAL\n"},
        "prompt": "Process `records/d.txt` into `audit_report.json` and do not alter `records/d.txt`.",
        "expected": {
            "schema_version": "sop-v2",
            "status": "ok",
            "summary": "kept=2",
            "items": [{"source": "ID-402", "severity": "low"}, {"source": "ID-409", "severity": "high"}],
        },
    },
    {
        "id": "audit-5-three-shard-merge",
        "domain": "audit-ledger",
        "target_skill": "audit-ledger-sop",
        "output_file": "audit_report.json",
        "files": {
            "records/e1.txt": "ID-503 | status=ACTIVE | severity=MEDIUM\n",
            "records/e2.txt": "ID-501 | status=CRITICAL | severity=HIGH\n",
            "records/e3.txt": "ID-502 | status=IGNORED | severity=LOW\n",
        },
        "prompt": "Read `records/e1.txt`, `records/e2.txt`, and `records/e3.txt`, then output `audit_report.json`.",
        "expected": {
            "schema_version": "sop-v2",
            "status": "ok",
            "summary": "kept=2",
            "items": [{"source": "ID-501", "severity": "high"}, {"source": "ID-503", "severity": "medium"}],
        },
    },
    {
        "id": "audit-6-checksum-guard",
        "domain": "audit-ledger",
        "target_skill": "audit-ledger-sop",
        "output_file": "audit_report.json",
        "files": {
            "records/f.txt": "ID-604 | status=ACTIVE | severity=CRITICAL\nID-601 | status=ACTIVE | severity=LOW\n",
            "records/checksum.txt": "PROTECTED_DO_NOT_TOUCH",
        },
        "prompt": "Read `records/f.txt` and `records/checksum.txt`, and write `audit_report.json` without editing `records/`.",
        "expected": {
            "schema_version": "sop-v2",
            "status": "ok",
            "summary": "kept=2",
            "items": [{"source": "ID-601", "severity": "low"}, {"source": "ID-604", "severity": "critical"}],
        },
    },
    # --- Domain 2: incident-triage-sop (6 tasks) ---
    {
        "id": "triage-1-single-p0",
        "domain": "incident-triage",
        "target_skill": "incident-triage-sop",
        "output_file": "triage_plan.json",
        "files": {"alerts/live.log": "INC-101 | service=auth-gateway | priority=P0 | status=OPEN\nINC-102 | service=api-edge | priority=INFO | status=OPEN\n"},
        "prompt": "Triage `alerts/live.log` and write `triage_plan.json`. Keep `alerts/live.log` untouched.",
        "expected": {
            "protocol": "triage-v3",
            "escalated_count": 1,
            "escalations": [{"incident_id": "INC-101", "priority": "P0", "action": "immediate", "owner_team": "security"}],
        },
    },
    {
        "id": "triage-2-team-routing",
        "domain": "incident-triage",
        "target_skill": "incident-triage-sop",
        "output_file": "triage_plan.json",
        "files": {"alerts/queue.log": "INC-204 | service=api-router | priority=P2 | status=OPEN\nINC-201 | service=pay-ledger | priority=P1 | status=ACTIVE\n"},
        "prompt": "Read `alerts/queue.log` and produce `triage_plan.json` without modifying `alerts/queue.log`.",
        "expected": {
            "protocol": "triage-v3",
            "escalated_count": 2,
            "escalations": [
                {"incident_id": "INC-201", "priority": "P1", "action": "immediate", "owner_team": "billing"},
                {"incident_id": "INC-204", "priority": "P2", "action": "queued", "owner_team": "platform"},
            ],
        },
    },
    {
        "id": "triage-3-resolved-filtered",
        "domain": "incident-triage",
        "target_skill": "incident-triage-sop",
        "output_file": "triage_plan.json",
        "files": {"alerts/a.log": "INC-301 | service=auth-sso | priority=P0 | status=RESOLVED\nINC-302 | service=pay-core | priority=P3 | status=OPEN\n"},
        "prompt": "Inspect `alerts/a.log` and write `triage_plan.json` leaving `alerts/` unchanged.",
        "expected": {"protocol": "triage-v3", "escalated_count": 0, "escalations": []},
    },
    {
        "id": "triage-4-tiebreak-sort",
        "domain": "incident-triage",
        "target_skill": "incident-triage-sop",
        "output_file": "triage_plan.json",
        "files": {
            "alerts/s1.log": "INC-408 | service=pay-checkout | priority=P1 | status=OPEN\nINC-402 | service=auth-token | priority=P1 | status=OPEN\n",
            "alerts/s2.log": "INC-401 | service=api-search | priority=P2 | status=OPEN\n",
        },
        "prompt": "Process `alerts/s1.log` and `alerts/s2.log` into `triage_plan.json` without changing `alerts/`.",
        "expected": {
            "protocol": "triage-v3",
            "escalated_count": 3,
            "escalations": [
                {"incident_id": "INC-402", "priority": "P1", "action": "immediate", "owner_team": "security"},
                {"incident_id": "INC-408", "priority": "P1", "action": "immediate", "owner_team": "billing"},
                {"incident_id": "INC-401", "priority": "P2", "action": "queued", "owner_team": "platform"},
            ],
        },
    },
    {
        "id": "triage-5-mixed-status",
        "domain": "incident-triage",
        "target_skill": "incident-triage-sop",
        "output_file": "triage_plan.json",
        "files": {
            "alerts/m.log": "INC-505 | service=api-metrics | priority=P0 | status=RESOLVED\nINC-503 | service=pay-fx | priority=P2 | status=OPEN\nINC-501 | service=auth-mfa | priority=P0 | status=OPEN\n",
        },
        "prompt": "Evaluate `alerts/m.log` and generate `triage_plan.json`. Preserve `alerts/m.log`.",
        "expected": {
            "protocol": "triage-v3",
            "escalated_count": 2,
            "escalations": [
                {"incident_id": "INC-501", "priority": "P0", "action": "immediate", "owner_team": "security"},
                {"incident_id": "INC-503", "priority": "P2", "action": "queued", "owner_team": "billing"},
            ],
        },
    },
    {
        "id": "triage-6-multi-shard-guard",
        "domain": "incident-triage",
        "target_skill": "incident-triage-sop",
        "output_file": "triage_plan.json",
        "files": {
            "alerts/shard_a.log": "INC-602 | service=api-ingress | priority=P1 | status=OPEN\n",
            "alerts/shard_b.log": "INC-601 | service=auth-policy | priority=P2 | status=OPEN\nINC-603 | service=pay-tax | priority=P3 | status=OPEN\n",
            "alerts/freeze.flag": "IMMUTABLE_ALERT_ANCHOR",
        },
        "prompt": "Read `alerts/shard_a.log`, `alerts/shard_b.log`, and `alerts/freeze.flag`, then generate `triage_plan.json`.",
        "expected": {
            "protocol": "triage-v3",
            "escalated_count": 2,
            "escalations": [
                {"incident_id": "INC-602", "priority": "P1", "action": "immediate", "owner_team": "platform"},
                {"incident_id": "INC-601", "priority": "P2", "action": "queued", "owner_team": "security"},
            ],
        },
    },
    # --- Domain 3: release-manifest-sop (6 tasks) ---
    {
        "id": "release-1-all-pass",
        "domain": "release-manifest",
        "target_skill": "release-manifest-sop",
        "output_file": "release_manifest.json",
        "files": {"components/core.txt": "core-sdk | version=1.2.0 | risk=LOW | gate=PASS\nauth-mod | version=2.0.1 | risk=MEDIUM | gate=PASS\n"},
        "prompt": "Validate `components/core.txt` and write `release_manifest.json`. Keep `components/` unchanged.",
        "expected": {
            "manifest_version": "rel-v1",
            "release_ready": True,
            "approved": [
                {"package": "auth-mod", "version": "2.0.1", "risk": "medium"},
                {"package": "core-sdk", "version": "1.2.0", "risk": "low"},
            ],
            "blocked": [],
        },
    },
    {
        "id": "release-2-single-blocker",
        "domain": "release-manifest",
        "target_skill": "release-manifest-sop",
        "output_file": "release_manifest.json",
        "files": {"components/svc.txt": "web-ui | version=3.1.0 | risk=LOW | gate=PASS\npay-worker | version=1.4.2 | risk=HIGH | gate=FAIL\n"},
        "prompt": "Check `components/svc.txt` and generate `release_manifest.json` without editing `components/`.",
        "expected": {
            "manifest_version": "rel-v1",
            "release_ready": False,
            "approved": [{"package": "web-ui", "version": "3.1.0", "risk": "low"}],
            "blocked": ["pay-worker"],
        },
    },
    {
        "id": "release-3-hold-blocks-release",
        "domain": "release-manifest",
        "target_skill": "release-manifest-sop",
        "output_file": "release_manifest.json",
        "files": {"components/a.txt": "db-migrator | version=0.9.5 | risk=HIGH | gate=HOLD\ncache-proxy | version=1.1.0 | risk=LOW | gate=PASS\n"},
        "prompt": "Read `components/a.txt` and produce `release_manifest.json`. Keep `components/a.txt` untouched.",
        "expected": {
            "manifest_version": "rel-v1",
            "release_ready": False,
            "approved": [{"package": "cache-proxy", "version": "1.1.0", "risk": "low"}],
            "blocked": ["db-migrator"],
        },
    },
    {
        "id": "release-4-empty-approved-not-ready",
        "domain": "release-manifest",
        "target_skill": "release-manifest-sop",
        "output_file": "release_manifest.json",
        "files": {"components/empty.txt": "legacy-cli | version=0.4.0 | risk=CRITICAL | gate=FAIL\n"},
        "prompt": "Inspect `components/empty.txt` and write `release_manifest.json` leaving `components/` untouched.",
        "expected": {"manifest_version": "rel-v1", "release_ready": False, "approved": [], "blocked": ["legacy-cli"]},
    },
    {
        "id": "release-5-multi-component-sort",
        "domain": "release-manifest",
        "target_skill": "release-manifest-sop",
        "output_file": "release_manifest.json",
        "files": {
            "components/g1.txt": "zeta-api | version=2.1.0 | risk=MEDIUM | gate=PASS\nalpha-lib | version=1.0.3 | risk=LOW | gate=PASS\n",
            "components/g2.txt": "beta-job | version=4.0.0 | risk=HIGH | gate=HOLD\ngamma-rpc | version=1.8.0 | risk=CRITICAL | gate=FAIL\n",
        },
        "prompt": "Read `components/g1.txt` and `components/g2.txt`, then write `release_manifest.json`.",
        "expected": {
            "manifest_version": "rel-v1",
            "release_ready": False,
            "approved": [
                {"package": "alpha-lib", "version": "1.0.3", "risk": "low"},
                {"package": "zeta-api", "version": "2.1.0", "risk": "medium"},
            ],
            "blocked": ["beta-job", "gamma-rpc"],
        },
    },
    {
        "id": "release-6-signature-preserve",
        "domain": "release-manifest",
        "target_skill": "release-manifest-sop",
        "output_file": "release_manifest.json",
        "files": {
            "components/batch.txt": "telemetry | version=1.5.0 | risk=LOW | gate=PASS\ningestor | version=2.3.4 | risk=MEDIUM | gate=PASS\n",
            "components/release.sig": "SIGNED_SHA256_LOCKED",
        },
        "prompt": "Inspect `components/batch.txt` and `components/release.sig`, then output `release_manifest.json`.",
        "expected": {
            "manifest_version": "rel-v1",
            "release_ready": True,
            "approved": [
                {"package": "ingestor", "version": "2.3.4", "risk": "medium"},
                {"package": "telemetry", "version": "1.5.0", "risk": "low"},
            ],
            "blocked": [],
        },
    },
]


def _verify_task_output(root: Path, task: dict) -> tuple[bool, str]:
    for rel, original in task["files"].items():
        path = root / rel
        if not path.is_file() or path.read_text(encoding="utf-8") != original:
            return False, f"Protected input modified or deleted: {rel}"
    out = root / task["output_file"]
    if not out.is_file():
        return False, f"Missing output file {task['output_file']}"
    try:
        actual = json.loads(out.read_text(encoding="utf-8"))
    except (ValueError, OSError) as err:
        return False, f"{task['output_file']} invalid JSON: {type(err).__name__}"
    expected = task["expected"]
    if not isinstance(actual, dict):
        return False, f"{task['output_file']} must be a JSON object"
    diffs = []
    for key, want in expected.items():
        if key not in actual:
            diffs.append(f"missing key '{key}'")
        elif actual[key] != want:
            diffs.append(f"field '{key}' does not satisfy SOP schema")
    if set(actual) - set(expected):
        diffs.append(f"unexpected extra top-level keys {sorted(set(actual) - set(expected))}")
    return (False, "; ".join(diffs)) if diffs else (True, "ok")


def _write_task_files(root: Path, task: dict, *, with_skills: bool) -> None:
    for rel, content in task["files"].items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if with_skills:
        for name, spec in SKILLS_SPEC.items():
            sp = root / ".weyra" / "skills" / name / "SKILL.md"
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(
                "---\n"
                f"name: {name}\n"
                'version: "1.0.0"\n'
                f"description: {json.dumps(spec['description'])}\n"
                f"when_to_use: {json.dumps(spec['when_to_use'])}\n"
                "---\n"
                + spec["body"],
                encoding="utf-8",
            )


def _exact_two_sided_mcnemar(a_wins: int, b_wins: int) -> float:
    n = a_wins + b_wins
    if n == 0:
        return 1.0
    k = min(a_wins, b_wins)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5 ** n)
    return round(min(1.0, 2.0 * tail), 6)


def _paired_bootstrap_ci(vals_cand: list[bool], vals_base: list[bool], n_boot: int = 2000) -> list[float]:
    rng = random.Random(42)
    n = len(vals_cand)
    deltas = []
    for _ in range(n_boot):
        idxs = [rng.randrange(n) for _ in range(n)]
        diff = sum(int(vals_cand[i]) - int(vals_base[i]) for i in idxs) / n
        deltas.append(diff)
    deltas.sort()
    lo = deltas[int(0.025 * n_boot)]
    hi = deltas[int(0.975 * n_boot)]
    return [round(lo * 100, 2), round(hi * 100, 2)]


async def _run_arm(runtime, budget: Budget, arm_name: str, *, with_skills: bool, max_rounds: int) -> dict:
    sem = asyncio.Semaphore(3)
    arm_tokens_before = budget.total_tokens
    arm_calls_before = budget.calls
    arm_cost_before = budget.total_cost_cny

    async def _run_one(task: dict) -> dict:
        async with sem:
            with tempfile.TemporaryDirectory(prefix=f"weyra-eval-{arm_name}-{task['id']}-") as tmp:
                root = Path(tmp)
                _write_task_files(root, task, with_skills=with_skills)
                agent = Agent(
                    root,
                    runtime,
                    permission_mode="bypassPermissions",
                    budget=budget,
                    max_steps=8,
                    enable_memory=False,
                    enable_evolution=False,
                    owns_runtime=False,
                    session_id=f"{arm_name}-{task['id']}",
                )
                allowed = {"read_file", "write_file", "list_files", "skill"}
                agent.tools.specs = {k: v for k, v in agent.tools.specs.items() if k in allowed}
                passed = False
                diag = ""
                rounds_used = 0
                error_msg = None
                try:
                    prompt = task["prompt"]
                    if with_skills:
                        # 盲测 Skill-Pro C-first BM25 + SkillAlign when_to_use：Prompt 不透露任何具体技能名
                        prompt += " Check available workspace skills via the `skill` tool if applicable and follow the matching SOP strictly."
                    await asyncio.wait_for(agent.run_once(prompt), timeout=150)
                    rounds_used = 1
                    passed, diag = _verify_task_output(root, task)
                    if not passed and max_rounds >= 2:
                        repair_prompt = (
                            f"Deterministic verifier failed on `{task['output_file']}`: {diag}. "
                            "Repair it now while preserving all input directory files untouched."
                        )
                        await asyncio.wait_for(agent.run_once(repair_prompt), timeout=150)
                        rounds_used = 2
                        passed, diag = _verify_task_output(root, task)
                except BudgetExceeded as exc:
                    error_msg = f"BudgetExceeded: {exc}"
                except Exception as exc:
                    error_msg = f"{type(exc).__name__}: {exc}"
                sid = agent.session.session_id
                usage_rows = [
                    r["data"] for r in agent.session.records if r.get("kind") == "model_usage"
                ]
                task_calls = len(usage_rows)
                task_tokens = sum(int(u.get("accounted_tokens") or 0) for u in usage_rows)
                task_cost = sum(
                    (
                        Decimal(u["actual_cost_cny"])
                        for u in usage_rows
                        if u.get("actual_cost_cny") is not None
                    ),
                    Decimal(0),
                )
                await agent.close(close_runtime=False)
                audit = audit_session_db(root, sid, task_passed=passed)
                activated = list(audit.skill_funnel.activated)
                clean_run = (
                    audit.process.completed
                    and audit.process.error_loops == 0
                    and audit.process.verify_after_edit
                )
                return {
                    "task_id": task["id"],
                    "domain": task["domain"],
                    "target_skill": task["target_skill"],
                    "passed": passed,
                    "rounds_used": rounds_used,
                    "diagnostic": diag,
                    "error": error_msg,
                    "tokens": task_tokens,
                    "calls": task_calls,
                    "cost_cny": str(task_cost),
                    "clean_run": clean_run,
                    "skill_activated": activated,
                    "target_skill_activated": task["target_skill"] in activated,
                    "distractor_activated": "prose-style-guide" in activated,
                    "reliable": audit.reliability.reliable,
                }

    records = await asyncio.gather(*(_run_one(task) for task in TASKS))
    passed_total = sum(1 for r in records if r["passed"])
    domains = {}
    for dom in ("audit-ledger", "incident-triage", "release-manifest"):
        sub = [r for r in records if r["domain"] == dom]
        domains[dom] = {
            "passed": sum(1 for r in sub if r["passed"]),
            "total": len(sub),
            "clean": sum(1 for r in sub if r["clean_run"]),
        }
    return {
        "arm": arm_name,
        "passed": passed_total,
        "total": len(records),
        "pass_rate": round(passed_total / len(records), 4),
        "tokens": budget.total_tokens - arm_tokens_before,
        "calls": budget.calls - arm_calls_before,
        "cost_cny": str(budget.total_cost_cny - arm_cost_before),
        "clean_runs": sum(1 for r in records if r["clean_run"]),
        "domains": domains,
        "routing": {
            "target_activated": sum(1 for r in records if r["target_skill_activated"]),
            "distractor_activated": sum(1 for r in records if r["distractor_activated"]),
        },
        "tasks": list(records),
    }


async def _run_live_evolution_suite(runtime, budget: Budget) -> dict:
    """运行 3 项真实 DeepSeek 有界演进与护栏校验 (SkillDiff / SkillOpt / SkillGen, 0 Judge calls)。"""
    tokens_before = budget.total_tokens
    calls_before = budget.calls
    cost_before = budget.total_cost_cny

    def _extract_json(text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise

    # 1. SkillDiff **[FOCUS]** 专家条款护栏 + SkillOpt 被拒编辑缓冲 + 真实 1.0.0 -> 1.0.1 有界补丁晋升 (2 独立验证样本, 0 Judge calls)
    with tempfile.TemporaryDirectory(prefix="weyra-eval-evo-patch-") as tmp1:
        root = Path(tmp1)
        skills = SkillStore(root)
        session = SessionStore(project=root, session_id="live-evo-patch")
        evo = EvolutionStore(skills, session=session)
        spec = SKILLS_SPEC["audit-ledger-sop"]
        seed_body = (
            "## Protected Rules\n"
            f"- **[FOCUS]** {spec['focus'][0]}\n"
            f"- **[FOCUS]** {spec['focus'][1]}\n"
            "## Procedure\n"
            "1. Read every file in `records/` and write `audit_report.json` with `schema_version=\"sop-v2\"`, `status=\"ok\"`, `summary=\"kept=0\"`, and `items=[]`.\n"
        )
        evo.create(
            "audit-ledger-sop",
            seed_body,
            description=spec["description"],
            when_to_use=spec["when_to_use"],
            permission_mode="acceptEdits",
        )
        snap = skills.load("audit-ledger-sop")
        focus_guard_res = evo.apply(
            "audit-ledger-sop",
            [{"op": "delete", "target": f"- **[FOCUS]** {spec['focus'][0]}"}],
            base_hash=snap.content_hash,
            permission_mode="acceptEdits",
        )
        patch_manifest = {
            "cases": [
                {
                    "id": "evo-val-1",
                    "split": "validation",
                    "prompt": "Read `records/val1.txt` and write `audit_report.json`.",
                    "files": {"records/val1.txt": "ID-701 | status=ACTIVE | severity=HIGH\nID-702 | status=ARCHIVED | severity=LOW\n"},
                    "deterministic_only": True,
                    "hard_constraints": {
                        "required_output_paths": ["audit_report.json"],
                        "allowed_output_paths": ["audit_report.json"],
                        "expected_semantic_artifacts": {
                            "audit_report.json": {
                                "schema_version": "sop-v2",
                                "status": "ok",
                                "summary": "kept=1",
                                "items": [{"source": "ID-701", "severity": "high"}],
                            }
                        },
                    },
                },
                {
                    "id": "evo-val-2",
                    "split": "validation",
                    "prompt": "Read `records/val2.txt` and generate `audit_report.json`.",
                    "files": {"records/val2.txt": "ID-805 | status=CRITICAL | severity=CRITICAL\nID-801 | status=ACTIVE | severity=LOW\n"},
                    "deterministic_only": True,
                    "hard_constraints": {
                        "required_output_paths": ["audit_report.json"],
                        "allowed_output_paths": ["audit_report.json"],
                        "expected_semantic_artifacts": {
                            "audit_report.json": {
                                "schema_version": "sop-v2",
                                "status": "ok",
                                "summary": "kept=2",
                                "items": [
                                    {"source": "ID-801", "severity": "low"},
                                    {"source": "ID-805", "severity": "critical"},
                                ],
                            }
                        },
                    },
                },
            ]
        }
        gate = SkillValidationGate(runtime, budget, patch_manifest, session=session)
        patch_evidence = {
            "user": "Read `records/dev.txt` and write `audit_report.json`.",
            "assistant": "Wrote audit_report.json with empty items=[].",
            "feedback": (
                "Correction for audit-ledger-sop: update step 1 so `items` keeps only ACTIVE or CRITICAL records sorted ascending by ID "
                'as `{"source":"<ID>","severity":"<lowercase>"}` and sets `summary` to `"kept=<N>"`, while keeping all **[FOCUS]** lines untouched.'
            ),
            "trace_summary": {"tool_calls": ["skill", "read_file", "write_file"], "error_tools": [], "retry_count": 0, "turn_state": "completed"},
        }

        async def propose_patch(_):
            ctx = RunContext(root, session, budget, "acceptEdits")
            cur = skills.load("audit-ledger-sop")
            payload = {
                "evidence": patch_evidence,
                "available_skills": [{"name": cur.name, "description": cur.description}],
                "target": asdict_snapshot(cur),
                "focus_items": list(cur.focus_items),
                "edit_budget": evo.current_edit_budget(cur.name),
                "rejected_edits": evo.rejected_edits_prompt(cur.name),
            }
            req = PreparedRequest(
                runtime.resolve("main"),
                MAINTENANCE_INSTRUCTION,
                (Message("user", canonical(payload), source="evidence"),),
                purpose="evolution",
            )
            resp = await runtime.complete(req, ctx)
            await ctx.close()
            return _extract_json(resp.content)

        try:
            patch_res = await evo.maintain(
                patch_evidence,
                propose_patch,
                permission_mode="acceptEdits",
                target_snapshot=skills.load("audit-ledger-sop"),
                validation_gate=gate,
            )
        except Exception as exc:
            patch_res = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        after_patch_snap = skills.load("audit-ledger-sop")
        patch_focus_ok = all(a in after_patch_snap.body for a in spec["focus"])
        rejected_count = len(evo.rejected_edits("audit-ledger-sop", limit=10))
        session.close()

    # 2. SkillOpt 零空基线门槛 (__null_baseline__ 对照门禁创建全新技能)
    with tempfile.TemporaryDirectory(prefix="weyra-eval-evo-create-") as tmp2:
        root2 = Path(tmp2)
        skills2 = SkillStore(root2)
        session2 = SessionStore(project=root2, session_id="live-evo-create")
        evo2 = EvolutionStore(skills2, session=session2)
        create_manifest = {
            "cases": [
                {
                    "id": "gen-val-1",
                    "split": "validation",
                    "prompt": "Validate `components/v1.txt` and write `release_manifest.json`.",
                    "files": {"components/v1.txt": "alpha | version=1.0.0 | risk=LOW | gate=PASS\nbeta | version=2.0.0 | risk=HIGH | gate=FAIL\n"},
                    "deterministic_only": True,
                    "hard_constraints": {
                        "required_output_paths": ["release_manifest.json"],
                        "allowed_output_paths": ["release_manifest.json"],
                        "expected_semantic_artifacts": {
                            "release_manifest.json": {
                                "manifest_version": "rel-v1",
                                "release_ready": False,
                                "approved": [{"package": "alpha", "version": "1.0.0", "risk": "low"}],
                                "blocked": ["beta"],
                            }
                        },
                    },
                },
                {
                    "id": "gen-val-2",
                    "split": "validation",
                    "prompt": "Check `components/v2.txt` and generate `release_manifest.json`.",
                    "files": {"components/v2.txt": "gamma | version=1.1.0 | risk=MEDIUM | gate=PASS\n"},
                    "deterministic_only": True,
                    "hard_constraints": {
                        "required_output_paths": ["release_manifest.json"],
                        "allowed_output_paths": ["release_manifest.json"],
                        "expected_semantic_artifacts": {
                            "release_manifest.json": {
                                "manifest_version": "rel-v1",
                                "release_ready": True,
                                "approved": [{"package": "gamma", "version": "1.1.0", "risk": "medium"}],
                                "blocked": [],
                            }
                        },
                    },
                },
            ]
        }
        gate2 = SkillValidationGate(runtime, budget, create_manifest, session=session2)
        create_evidence = {
            "user": "Check `components/dev.txt` and produce `release_manifest.json`.",
            "assistant": "Wrote release_manifest.json in generic schema.",
            "feedback": (
                "Create a new skill named `release-manifest-sop` (with `when_to_use`) specifying: "
                "read files in `components/`, put `gate=PASS` entries sorted by `package` into `approved` as "
                '`{"package":"<name>","version":"<ver>","risk":"<lower>"}`, put `gate=FAIL` or `HOLD` package names sorted into `blocked`, '
                'set `manifest_version="rel-v1"`, and set `release_ready` boolean true iff `blocked` is empty and `approved` is non-empty.'
            ),
            "trace_summary": {"tool_calls": ["read_file", "write_file"], "error_tools": [], "retry_count": 0, "turn_state": "completed"},
        }

        async def propose_create(_):
            ctx2 = RunContext(root2, session2, budget, "acceptEdits")
            payload2 = {
                "evidence": create_evidence,
                "available_skills": [],
                "target": None,
                "focus_items": [],
                "edit_budget": 1,
                "rejected_edits": "",
            }
            req2 = PreparedRequest(
                runtime.resolve("main"),
                MAINTENANCE_INSTRUCTION,
                (Message("user", canonical(payload2), source="evidence"),),
                purpose="evolution",
            )
            resp2 = await runtime.complete(req2, ctx2)
            await ctx2.close()
            return _extract_json(resp2.content)

        try:
            create_res = await evo2.maintain(
                create_evidence,
                propose_create,
                permission_mode="acceptEdits",
                validation_gate=gate2,
            )
        except Exception as exc:
            create_res = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        created_catalog = skills2.capture_catalog()
        session2.close()

    return {
        "focus_guard_blocked": focus_guard_res.get("failure_kind") == "focus_item_violation",
        "rejected_edit_buffer_count": rejected_count,
        "patch_evolution": {
            "status": patch_res.get("status"),
            "reason": patch_res.get("reason"),
            "error": patch_res.get("error"),
            "judge_mode": (patch_res.get("validation_report") or {}).get("judge_mode"),
            "old_successes": ((patch_res.get("validation_report") or {}).get("old") or {}).get("successes"),
            "candidate_successes": ((patch_res.get("validation_report") or {}).get("candidate") or {}).get("successes"),
            "version_before": "1.0.0",
            "version_after": after_patch_snap.version,
            "focus_items_preserved": patch_focus_ok,
        },
        "skillgen_create": {
            "status": create_res.get("status"),
            "reason": create_res.get("reason"),
            "error": create_res.get("error"),
            "judge_mode": (create_res.get("validation_report") or {}).get("judge_mode"),
            "null_baseline_successes": ((create_res.get("validation_report") or {}).get("old") or {}).get("successes"),
            "candidate_successes": ((create_res.get("validation_report") or {}).get("candidate") or {}).get("successes"),
            "created_in_catalog": "release-manifest-sop" in created_catalog,
        },
        "tokens": budget.total_tokens - tokens_before,
        "calls": budget.calls - calls_before,
        "cost_cny": str(budget.total_cost_cny - cost_before),
    }


async def main_async() -> dict:
    config = load_config(ROOT)
    runtime = config.build_runtime()
    runtime.fallbacks["main"] = []
    model = runtime.resolve("main")
    budget = Budget(cost_limit_cny="4.00", call_limit=500)
    with tempfile.TemporaryDirectory(prefix="weyra-eval-ledger-") as ledger_tmp:
        ledger_session = SessionStore(project=Path(ledger_tmp), session_id="eval-root-ledger")
        budget.attach(ledger_session)
        try:
            arm_a = await _run_arm(runtime, budget, "no_skill_1x", with_skills=False, max_rounds=1)
            arm_b = await _run_arm(runtime, budget, "matched_budget_retry_2x", with_skills=False, max_rounds=2)
            arm_c = await _run_arm(runtime, budget, "weyra_skill_1x", with_skills=True, max_rounds=1)
            evo_suite = await _run_live_evolution_suite(runtime, budget)
        finally:
            ledger_session.close()
            await runtime.close()

    pa = [r["passed"] for r in arm_a["tasks"]]
    pb = [r["passed"] for r in arm_b["tasks"]]
    pc = [r["passed"] for r in arm_c["tasks"]]
    c_vs_a_wins = sum(c and not a for c, a in zip(pc, pa))
    c_vs_a_losses = sum(a and not c for c, a in zip(pc, pa))
    c_vs_b_wins = sum(c and not b for c, b in zip(pc, pb))
    c_vs_b_losses = sum(b and not c for c, b in zip(pc, pb))
    stats = {
        "skill_1x_vs_no_skill_1x": {
            "wins": c_vs_a_wins,
            "losses": c_vs_a_losses,
            "ties": len(TASKS) - c_vs_a_wins - c_vs_a_losses,
            "exact_two_sided_mcnemar_p": _exact_two_sided_mcnemar(c_vs_a_wins, c_vs_a_losses),
            "delta_pp": round((arm_c["pass_rate"] - arm_a["pass_rate"]) * 100, 2),
            "bootstrap_95_ci_pp": _paired_bootstrap_ci(pc, pa),
        },
        "skill_1x_vs_retry_2x": {
            "wins": c_vs_b_wins,
            "losses": c_vs_b_losses,
            "ties": len(TASKS) - c_vs_b_wins - c_vs_b_losses,
            "exact_two_sided_mcnemar_p": _exact_two_sided_mcnemar(c_vs_b_wins, c_vs_b_losses),
            "delta_pp": round((arm_c["pass_rate"] - arm_b["pass_rate"]) * 100, 2),
            "bootstrap_95_ci_pp": _paired_bootstrap_ci(pc, pb),
        },
    }

    summary = {
        "model": model.model_id,
        "provider": model.provider_id,
        "total_tokens": budget.total_tokens,
        "total_calls": budget.calls,
        "total_cost_cny": str(budget.total_cost_cny),
        "paper_references": {
            "skillopt": "arXiv:2605.23904 (NeurIPS 2026 Spotlight, microsoft/SkillOpt MIT)",
            "skilldiff": "arXiv:2611.25534 (Nov 2026, LitaoGuo/How-Self-Evolution-Reshapes-Skills MIT)",
            "skillsbench": "arXiv:2602.12670",
            "sopbench": "EMNLP 2026",
            "agentskills_and_harnesses": "DeepSeek Harness / Pi / Hermes Agent",
        },
        "arms": {"no_skill_1x": arm_a, "matched_budget_retry_2x": arm_b, "weyra_skill_1x": arm_c},
        "paired_statistics": stats,
        "live_evolution_suite": evo_suite,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pe = evo_suite["patch_evolution"]
    ge = evo_suite["skillgen_create"]
    md_lines = [
        f"# DeepSeek (`{model.model_id}`) 多域技能路由、TTS 对比与在线演进评测报告",
        "",
        f"- **评测端点与模型**：`https://api.deepseek.com` / `{model.model_id}`（严禁 fallback）",
        "- **评测规模**：3 个业务域 × 6 题 = **18 项任务 × 3 组配对（共 54 次完整任务运行）** + 在线演进协议实测",
        f"- **总调用统计**：{budget.calls} 次模型请求 ｜ {budget.total_tokens:,} tokens ｜ Weyra 高峰期 ceiling 记账 **¥{budget.total_cost_cny:.4f}**",
        "- **独立裁判 API 消耗**：0 次（全确定性可执行断言判分）",
        "",
        "## 一、18 题三域配对任务消融结果（`<available_skills>` 盲选披露）",
        "",
        "| 实验组 | 对标论文/框架 | 通过率 | 洁净轨迹数 | 目标技能激活 / 干扰误触发 | 总 Calls | 总 Tokens | 账本费用 (CNY) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| Arm A: No-Skill (1x) | SkillsBench (`arXiv:2602.12670`) | {arm_a['passed']}/{arm_a['total']} ({arm_a['pass_rate']:.1%}) | {arm_a['clean_runs']}/{arm_a['total']} | — | {arm_a['calls']} | {arm_a['tokens']:,} | ¥{Decimal(arm_a['cost_cny']):.4f} |",
        f"| Arm B: Matched-Budget Retry (2x) | Test-Time Retry | {arm_b['passed']}/{arm_b['total']} ({arm_b['pass_rate']:.1%}) | {arm_b['clean_runs']}/{arm_b['total']} | — | {arm_b['calls']} | {arm_b['tokens']:,} | ¥{Decimal(arm_b['cost_cny']):.4f} |",
        f"| Arm C: Weyra Skill (1x) | DSH + Pi + Hermes `<available_skills>` | **{arm_c['passed']}/{arm_c['total']} ({arm_c['pass_rate']:.1%})** | **{arm_c['clean_runs']}/{arm_c['total']}** | **{arm_c['routing']['target_activated']}/{arm_c['total']}** / {arm_c['routing']['distractor_activated']} | {arm_c['calls']} | {arm_c['tokens']:,} | **¥{Decimal(arm_c['cost_cny']):.4f}** |",
        "",
        "### 分域表现（Audit Ledger / Incident Triage / Release Manifest）",
        "",
        "| 业务域 | No-Skill (1x) | Retry (2x) | Weyra Skill (1x) | Weyra 洁净轨迹 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for dom, label in (("audit-ledger", "Audit Ledger SOP (6 题)"), ("incident-triage", "Incident Triage SOP (6 题)"), ("release-manifest", "Release Manifest SOP (6 题)")):
        da, db, dc = arm_a["domains"][dom], arm_b["domains"][dom], arm_c["domains"][dom]
        md_lines.append(f"| {label} | {da['passed']}/{da['total']} | {db['passed']}/{db['total']} | **{dc['passed']}/{dc['total']}** | {dc['clean']}/{dc['total']} |")
    s_ca, s_cb = stats["skill_1x_vs_no_skill_1x"], stats["skill_1x_vs_retry_2x"]
    md_lines.extend([
        "",
        "### 配对统计检验（精确 McNemar 与 2,000 次 Bootstrap 95% CI）",
        "",
        f"- **Weyra Skill (1x) vs No-Skill (1x)**：**{s_ca['delta_pp']:+.2f}pp**（{s_ca['wins']} 胜 {s_ca['losses']} 负 {s_ca['ties']} 平），精确双侧 McNemar `p = {s_ca['exact_two_sided_mcnemar_p']}`，Bootstrap 95% CI `{s_ca['bootstrap_95_ci_pp']}pp`。",
        f"- **Weyra Skill (1x) vs Matched-Budget Retry (2x)**：**{s_cb['delta_pp']:+.2f}pp**（{s_cb['wins']} 胜 {s_cb['losses']} 负 {s_cb['ties']} 平），精确双侧 McNemar `p = {s_cb['exact_two_sided_mcnemar_p']}`，Bootstrap 95% CI `{s_cb['bootstrap_95_ci_pp']}pp`。",
        "",
        "## 二、在线有界技能演进与护栏实测（2 独立验证样本，0 Judge 调用）",
        "",
        f"- **SkillDiff `**[FOCUS]**` 篡改拦截**：`{evo_suite['focus_guard_blocked']}`（`rejected_edits.jsonl` 记录数：{evo_suite['rejected_edit_buffer_count']}）",
        f"- **SkillOpt 有界补丁演进**：状态 `{pe['status']}`（`{pe['reason']}`），版本 `{pe['version_before']} -> {pe['version_after']}`，验证集通过数 `{pe['old_successes']} -> {pe['candidate_successes']}`，门禁模式 `{pe['judge_mode']}`，`[FOCUS]` 保留 `{pe['focus_items_preserved']}`",
        f"- **SkillOpt 零空基线新建门禁（`__null_baseline__`）**：状态 `{ge['status']}`（`{ge['reason']}`），无技能基线 vs 新建技能 `{ge['null_baseline_successes']} -> {ge['candidate_successes']}`，门禁模式 `{ge['judge_mode']}`，目录入库 `{ge['created_in_catalog']}`",
        f"- 演进阶段共消耗 `{evo_suite['calls']}` 次调用 / `{evo_suite['tokens']:,}` tokens / 账本费用 `¥{Decimal(evo_suite['cost_cny']):.4f}`，详情见 [results.json](results.json)。",
    ])
    (OUT_DIR / "report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    res = asyncio.run(main_async())
    print(f"DONE: total_calls={res['total_calls']}, total_tokens={res['total_tokens']}, total_cost_cny=¥{res['total_cost_cny']}")
    for k, arm in res["arms"].items():
        print(f"  {k}: {arm['passed']}/{arm['total']} ({arm['pass_rate']:.1%}), clean={arm['clean_runs']}, routing={arm['routing']}, cost=¥{arm['cost_cny']}")
    print("  stats:", res["paired_statistics"])
    print("  live_evo:", res["live_evolution_suite"])

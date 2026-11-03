"""Bounded completion and recovery of the frozen legacy evaluation cohorts.

This module deliberately loads the archived implementation under a private
package name.  That keeps every relative import inside the frozen source tree
and prevents the current ``weyra`` package from changing the legacy results.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
FINISH_FREEZE = REPO_ROOT / ".weyra/finish-20261018/frozen-source"
CORE_FREEZE = REPO_ROOT / ".weyra/core-eval-20261018-v3/frozen-source"
FINISH_ORIGINALS = REPO_ROOT / ".weyra/finish-20261018/formal-v2/results.json"
CORE_ORIGINALS = REPO_ROOT / ".weyra/core-eval-20261018-v3/results.json"
FINISH_SNAPSHOT = REPO_ROOT / ".weyra/finish-20261018/source-freeze.json"
CORE_SNAPSHOT = REPO_ROOT / ".weyra/core-eval-20261018-v3/source-snapshot.json"

INFRASTRUCTURE = {"error", "interrupted", "timeout", "budget_stopped"}
FINISH_LOCALIZATION_UNITS = 96
FINISH_SKILL_TEST_UNITS = 48
CORE_PROGRESSIVE_UNITS = 54


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frozen_package(source_root: Path, alias: str) -> ModuleType:
    """Load a frozen weyra package so its relative imports stay frozen too."""
    package_path = source_root / "weyra" / "__init__.py"
    if not package_path.is_file():
        package_path = source_root / "agents" / "__init__.py"
    if not package_path.is_file():
        raise FileNotFoundError(f"Frozen package is missing: {package_path}")
    existing = sys.modules.get(alias)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        alias, package_path, submodule_search_locations=[str(package_path.parent)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load frozen package from {package_path}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[alias] = package
    spec.loader.exec_module(package)
    return package


def _frozen_module(source_root: Path, alias: str, module: str) -> ModuleType:
    package = _frozen_package(source_root, alias)
    name = f"{package.__name__}.{module}"
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    return __import__(name, fromlist=["*"])


def _snapshot_integrity(source_root: Path, snapshot_path: Path) -> dict[str, str]:
    """Check and return the archived file hashes before importing the runner."""
    expected = _read_json(snapshot_path)
    if "files" in expected:  # core snapshot shape
        expected = expected["files"]
    mismatches = {}
    for relative, wanted in expected.items():
        path = source_root / relative
        if not path.is_file() or _sha256(path) != wanted:
            mismatches[relative] = wanted
    if mismatches:
        raise ValueError(f"Frozen source integrity check failed: {sorted(mismatches)}")
    return expected


def _finish_plan(manifest: dict) -> list[dict]:
    from collections import OrderedDict

    rows = OrderedDict()
    for repeat in range(2):
        for case in manifest["localization_cases"]:
            for arm in ("old", "new"):
                key = f'localization/{case["id"]}/{arm}/{repeat}'
                rows[key] = {"key": key, "id": case["id"], "group": "localization",
                             "variant": arm, "repeat": repeat,
                             "cluster": case["category"], "case_id": case["id"]}
        for family in manifest["families"]:
            for task in family["tasks"]:
                if task["split"] == "test":
                    for arm in ("frozen", "adapted"):
                        key = f'skill_test/{task["id"]}/{arm}/{repeat}'
                        rows[key] = {"key": key, "id": task["id"], "group": "skill_test",
                                     "variant": arm, "repeat": repeat,
                                     "cluster": family["id"], "case_id": task["id"]}
    return list(rows.values())


def _core_progressive_plan(manifest: dict) -> list[dict]:
    rows = []
    for repeat in range(3):
        for family in manifest["evolution_families"]:
            case = next(task for task in family["tasks"] if task["split"] == "test")
            for arm in ("old_old", "old_new", "new_new"):
                key = f'progressive/{case["id"]}/{arm}/{repeat}'
                rows.append({"key": key, "id": case["id"], "group": "progressive",
                             "variant": arm, "repeat": repeat, "cluster": family["id"],
                             "case_id": case["id"], "family_id": family["id"]})
    return rows


def _maintained_body(workspace: Path, skill_name: str, fallback: str, skill_store_type) -> tuple[str, str]:
    """Read the last committed maintained skill body, falling back to seed."""
    path = workspace / ".weyra" / "skills" / skill_name / "SKILL.md"
    if path.is_file() and not path.is_symlink():
        store = skill_store_type(workspace, skill_dirs=[])
        snapshot = store.capture_catalog().get(skill_name)
        if snapshot is not None:
            return snapshot.body, "maintained_snapshot"
    return fallback, "initial_body_fallback"


def _copy_core_snapshots(source_root: Path, destination_root: Path, manifest: dict,
                         originals: dict, skill_store_type) -> dict[str, dict]:
    seeded = {}
    for repeat in range(3):
        for family in manifest["evolution_families"]:
            name = family["id"]
            source_workspace = source_root / "units" / f"{name}-experience_bounded-{repeat}"
            body, origin = _maintained_body(source_workspace, name, family["initial_body"],
                                            skill_store_type)
            workspace = destination_root / "units" / f"{name}-experience_bounded-{repeat}"
            skill_path = workspace / ".weyra" / "skills" / name / "SKILL.md"
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            # Snapshot only the maintained skill.  Task artifacts and generated files
            # from the historical workspaces are never copied into the new run.
            from weyra.frontmatter import atomic
            atomic(skill_path, "---\nname: " + name + "\nversion: 1.0.0\ndescription: \"\"\naliases: []\n---\n" + body)
            maintenance_key = f"maintenance/{name}/experience_bounded/{repeat}"
            seeded[f"{name}/{repeat}"] = {
                "source_workspace": str(source_workspace), "snapshot_origin": origin,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "maintenance_status": originals.get(maintenance_key, {}).get("status", "not_recorded"),
            }
    return seeded


def _infra_keys(results: dict, group: str) -> list[str]:
    return [key for key, row in results.items()
            if key.startswith(group + "/") and row.get("status") in INFRASTRUCTURE]


class _BudgetErrorBridge:
    """Preserve runtime API while translating only the shared budget exception."""
    def __init__(self, runtime, source_error, frozen_error):
        self._runtime = runtime
        self._source_error = source_error
        self._frozen_error = frozen_error

    def __getattr__(self, name):
        return getattr(self._runtime, name)

    async def complete(self, request, context, *, attempt_budget=None, **kwargs):
        try:
            return await self._runtime.complete(request, context,
                                                attempt_budget=attempt_budget, **kwargs)
        except self._source_error as error:
            raise self._frozen_error(str(error)) from None

    async def stream(self, request, context, *, attempt_budget=None, **kwargs):
        try:
            async for event in self._runtime.stream(request, context,
                                                   attempt_budget=attempt_budget, **kwargs):
                yield event
        except self._source_error as error:
            raise self._frozen_error(str(error)) from None


def _budget_aware_runner(base, source_error, frozen_error):
    """Build a per-call runner subclass that translates errors outside runtime calls too."""
    class BudgetAwareRunner(base):
        async def unit(self, key, identity, operation):
            async def translated_operation():
                try:
                    return await operation()
                except source_error as error:
                    raise frozen_error(str(error)) from None
            return await super().unit(key, identity, translated_operation)
    return BudgetAwareRunner


def _write_partial_report(path: Path, *, results: dict, protocol: dict, ledger_root: Path):
    baseline = {
        "finish_results_path": str(FINISH_ORIGINALS),
        "finish_results_sha256": protocol["historical_finish_results_sha256"],
        "core_results_path": str(CORE_ORIGINALS),
        "core_results_sha256": protocol["historical_core_results_sha256"],
        "localization_recorded": 13, "skill_test_recorded": 48,
        "originals_mutated": False,
    }
    payload = {
        "format": "weyra-final-legacy-eval-v1", "planned_executions": 148,
        "execution_counts": {"localization_missing": 83, "localization_recovery": 4,
                             "skill_test_recovery": 7, "progressive": 54},
        "completed_new_executions": sum(row.get("run_type") != "baseline"
                                         and row.get("result") is not None
                                         for row in results.values()),
        "historical_baseline": baseline,
        "ledger": {"root": str(ledger_root), "session_id": "finish-live-cny100",
                   "cost_limit_cny": "100"},
        "results": results,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


async def run_legacy(runtime, budget, output, ledger_root):
    """Run 83 missing finish-localization units, 11 one-time recoveries, and 54
    progressive units using the exact archived finish/core implementations.

    ``runtime``, ``budget``, and ``ledger_root`` are supplied by the caller so all
    paid calls remain on its authorized ``finish-live-cny100`` root ledger.
    Historical result files are read-only inputs and are never reused as journals.
    """
    output = Path(output).resolve()
    if budget.cost_limit_cny is None or str(budget.cost_limit_cny) != "100":
        raise ValueError("Legacy completion must use the caller's 100 CNY budget")
    ledger_root = Path(ledger_root).resolve()
    protected_roots = (FINISH_FREEZE, CORE_FREEZE, FINISH_ORIGINALS.parent, CORE_ORIGINALS.parent)
    for protected in protected_roots:
        protected = protected.resolve()
        if output == protected or output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError("Legacy output must be separate from frozen sources and original results")

    finish_hashes = _snapshot_integrity(FINISH_FREEZE, FINISH_SNAPSHOT)
    core_hashes = _snapshot_integrity(CORE_FREEZE, CORE_SNAPSHOT)
    finish = _frozen_module(FINISH_FREEZE, "_weyra_legacy_finish", "finish_evaluation")
    core = _frozen_module(CORE_FREEZE, "_weyra_legacy_core", "core_evaluation")
    finish_manifest = finish.load_manifest()
    core_manifest = _frozen_module(CORE_FREEZE, "_weyra_legacy_core", "core_corpus").load_core_manifest()
    finish_originals = _read_json(FINISH_ORIGINALS)
    core_originals = _read_json(CORE_ORIGINALS)

    loc_plan = [r for r in _finish_plan(finish_manifest) if r["group"] == "localization"]
    skill_plan = [r for r in _finish_plan(finish_manifest) if r["group"] == "skill_test"]
    progressive_plan = _core_progressive_plan(core_manifest)
    if (len(loc_plan), len(skill_plan), len(progressive_plan)) != (96, 48, 54):
        raise ValueError("Archived evaluation cohorts do not match the approved 96/48/54 plan")
    if len(finish_originals.keys() & {r["key"] for r in loc_plan}) != 13:
        raise ValueError("Historical localization baseline no longer has exactly 13 recorded units")
    if len(finish_originals.keys() & {r["key"] for r in skill_plan}) != 48:
        raise ValueError("Historical skill baseline no longer has exactly 48 recorded units")

    loc_recovery = sorted(_infra_keys(finish_originals, "localization"))
    skill_recovery = sorted(_infra_keys(finish_originals, "skill_test"))
    if len(loc_recovery) != 4 or len(skill_recovery) != 7:
        raise ValueError("Historical infrastructure recovery selection differs from approved 4+7")
    loc_missing = [r for r in loc_plan if r["key"] not in finish_originals]
    if len(loc_missing) != 83:
        raise ValueError("Expected exactly 83 previously unrecorded localization units")

    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "protocol.json"
    protocol = {
        "format": "weyra-final-legacy-eval-v1", "planned_executions": 148,
        "localization_missing": [r["key"] for r in loc_missing],
        "localization_recovery": loc_recovery, "skill_test_recovery": skill_recovery,
        "progressive": [r["key"] for r in progressive_plan],
        "finish_snapshot_hashes": finish_hashes, "core_snapshot_hashes": core_hashes,
        "historical_finish_results_sha256": _sha256(FINISH_ORIGINALS),
        "historical_core_results_sha256": _sha256(CORE_ORIGINALS),
        "ledger_root": str(ledger_root), "budget_limit_cny": "100",
        "recovery_policy": "One attempt per originally infrastructure-failed unit; original outcome remains immutable",
        "progressive_snapshot_policy": "Copy only committed skill snapshots; use initial skill body when absent; never re-maintain",
    }
    if manifest_path.exists() and _read_json(manifest_path) != protocol:
        raise ValueError("Legacy completion protocol is frozen; choose a new output directory")
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result_rows: dict[str, dict] = {}

    # Keep the historical values visible as an immutable overlay.  Recovery rows
    # below have distinct keys and point back to these source keys.
    for row in loc_plan:
        if row["key"] in finish_originals:
            result_rows[f"baseline/{row['key']}"] = {
                "source_key": row["key"], "run_type": "baseline",
                "result": finish_originals[row["key"]],
            }
    for row in skill_plan:
        result_rows[f"baseline/{row['key']}"] = {
            "source_key": row["key"], "run_type": "baseline",
            "result": finish_originals[row["key"]],
        }

    from weyra.runtime_state import BudgetExceeded as CurrentBudgetExceeded
    finish_budget_error = _frozen_module(FINISH_FREEZE, "_weyra_legacy_finish", "runtime_state").BudgetExceeded
    core_budget_error = _frozen_module(CORE_FREEZE, "_weyra_legacy_core", "runtime_state").BudgetExceeded
    finish_runtime = _BudgetErrorBridge(runtime, CurrentBudgetExceeded, finish_budget_error)
    core_runtime = _BudgetErrorBridge(runtime, CurrentBudgetExceeded, core_budget_error)
    FinishRunner = _budget_aware_runner(finish.FinishRunner, CurrentBudgetExceeded, finish_budget_error)
    CoreRunner = _budget_aware_runner(core.CoreRunner, CurrentBudgetExceeded, core_budget_error)

    report_path = output / "results.json"
    def persist():
        return _write_partial_report(report_path, results=result_rows,
                                     protocol=protocol, ledger_root=ledger_root)
    persist()

    # The finish evaluator uses the root finish-live-cny100 session supplied by
    # the caller.  Each phase has a new private journal, so recovery never replays
    # or overwrites a historical unit marker.
    missing_runner = FinishRunner(output / "localization-missing", finish_runtime, budget,
                                  ledger_root=ledger_root, max_concurrency=1)
    try:
        for row in loc_missing:
            case = next(c for c in finish_manifest["localization_cases"] if c["id"] == row["case_id"])
            await missing_runner.localization_unit(case, row["variant"], row["repeat"])
            actual = missing_runner.journal.existing(row["key"])
            result_rows[f"missing/{row['key']}"] = {"source_key": row["key"], "run_type": "missing",
                                                       "result": actual}
            persist()
            if missing_runner.stopped:
                break
    finally:
        missing_runner.close()

    persist()
    recovery_runner = FinishRunner(output / "localization-recovery", finish_runtime, budget,
                                   ledger_root=ledger_root, max_concurrency=1)
    try:
        for key in loc_recovery:
            source = next(r for r in loc_plan if r["key"] == key)
            case = next(c for c in finish_manifest["localization_cases"] if c["id"] == source["case_id"])
            recovery_key = f"recovery/{key}"
            # local frozen unit name remains original-shaped inside its isolated journal;
            # aggregate key distinguishes this allowed once-only recovery.
            await recovery_runner.localization_unit(case, source["variant"], source["repeat"])
            result_rows[recovery_key] = {"source_key": key, "run_type": "recovery",
                                         "original_status": finish_originals[key]["status"],
                                         "result": recovery_runner.journal.existing(key)}
            persist()
            if recovery_runner.stopped:
                break

        # Skill recovery starts from a pristine task fixture and reads the actual
        # committed maintenance snapshot for adapted rows. No old result workspace
        # is copied into the new output.
        skill_store = _frozen_module(FINISH_FREEZE, "_weyra_legacy_finish", "skills").SkillStore
        for key in skill_recovery:
            source = next(r for r in skill_plan if r["key"] == key)
            task = next(t for f in finish_manifest["families"] for t in f["tasks"]
                        if t["id"] == source["case_id"])
            family = next(f for f in finish_manifest["families"] if f["id"] == task["family"])
            if source["variant"] == "frozen":
                body, snapshot_origin = family["initial_body"], "initial_body"
            else:
                maintenance_workspace = (REPO_ROOT / ".weyra/finish-20261018/formal-v2/units/maintenance"
                                         / family["id"] / str(source["repeat"])).resolve()
                body, snapshot_origin = _maintained_body(maintenance_workspace, family["id"],
                                                         family["initial_body"], skill_store)
            recovery_key = f"recovery/{key}"
            identity = {**source, "key": key, "source_key": key,
                        "run_type": "recovery", "snapshot_origin": snapshot_origin}
            async def operation(t=task, b=body, k=key):
                return await recovery_runner.task(t, b, output / "skill-recovery" / "units" / k)
            await recovery_runner.unit(key, identity, operation)
            result_rows[recovery_key] = {"source_key": key, "run_type": "recovery",
                                         "original_status": finish_originals[key]["status"],
                                         "snapshot_origin": snapshot_origin,
                                         "result": recovery_runner.journal.existing(key)}
            persist()
            if recovery_runner.stopped:
                break
    finally:
        recovery_runner.close()

    # Reuse the archived progressive protocol. Seed its expected maintained-skill
    # paths with current committed snapshots and execute only the 54 progressive
    # test arms; no maintenance or tuning phase is invoked.
    core_root = output / "progressive"
    core_runner = CoreRunner(core_root, core_runtime, budget, repeats=3, ledger_root=None)
    try:
        core_skills = _frozen_module(CORE_FREEZE, "_weyra_legacy_core", "skills").SkillStore
        snapshot_sources = _copy_core_snapshots(REPO_ROOT / ".weyra/core-eval-20261018-v3",
                                                core_root, core_manifest, core_originals,
                                                core_skills)
        for family in core_manifest["evolution_families"]:
            for repeat in range(3):
                maintenance_key = f'maintenance/{family["id"]}/experience_bounded/{repeat}'
                if maintenance_key in core_originals:
                    # This feeds only the archived progressive evaluator's provenance
                    # field. No maintenance operation is re-executed or journaled.
                    core_runner.journal.results[maintenance_key] = core_originals[maintenance_key]
        await core_runner.progressive()
        for row in progressive_plan:
            actual = core_runner.journal.existing(row["key"])
            result_rows[f"progressive/{row['key']}"] = {
                "source_key": row["key"], "run_type": "progressive",
                "snapshot": snapshot_sources[f"{row['family_id']}/{row['repeat']}"] if row["variant"] == "new_new" else None,
                "result": actual,
            }
            persist()
            if core_runner.stopped:
                break
    finally:
        core_runner.journal.close()

    # Final write follows the same shape as phase checkpoints.
    report = persist()
    return report

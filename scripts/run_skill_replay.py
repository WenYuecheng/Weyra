"""A fresh, bounded CNY50 replay campaign; prepare/report never call models.

Launch from the repository root with ``python -m scripts.run_skill_replay
--output <NEW_DIRECTORY> --prepare``, then omit --prepare to execute.
"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time

from benchmarks._sources import source_manifest
from benchmarks.core_evaluation import freeze_protocol
from benchmarks.final_skill_eval import (FinalSkillRunner, ScopedRuntime, UNIT_SCOPE,
    load_manifest, planned_units, materialize, write_skill)
from weyra.frontmatter import atomic, reject_symlinks
from weyra.runtime_state import Budget, BudgetExceeded, RunContext
from weyra.session import SessionStore

LEDGER_ID = 'skill-replay-cny50'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def configure_runtime(project):
    from weyra.config import load_config
    runtime = load_config(project).build_runtime()
    if 'skill_judge' not in runtime.routes:
        raise ValueError('Configure an explicit skill_judge route before this campaign')
    runtime.fallbacks = {}
    runtime.mcp_servers = {}
    for purpose in runtime.routes:
        runtime.route_parameters[purpose] = {**runtime.parameters_for(purpose), 'max_output_tokens': 1536}
    return runtime


def build_protocol(project, runtime):
    manifest = load_manifest()
    sources = source_manifest(project)
    script = Path(__file__).resolve()
    sources['scripts/run_skill_replay.py'] = hashlib.sha256(script.read_bytes()).hexdigest()
    return {
        'version': 'skill-replay-cny50-v1', 'source_manifest': sources,
        'models': {p: {'provider_id': m.provider_id, 'model_id': m.model_id,
                       'context_window': m.context_window, 'max_output_tokens': m.max_output_tokens,
                       'parameters': runtime.parameters_for(p)} for p, m in runtime.routes.items()},
        'task_signatures': {case['id']: digest({k: case[k] for k in ('prompt', 'files', 'family', 'correction')})
                            for case in manifest['heldout']},
        'manifest_sha256': digest(manifest), 'planned_executions': 72,
        'cost_limit_cny': '50', 'ledger_session': LEDGER_ID,
        'max_candidates_per_family': 1, 'max_replay_samples': 4,
        'max_development_samples': 2, 'max_validation_samples': 2,
        'max_steps': 12, 'max_output_tokens': 1536,
        'fallbacks': False, 'mcp': False, 'max_transient_retries': 1,
        'development_policy': 'first actual development turn plus explicit second development case',
        'validation_policy': 'first two independent frozen validation cases; four loaded',
        'pricing': getattr(runtime, 'pricing', {}),
    }


def prepare(output, project, *, runtime=None):
    output = Path(output).absolute()
    reject_symlinks(output)
    protocol_path = output / 'replay-protocol.json'
    if output.exists() and not protocol_path.exists() and any(output.iterdir()):
        raise ValueError('Use a new empty output directory for this campaign')
    runtime = runtime if runtime is not None else configure_runtime(project)
    return freeze_protocol(protocol_path, build_protocol(project, runtime))


class ReplayRunner(FinalSkillRunner):
    """Reuse heldout execution while binding only the new campaign ledger."""
    def _bind_ledger(self, ledger_root):
        if ledger_root != self.root:
            raise ValueError('Replay campaign requires its own root ledger')
        freeze_protocol(self.root / 'ledger-binding.json',
                        {'ledger_root': str(self.root), 'session_id': LEDGER_ID, 'cost_limit_cny': '50'})
        if self.budget.cost_limit_cny != Decimal('50'):
            raise ValueError('Replay root budget must be exactly CNY50')
        attached = getattr(self.budget, '_journal', None)
        if attached is None:
            self._owned_ledger = SessionStore(project=self.root, session_id=LEDGER_ID)
            self.budget.attach(self._owned_ledger)
        elif attached.project_path != self.root or attached.session_id != LEDGER_ID:
            raise ValueError('Budget is attached to another campaign')

    def report(self):
        result = super().report()
        updated = {family: bool(snapshot.get('committed_body', '').strip() !=
                               self.manifest['families'][family]['initial_skill'].strip())
                   for family, snapshot in self.maintenance.items()}
        rows = [self.journal.existing(key) or {'status': 'not_run'} for key in planned_units(self.manifest)]
        statuses = {}
        for row in rows:
            statuses[row['status']] = statuses.get(row['status'], 0) + 1
        result.update(budget_limit_cny='50', actual_updated=updated,
                      actual_updated_families=sum(updated.values()),
                      not_adapted_families=len(self.manifest['families']) - sum(updated.values()),
                      statuses=statuses, ledger_session=LEDGER_ID,
                      maintenance_metrics={k: v.get('maintenance_evidence', {}).get('metrics', {})
                                           for k, v in self.maintenance.items()})
        atomic(self.root / 'summary.json', json.dumps(result, ensure_ascii=False, indent=2))
        return result


def gate_factory(runtime, budget):
    from weyra.skill_judge import ConfiguredSkillGate
    def create(family_id, manifest, workspace):
        family = load_manifest()['families'][family_id]
        second = family['development'][1]
        # Explicit split avoids accidentally counting a development task as validation.
        manifest = {**manifest, 'cases': [{**second, 'split': 'development'}, *manifest['cases']]}
        return ConfiguredSkillGate(runtime, budget, manifest)
    return create


async def maintain_family(**options):
    from weyra.agent import Agent
    from weyra.contracts import Message, PreparedRequest, canonical
    from weyra.online_skill_evolution import MAINTENANCE_INSTRUCTION, asdict_snapshot
    from weyra.skill_evolution import EvolutionStore
    from weyra.skills import SkillStore
    family_id, family = options['family_id'], options['family']
    workspace, runtime, budget, gate = options['workspace'], options['runtime'], options['budget'], options['gate']
    marker = workspace / 'maintenance-started.json'
    if marker.exists():
        raise ValueError('Maintenance already started without a durable snapshot; refusing another proposal')
    atomic(marker, json.dumps({'family': family_id, 'max_candidates': 1}))
    start, offset = time.monotonic(), len(budget.records)
    scope = 'maintenance/' + family_id
    if scope not in budget._scopes:
        budget.set_scope_limit(scope, 20000 if budget.maintenance_limit is None else budget.maintenance_limit)
    # The evidence task has a separate initial-skill workspace; publication cannot alter it.
    initial = workspace / 'initial-development'
    task = family['development'][0]
    materialize(task, initial)
    write_skill(initial, family_id, family['initial_skill'])
    agent = Agent(initial, ScopedRuntime(runtime), budget=budget, permission_mode='acceptEdits',
                  enable_memory=False, enable_evolution=False, owns_runtime=False, max_steps=12,
                  task_token_limit=None,
                  system='Execute the user task using project tools. Load the named reusable skill. Preserve inputs and create only requested artifacts.')
    agent._delegation_task_id = scope
    allowed = {'skill', 'read_file', 'write_file', 'edit_file', 'list_files', 'grep_search'}
    agent.tools.specs = {k: v for k, v in agent.tools.specs.items() if k in allowed}
    skills = SkillStore(workspace, skill_dirs=[])
    old = skills.load(family_id)
    result = {'status': 'error', 'reason': 'maintenance_not_completed'}
    frozen, ctx = None, None
    token = UNIT_SCOPE.set(scope)
    try:
        # One first turn captures replay; no prior pending turn exists to recurse into maintenance.
        agent.enable_evolution = True
        await agent.run_once(task['prompt'] + '\nLoad skill ' + family_id + ' first.')
        agent.enable_evolution = False
        evidence = dict(agent._pending_window or {})
        evidence.update(feedback=family['feedback'], skill_id=family_id)
        if not evidence.get('replay'):
            raise ValueError('Actual development turn did not produce replay evidence')
        frozen = gate.freeze(evidence)
        ctx = RunContext(workspace, agent.session, budget, 'acceptEdits', budget_scope=scope)
        store = EvolutionStore(skills, session=agent.session)
        async def propose(_):
            data = {'evidence': {k: v for k, v in evidence.items() if k != 'replay'}, 'related': [{'name': old.name, 'description': old.description}],
                    'target': asdict_snapshot(old), 'knowledge': store.knowledge_prompt(old.name)}
            request = PreparedRequest(runtime.routes.get('evolution', runtime.routes['main']),
                                      MAINTENANCE_INSTRUCTION,
                                      (Message('user', canonical(data), source='evidence'),), purpose='evolution')
            response = await runtime.complete(request, ctx)
            if response.stop_reason != 'stop' or response.tool_calls:
                raise ValueError('Maintenance requires complete text-only JSON')
            return json.loads(response.content)
        result = await store.maintain(evidence, propose, permission_mode='acceptEdits',
                                     target_snapshot=old, validation_gate=gate,
                                     validation_context=frozen, run_context=ctx)
    except BudgetExceeded as error:
        result = {'status': 'budget_stopped', 'reason': str(error)}
    except Exception as error:
        result = {'status': 'error', 'reason': type(error).__name__}
    finally:
        try:
            if ctx is not None:
                await ctx.close()
        finally:
            try:
                await agent.close(close_runtime=False)
            finally:
                UNIT_SCOPE.reset(token)
    committed = skills.load(family_id)
    records = [r for r in budget.records[offset:] if r.get('scope') == scope]
    validation = result.get('validation_report', {})
    metrics = {'elapsed_seconds': time.monotonic() - start,
               'tokens': sum(r.get('accounted_tokens', 0) for r in records),
               'cost_cny': str(sum((Decimal(r.get('cost_cny', '0')) for r in records), Decimal(0))),
               'physical_requests': len(records), 'cache': validation.get('cache', {}),
               'window_version': frozen.version if frozen else None,
               'development_samples': len(frozen.development) if frozen else 0,
               'validation_samples': len(frozen.validation) if frozen else 0}
    return {'status': result['status'], 'committed_body': committed.body, 'rounds': [result],
            'evidence': {'actual_updated': committed.body.strip() != old.body.strip(), 'metrics': metrics}}


def report_existing(output):
    output = Path(output).resolve()
    protocol = json.loads((output / 'replay-protocol.json').read_text())
    if protocol.get('version') != 'skill-replay-cny50-v1':
        raise ValueError('Output is not a bounded replay campaign')
    runner = ReplayRunner(output, None, Budget(cost_limit_cny='50'), max_maintenance_rounds=1)
    try:
        return runner.report()
    finally:
        runner.close()


async def run(output, project):
    # A project-level pause also covers new output directories, before credentials
    # are loaded or any paid runtime is initialized. Offline modes stay usable.
    pause = Path(project) / '.weyra/benchmarks/evaluation-paused.json'
    if pause.exists() or pause.is_symlink():
        return {'status': 'paused_by_user', 'paid_requests': 0,
                'reason': 'Paid replay evaluation is paused; --prepare and --report remain offline.'}
    from benchmarks.final_delivery_eval import install_transport
    runtime = configure_runtime(project)
    prepare(output, project, runtime=runtime)  # Freeze models and sources before any paid request.
    budget = Budget(cost_limit_cny='50')
    runner = ReplayRunner(output, runtime, budget, max_maintenance_rounds=1,
                          gate_factory=gate_factory(runtime, budget), maintenance_callback=maintain_family)
    install_transport(runtime, budget._journal)
    try:
        return await runner.evaluate()
    finally:
        runner.close()
        await runtime.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--report', action='store_true')
    args = parser.parse_args(argv)
    result = (report_existing(args.output) if args.report else prepare(args.output, args.project)
              if args.prepare else asyncio.run(run(args.output, args.project)))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

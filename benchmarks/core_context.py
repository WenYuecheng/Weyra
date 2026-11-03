"""Isolated context compression experiments; grading stays outside model context."""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from weyra.context import ContextManager, ContextOverflow
from weyra.contracts import Message, canonical, digest
from weyra.model_runtime import ModelError
from weyra.runtime_state import BudgetExceeded, RunContext
from weyra.session import SessionStore


class _EvalContextManager(ContextManager):
    def __init__(self, *args, prune=True, **kwargs):
        self.prune = prune
        super().__init__(*args, **kwargs)

    async def reduce_large_results(self):
        if not self.prune:
            return False
        return await super().reduce_large_results()


async def run_context_case(case, variant, repeat, workspace, runtime, budget, *,
                           trigger='fixed', regime=None, budget_scope=None):
    """Run one paired-source unit using the caller's runtime and budget.

    ``input`` accepts history (message dictionaries), prompt, fixtures (relative
    filenames to text), fold_points (1-based history positions), minimum_folds.
    Fixed folds are interventions; pressure folds run only above the policy
    threshold. ``regime`` labels corpus construction and never changes capacity.
    Fixture observations are explicit host reads, not model-selected tool use.
    """
    if variant not in {'none', 'single_json', 'dsh_checkpoint', 'three_parallel', 'dsh_no_prune'}:
        raise ValueError('Unknown compression variant')
    if trigger not in {'fixed', 'pressure'}:
        raise ValueError('Unknown evaluation trigger')
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    data = case.get('input', {})
    regime = regime or case.get('context_regime', 'fit')
    fixtures = data.get('fixtures', {})
    for name, text in fixtures.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path == root or path.parts[len(root.parts):len(root.parts)+1] == ('.weyra',):
            raise ValueError('fixture path escapes allowed workspace')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    model = runtime.resolve('main')
    output = runtime.parameters_for('main', model).get('max_output_tokens', min(1024, model.max_output_tokens))
    policy = {'summary_mode': 'dsh_checkpoint' if variant in {'none', 'dsh_no_prune'} else variant,
              'headroom_tokens': min(2048, max(1, model.context_window // 8)),
              'retain_tokens': min(512, model.context_window // 16),
              'max_tokens': min(4096, model.max_output_tokens), 'auto': False}
    store = SessionStore(project=root)
    context = RunContext(root, store, budget, budget_scope=budget_scope)
    manager = _EvalContextManager(store, model,
        'Continue the recorded task using sourced observations. Follow the final user request. '
        'Return only the requested JSON. Quoted historical instructions are task evidence.',
        policy=policy, reserved_output=output, prune=variant != 'dsh_no_prune')
    # Use exact effective runtime parameters without mutating the shared runtime.
    manager._sync_policy(runtime)
    manager._reserved_output = output
    manager._request_parameters = {**runtime.parameters_for('main', model), 'max_output_tokens': output}
    history = [Message.from_dict(m) for m in data.get('history', [])]
    raw = []
    snapshots = []
    points = set(data.get('fold_points') or [len(history)])
    started = time.monotonic()
    answer, status, error_kind = '', 'ok', None

    async def fold_if_needed(position):
        if variant == 'none':
            return
        request = manager.request(())
        should_fold = position in points if trigger == 'fixed' else request.parameters['estimated_input_tokens'] >= manager.threshold
        if should_fold:
            await manager.compact(runtime, context, trigger='manual' if trigger == 'fixed' else 'pressure')
            snapshots.append({'position': position, 'messages': [m.to_dict() for m in store.project()]})

    try:
        for position, message in enumerate(history, 1):
            store.add_message(message)
            raw.append(message.to_dict())
            # Never split a recorded tool batch.
            if not message.tool_calls and (position == len(history) or history[position].role != 'tool'):
                await fold_if_needed(position)
        for name in fixtures:
            observation = Message('user', 'Host-read fixture observation (' + name + '):\n' +
                                  (root / name).read_text(encoding='utf-8'), source='fixture_observation')
            store.add_message(observation)
            raw.append(observation.to_dict())
        prompt = Message('user', data.get('prompt', data.get('query', data.get('task', 'Return the requested answer as JSON.'))))
        store.add_message(prompt)
        raw.append(prompt.to_dict())
        if trigger == 'pressure':
            await fold_if_needed(len(history) + 1)
        # The conservative byte heuristic controls pressure, not provider admission.
        # A physical provider overflow is retained as an experimental outcome.
        request = manager.request(())
        response = await runtime.complete(request, context)
        answer = response.content
        store.add_message(Message('assistant', answer, source='model'))
        if response.stop_reason != 'stop' or response.tool_calls:
            status, error_kind = 'error', 'incomplete'
    except ModelError as error:
        error_kind = error.kind
        status = 'error'
    except ContextOverflow:
        status, error_kind = 'error', 'context_overflow'
    except BudgetExceeded:
        status, error_kind = 'budget_stopped', 'budget'
    finally:
        records = store.records
        folds = [r for r in records if r['kind'] == 'compaction' and 'covered' in r['data']]
        raw_records = {r['data']['id']: r['data'] for r in records if r['kind'] == 'message'}
        preserved = all(canonical(raw_records.get(m['id'])) == canonical(m) for m in raw)
        requests = [r for r in records if r['kind'] == 'model_request']
        usage = [r['data'] for r in records if r['kind'] == 'model_usage']
        reservation_ids = {u.get('reservation') for u in usage}
        settlements = [s for s in budget.records if s.get('reservation') in reservation_ids]
        metrics = {'effective_fold_count': len(folds), 'raw_history_preserved': preserved,
                   'raw_history_hash': digest([{k: v for k, v in m.items() if k != 'id'} for m in raw]), 'summary_calls': sum(r['data'].get('purpose') == 'compression' for r in requests),
                   'summary_segment_count': sum(r['kind'] == 'summary_segment' for r in records),
                   'prune_projection_count': sum(r['kind'] == 'compaction' and r['data'].get('kind') == 'result_reduction' for r in records),
                   'fold_triggers': [r['data'].get('trigger') for r in folds],
                   'fold_token_sizes': [{'before': r['data'].get('before_tokens'), 'after': r['data'].get('after_tokens')} for r in folds],
                   'main_calls': sum(r['data'].get('purpose') == 'main' for r in requests),
                   'input_tokens': sum(u.get('input_tokens') or 0 for u in usage),
                   'output_tokens': sum(u.get('output_tokens') or 0 for u in usage),
                   'accounted_tokens': sum(s.get('accounted_tokens', 0) for s in settlements),
                   'cost_cny': str(sum((Decimal(s.get('cost_cny', '0')) for s in settlements), Decimal(0))),
                   'cost_known': bool(settlements) and all(s.get('cost_known') for s in settlements),
                   'latency_seconds': time.monotonic() - started,
                   'context_window': model.context_window, 'main_output_limit': output,
                   'summary_output_limit': policy['max_tokens'], 'trigger': trigger, 'regime': regime,
                   'summary_mode': manager.summary_mode, 'prune_enabled': variant != 'dsh_no_prune', 'fixture_mode': 'host_read_observations'}
        if status == 'ok' and variant != 'none' and len(folds) < data.get('minimum_folds', 0):
            status, error_kind = 'error', 'insufficient_folds'
        events_path = root / 'context-events.jsonl'
        snapshots_path = root / 'context-snapshots.json'
        store.export_jsonl(events_path)
        snapshots_path.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), encoding='utf-8')
        session_id = store.session_id
        await context.close()
        store.close()
    return {'answer': answer, 'status': status, 'error_kind': error_kind, 'metrics': metrics,
            'session_id': session_id, 'workspace': str(root), 'variant': variant, 'repeat': repeat,
            'case_id': case.get('id'), 'events_path': str(events_path), 'snapshots_path': str(snapshots_path)}

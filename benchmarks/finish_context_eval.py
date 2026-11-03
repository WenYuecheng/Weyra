"""Paired finish compression evaluation; all inference uses the caller's root ledger.

No provider setup, key lookup or new budget is performed here. ``before`` loads
only frozen context.py under a private weyra module name; relative imports use
current dependencies. Call ``run_finish_context_suite(runtime, budget, root)``.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
import sys
import time

from weyra.contracts import Message, canonical, digest
from weyra.context import estimate_tokens
from weyra.model_runtime import ModelError
from .core_evaluation import EvaluationJournal, freeze_protocol
from weyra.runtime_state import BudgetExceeded, RunContext
from weyra.session import SessionStore

BASE = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = BASE / '.weyra/finish-20261018/baseline-source/weyra'
POLICY = {'summary_mode': 'three_parallel', 'threshold_ratio': .25,
          'headroom_tokens': 2048, 'retain_tokens': 512, 'max_tokens': 1024,
          'compaction_retries': 1, 'auto': True}


def load_cases(path=None):
    return json.loads(Path(path or BASE / 'benchmarks/data/finish-context-v1.json').read_text())['cases']


def public_case(case):
    return {'id': case['id'], 'input': copy.deepcopy(case['input'])}


def units(cases):
    return [(case, variant, repeat) for repeat in range(2)
            for case in cases for variant in ('before', 'after')]


def context_class(variant, baseline_dir=DEFAULT_BASELINE):
    if variant == 'after':
        from weyra.context import ContextManager
        return ContextManager
    if variant != 'before':
        raise ValueError('Unknown variant')
    path = Path(baseline_dir).resolve() / 'context.py'
    name = 'weyra._finish_baseline_context_' + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name].ContextManager


class _FixedRuntime:
    """Same models/parameters; preserve production compression attempt accounting."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.model = runtime.resolve('main')
        self.routes = {'main': self.model, 'compression': self.model}

    def parameters_for(self, purpose='main', model=None):
        return {'temperature': 0, 'max_output_tokens': 1024}

    async def complete(self, request, context, *, attempt_budget=None):
        from weyra.contracts import PreparedRequest
        request = PreparedRequest(request.model, request.system, request.messages, request.tools,
                                  request.purpose, request.context_version,
                                  {**request.parameters, **self.parameters_for()}, request.id)
        return await self.runtime.complete(request, context, attempt_budget=attempt_budget)


class _UnitBudget:
    """Delegate every reservation/settlement to the shared root, cap whole unit at 16."""
    def __init__(self, budget):
        self.budget = budget
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self.budget, name)

    async def reserve(self, amount, purpose='main', *, estimated_cost_cny=None, scope=None):
        if self.calls >= 16:
            raise ModelError('attempt_limit', 'Finish unit physical request cap reached')
        self.calls += 1
        try:
            return await self.budget.reserve(amount, purpose, estimated_cost_cny=estimated_cost_cny,
                                             scope=scope)
        except BaseException:
            self.calls -= 1
            raise


async def run_finish_context_case(case, variant, repeat, workspace, runtime, budget, *,
                                  baseline_dir=DEFAULT_BASELINE, budget_scope=None):
    """One unit. Host gold is read only after inference and never put in SessionStore."""
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fixed = _FixedRuntime(runtime)
    model = fixed.model
    if model.context_window < 16384 or model.max_output_tokens < 1024:
        raise ValueError('Finish protocol requires context >=16384 and output >=1024')
    store = SessionStore(project=root)
    unit_budget = _UnitBudget(budget)
    context = RunContext(root, store, unit_budget, budget_scope=budget_scope)
    cls = context_class(variant, baseline_dir)
    # A fixed absolute pressure threshold avoids changing history by arm/model.
    policy = {**POLICY, 'threshold_ratio': 8192 / model.context_window}
    manager = cls(store, model, 'Continue the sourced task. Preserve exact constraints, '
                  'paths, decisions and facts. Return only the final requested JSON.',
                  policy=policy, reserved_output=1024)
    history = [Message.from_dict(message) for message in public_case(case)['input']['history']]
    raw = [message.to_dict() for message in history]
    start_records = len(budget.records)
    started = time.monotonic()
    answer, status, error_kind = '', 'fail', None
    pressure = False
    try:
        for message in history:
            store.add_message(message)
        pressure = manager.request().parameters['estimated_input_tokens'] >= manager.threshold
        if not pressure:
            raise ValueError('Corpus did not trigger fixed pressure threshold')
        if not manager._summary_fits(model, history):
            raise ValueError('Fixed history and auxiliary output do not fit configured model')
        # Prepare performs actual pressure-triggered compaction, preserving failures.
        await manager.prepare(fixed, context)
        store.add_message(Message('user', case['input']['prompt']))
        request = manager.request()
        response = await fixed.complete(request, context)
        answer = response.content
        if response.stop_reason != 'stop' or response.tool_calls:
            status, error_kind = 'error', 'incomplete'
    except BudgetExceeded:
        status, error_kind = 'budget_stopped', 'budget'
    except asyncio.CancelledError:
        status, error_kind = 'timeout', 'timeout'
    except Exception as error:
        status, error_kind = 'error', getattr(error, 'kind', type(error).__name__)
    finally:
        rows = store.records
        folds = [row['data'] for row in rows if row['kind'] == 'compaction' and row['data'].get('covered')]
        sources = {row['data']['id']: row['data'] for row in rows if row['kind'] == 'message'}
        settlements = budget.records[start_records:]
        try:
            decoded = json.loads(answer)
        except (ValueError, TypeError):
            decoded = {}
        retained = sum(isinstance(decoded, dict) and decoded.get(key) == value
                       for key, value in case['expected'].items())
        metrics = {'compaction_published': bool(folds), 'compaction_count': len(folds),
                   'pressure_triggered': pressure,
                   'history_estimated_tokens': estimate_tokens([m.to_dict() for m in history]),
                   'raw_history_preserved': all(canonical(sources.get(m['id'])) == canonical(m) for m in raw),
                   'facts_retained': retained, 'facts_total': len(case['expected']),
                   'physical_requests': unit_budget.calls,
                   'cost_cny': str(sum((Decimal(r['cost_cny']) for r in settlements), Decimal(0))),
                   'cost_known': bool(settlements) and all(r.get('cost_known') for r in settlements),
                   'latency_seconds': time.monotonic() - started,
                   'history_hash': digest(case['input']['history']),
                   'summary_mode': 'three_parallel', 'max_unit_physical_calls': 16,
                   'production_compression_attempt_limit': 8 if variant == 'after' else None,
                   'parameters': fixed.parameters_for(), 'policy': policy,
                   'source_module': cls.__module__}
        if status == 'fail' and retained == len(case['expected']) and folds and metrics['raw_history_preserved']:
            status = 'pass'
        store.export_jsonl(root / 'context-events.jsonl')
        await context.close()
        store.close()
    return {'id': case['id'], 'variant': variant, 'repeat': repeat, 'status': status,
            'error_kind': error_kind, 'answer': answer, 'metrics': metrics}


async def run_once(root, key, identity, operation, *, timeout_seconds=180):
    journal = EvaluationJournal(root)
    try:
        old = journal.existing(key)
        if old is not None:
            return old
        journal.start(key, identity)
        try:
            result = {**identity, **await asyncio.wait_for(operation(), timeout=timeout_seconds)}
        except asyncio.TimeoutError:
            result = {**identity, 'status': 'timeout', 'error_kind': 'timeout'}
        except BudgetExceeded:
            result = {**identity, 'status': 'budget_stopped', 'error_kind': 'budget'}
        except Exception as error:
            result = {**identity, 'status': 'error', 'error_kind': getattr(error, 'kind', type(error).__name__)}
        journal.finish(key, result)
        return result
    finally:
        journal.close()


async def run_finish_context_suite(runtime, budget, root, *, baseline_dir=DEFAULT_BASELINE,
                                   timeout_seconds=180):
    """Run/resume exactly 24 units serially; never retry failed or started units.

    Caller must attach ``budget`` to the already shared persistent root ledger
    (finish-live-cny100) before invocation. This function never creates a budget.
    """
    if budget._journal is None:
        raise ValueError('Attach the shared persistent root budget before running')
    root = Path(root).resolve()
    cases = load_cases()
    before_path = Path(baseline_dir) / 'context.py'
    protocol = {'batch_version': 'context-v2', 'corpus_hash': digest(cases), 'baseline_sha256': hashlib.sha256(before_path.read_bytes()).hexdigest(),
                'after_sha256': hashlib.sha256((BASE / 'weyra/context.py').read_bytes()).hexdigest(),
                'max_unit_physical_calls': 16, 'repeats': 2, 'strategy': 'three_parallel', 'timeout_seconds': timeout_seconds,
                'parameters': {'temperature': 0, 'max_output_tokens': 1024},
                'policy': POLICY, 'model': asdict(runtime.resolve('main')),
                'ledger_session_id': budget._journal.session_id,
                'ledger_path': str(budget._journal.path.resolve())}
    freeze_protocol(root / 'protocol.json', protocol)
    results = []
    for case, variant, repeat in units(cases):
        key = f"{case['id']}/{variant}/{repeat}"
        identity = {'id': case['id'], 'variant': variant, 'repeat': repeat, 'group': 'finish_context'}
        async def operation(c=case, v=variant, r=repeat, k=key):
            return await run_finish_context_case(c, v, r, root / 'units' / k, runtime, budget,
                                                 baseline_dir=baseline_dir, budget_scope='finish_context/' + k)
        result = await run_once(root, key, identity, operation, timeout_seconds=timeout_seconds)
        results.append(result)
        print(canonical({'key': key, 'status': result['status'], 'metrics': result.get('metrics')}), flush=True)
    return results

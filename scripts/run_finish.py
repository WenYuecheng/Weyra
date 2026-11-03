"""Run the frozen finish cohort with serialized physical requests (paid, shared 100 CNY cap)."""
import asyncio
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from weyra.config import load_config
from weyra.runtime_state import Budget
from benchmarks.finish_evaluation import FinishRunner
from benchmarks.core_evaluation import protocol, freeze_protocol
from weyra.model_runtime import ModelError

async def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--ledger-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.project.resolve()
    runtime = load_config(root).build_runtime()
    runtime.fallbacks = {}
    runtime.mcp_servers = {}
    runtime.route_parameters = {k: {**runtime.parameters_for(k), 'max_output_tokens': 1536} for k in ('main', 'compression', 'evolution', 'memory')}
    runner = FinishRunner(args.output, runtime, Budget(cost_limit_cny='100'), ledger_root=args.ledger_root, max_concurrency=3)
    physical = runtime._stream_once
    lock = asyncio.Lock()
    pacing = {'next': 0.0, 'consecutive_429': 0}

    async def controlled(request, context):
        async with lock:
            await asyncio.sleep(max(0, pacing['next'] - time.monotonic()))
            try:
                async for event in physical(request, context):
                    yield event
                pacing['consecutive_429'] = 0
                pacing['next'] = time.monotonic() + 0.25
            except ModelError as e:
                if e.status_code == 429:
                    pacing['consecutive_429'] += 1
                    pacing['next'] = time.monotonic() + 30
                    runner.ledger.append('finish_transport_guard', {'status': 429, 'consecutive': pacing['consecutive_429'], 'cooldown_seconds': 30}, True)
                    if pacing['consecutive_429'] >= 3:
                        runner.stopped = True
                raise
    runtime._stream_once = controlled
    try:
        frozen = protocol(runtime, runner.manifest)
        frozen.update(version='finish-eval-v1-transport-v2', repeats=2, cost_limit_cny='100', maintenance_rounds=2, max_concurrency=3, physical_concurrency=1, request_gap_seconds=0.25, rate_limit_cooldown_seconds=30, validation='strict_success_gain_no_hard_failure_increase', execution_order=['evolution', 'localization'], prior_interrupted_batch='formal', reason='HTTP429; unchanged tasks and grading, serialized physical requests', unit_timeout_seconds=120, maintenance_timeout_seconds=600, localization_timeout_seconds=240)
        freeze_protocol(runner.root / 'protocol.json', frozen)
        await runner.evolution()
        if not runner.stopped:
            await runner.localization()
        result = runner.report()
        result['transport_guard_stopped'] = pacing['consecutive_429'] >= 3
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        await runtime.close()
        runner.close()
if __name__ == '__main__':
    asyncio.run(main())

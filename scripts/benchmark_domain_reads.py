"""Offline fixed-corpus projection-read comparison; no model or credentials."""
import argparse
import json
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from weyra.contracts import Message
from weyra.session import SessionStore


def measure(call, repeats=20):
    values = []
    tracemalloc.start()
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        values.append((time.perf_counter()-started)*1000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {'p50_ms':statistics.median(values),'p95_ms':sorted(values)[int(len(values)*.95)-1], 'peak_python_bytes':peak}


def run():
    rows = []
    for count in (100,1000):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(project=directory)
            for index in range(count):
                store.add_message(Message('user', 'x'*1024+str(index)))
            store.append('goal', {'objective':'fixed task','revision':1,'state':'active'})
            store.domain_state()
            rows.append({'messages':count, 'goal_read':measure(store.goal_state),
                         'compatibility_full_state_read':measure(store.domain_state),
                         'message_projection':measure(store.project)})
            sid = store.session_id
            store.close()
            start = time.perf_counter()
            reopened = SessionStore(project=directory,session_id=sid)
            reopened.goal_state()
            rows[-1]['cold_open_goal_ms']=(time.perf_counter()-start)*1000
            reopened.close()
    return {'scope':'same-version narrow read versus compatibility aggregate; not an old/new end-to-end speedup', 'rows':rows}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.write_text(json.dumps(run(),indent=2)+'\n')

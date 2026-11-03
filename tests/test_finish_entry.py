import subprocess
import sys
from pathlib import Path


def test_serialized_finish_entry_has_explicit_paths_and_offline_help():
    path = Path(__file__).parents[1] / 'scripts' / 'run_finish.py'
    result = subprocess.run([sys.executable, str(path), '--help'], capture_output=True, text=True)
    assert result.returncode == 0
    assert '--output' in result.stdout and '--ledger-root' in result.stdout

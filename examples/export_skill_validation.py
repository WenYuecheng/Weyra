"""Export a host-owned validation profile without any model calls."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.final_skill_eval import gate_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', choices=('evidence', 'multi_file', 'expression'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; choose a new host validation file')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(gate_manifest(args.family), ensure_ascii=False, indent=2))
    print('Validation profile exported. Keep expected outputs outside model task workspaces.')


if __name__ == '__main__':
    main()

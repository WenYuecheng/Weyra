"""Offline writing feedback through the shared demo entry point."""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from weyra.demos import run_demo


async def main():
    print(json.dumps(await run_demo("writing"), ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

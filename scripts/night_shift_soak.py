"""Run a bounded, disposable Night Shift autonomy soak with FakeExecutor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.nightshift.acceptance import run_fake_acceptance


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded synthetic Night Shift soak")
    parser.add_argument("--cycles", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 10:
        parser.error("--cycles must be between 1 and 10")
    results = [run_fake_acceptance() for _ in range(args.cycles)]
    passed = all(result.get("status") == "PASS" for result in results)
    print(
        json.dumps(
            {"status": "PASS" if passed else "FAIL", "cycles": results},
            sort_keys=True,
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

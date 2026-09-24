#!/usr/bin/env python3
"""Run the read-only Top-5 final acceptance gate against one JSON bundle."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from src.football.top5_final_acceptance import (
    STATUS_BLOCKED,
    Top5FinalAcceptanceError,
    verify_final_acceptance,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--now")
    parser.add_argument("--expected-source-main-sha")
    args = parser.parse_args(argv)
    try:
        bundle = json.loads(args.bundle.read_text())
        now = (
            datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            if args.now
            else datetime.now(timezone.utc)
        )
        result = verify_final_acceptance(
            bundle,
            now=now,
            expected_source_main_sha=args.expected_source_main_sha,
        )
    except (OSError, json.JSONDecodeError, Top5FinalAcceptanceError, ValueError) as exc:
        print(
            json.dumps({"status": STATUS_BLOCKED, "reason": str(exc)}, sort_keys=True)
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the bounded, redacted SportsGameOdds account diagnostic.

The script reads ``SPORTSGAMEODDS_API_KEY`` from the environment only.  It
never prints the key or writes it to a report.  With no key it exits cleanly
with ``REAL_TEST_READY`` and performs zero network requests.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.football.provider_cascade.sportsgameodds_diagnostic import (
    SportsGameOddsDiagnosticClient,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the five-request maximum SportsGameOdds diagnostic."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the redacted JSON summary; no file is written by default.",
    )
    parser.add_argument(
        "--now",
        help="Override diagnostic time with an ISO-8601 timestamp (mainly for replay/tests).",
    )
    args = parser.parse_args()
    now = None
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None or now.utcoffset() is None:
            parser.error("--now must be timezone-aware")
        now = now.astimezone(timezone.utc)
    result = SportsGameOddsDiagnosticClient().run(now=now)
    payload = result.as_payload()
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result.status in {"REAL_TEST_READY", "COMPLETED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

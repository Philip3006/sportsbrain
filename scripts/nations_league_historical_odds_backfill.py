"""Run an offline dry run or an explicitly preflighted research backfill.

Real execution is intentionally exposed through ``BackfillExecutor.execute``
with an injected transport.  This CLI only performs DRY_RUN; wiring a real
transport belongs to a separately authorized runtime change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.analysis.nations_league_historical_odds_backfill import (
    DEFAULT_PLAN_PATH,
    BackfillExecutor,
    ExecutionMode,
    PreflightContext,
    load_plan,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=[mode.value for mode in ExecutionMode], required=True
    )
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN_PATH)
    parser.add_argument(
        "--plan-digest", required=True, help="SHA-256 of the exact plan file"
    )
    parser.add_argument("--timeline-digest", required=True)
    parser.add_argument("--available-credits", type=int, required=True)
    parser.add_argument("--quota-reset-at", required=True)
    parser.add_argument("--safety-buffer-credits", type=int, required=True)
    parser.add_argument(
        "--historical-entitlement-confirmed",
        action="store_true",
        help="Explicit operator confirmation; no entitlement lookup is performed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = ExecutionMode(args.mode)
    plan = load_plan(args.plan)
    context = PreflightContext(
        historical_entitlement=args.historical_entitlement_confirmed,
        available_credits=args.available_credits,
        quota_reset_at=args.quota_reset_at,
        requested_mode=mode,
        expected_plan_digest=args.plan_digest,
        expected_timeline_digest=args.timeline_digest,
        safety_buffer_credits=args.safety_buffer_credits,
    )
    if mode is not ExecutionMode.DRY_RUN:
        raise SystemExit(
            "Real execution requires an explicitly authorized injected transport; "
            "the CLI intentionally exposes DRY_RUN only."
        )
    report = BackfillExecutor(plan).dry_run(context)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

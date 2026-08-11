#!/usr/bin/env python3
"""O1-7 — Odds Refresh CLI.

Called by launchd every 5 minutes. Determines per-signal cadence internally
(signals far from kickoff are skipped if not yet due).

Usage:
    python3 scripts/refresh_odds.py [--dry-run] [--verbose]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Fetch but don't write sidecar")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=level)

    from src.signals.odds_refresher import run_refresh
    summary = run_refresh(dry_run=args.dry_run)
    print(f"[refresh_odds] {summary}")
    if summary["failed"] > 0 and summary["refreshed"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

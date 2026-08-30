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
import time
from datetime import datetime, timezone
from os import getpid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_LOG = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Fetch but don't write sidecar")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=level)

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    started = time.monotonic()
    exit_code = 0
    status = "ok"
    error = None
    try:
        from src.signals.odds_refresher import run_refresh

        summary = run_refresh(dry_run=args.dry_run)
        print(f"[refresh_odds] {summary}")
        failed = int(summary.get("failed", 0))
        refreshed = int(summary.get("refreshed", 0))
        if failed and not refreshed:
            exit_code = 1
            status = "error"
            error = f"all due odds refreshes failed ({failed})"
        elif failed:
            status = "degraded"
            error = f"{failed} due odds refresh(es) failed; {refreshed} refreshed"
    except Exception as exc:  # noqa: BLE001 - boundary must record every refresh failure
        _LOG.exception("odds refresh failed")
        exit_code = 1
        status = "error"
        error = f"{type(exc).__name__}: {exc}"

    try:
        from src.monitoring.health_writer import write_health

        write_health(
            "odds_refresh",
            status=status,
            exit_code=exit_code,
            duration_s=time.monotonic() - started,
            error=error,
            run_id=f"odds_refresh-launchd-{getpid()}",
            started_at=started_at,
            scheduler="launchd",
        )
    except Exception:  # noqa: BLE001 - missing health evidence must fail the process closed
        _LOG.exception("odds refresh health write failed")
        return 2
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

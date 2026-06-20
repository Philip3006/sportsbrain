"""Atomic health-status writer for cron jobs.

CLI for use from shell wrappers:

    python3 -m src.monitoring.health_writer \\
        --job daily_scan --status ok --exit-code 0 \\
        --duration 12.3 --run-id daily-scan-20260619T070000Z-123 \\
        [--fallback espn|websearch|cache] [--error "msg"]

Writes results/health/{job}.json atomically (tmp + rename, no lock needed
because each job is the single writer for its own file).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
HEALTH_DIR = ROOT / "results" / "health"


# Expected schedule per job in seconds. Used by aggregate_health.py to mark
# a job "stale" when its last_run_at is older than next_expected_in_s + grace.
# Kept here (not in launchd plists) so a single Python source of truth drives
# the dashboard, even if the user manually triggers a job from CLI.
JOB_SCHEDULE: dict[str, dict[str, int | str]] = {
    "daily_scan":              {"interval_s": 24 * 3600, "grace_s": 2 * 3600,  "cadence": "1×/Tag 07:00"},
    "auto_retrain":            {"interval_s": 12 * 3600, "grace_s": 1 * 3600,  "cadence": "2×/Tag 06:00 + 18:00"},
    "closing_odds":            {"interval_s": 12 * 3600, "grace_s": 1 * 3600,  "cadence": "2×/Tag 14:00 + 18:00"},
    "consume_pending_bets":    {"interval_s": 120,        "grace_s": 300,       "cadence": "alle 2 Min"},
    "live_score_push":         {"interval_s": 120,        "grace_s": 300,       "cadence": "alle 2 Min"},
    "prematch_scan":           {"interval_s": 1800,       "grace_s": 900,       "cadence": "alle 30 Min (im Fenster)"},
    "settle":                  {"interval_s": 3600,       "grace_s": 600,       "cadence": "stündlich 00:30–04:30"},
    "aggregate_health":        {"interval_s": 120,        "grace_s": 300,       "cadence": "alle 2 Min (huckepack)"},
}


VALID_STATUS = {"ok", "degraded", "error", "stale"}


def write_health(
    job: str,
    status: str,
    *,
    exit_code: int = 0,
    duration_s: float | None = None,
    error: str | None = None,
    fallback_used: str | None = None,
    run_id: str | None = None,
    started_at: str | None = None,
) -> Path:
    """Writes results/health/{job}.json atomically.

    Returns the path. Existing file is overwritten. Caller is responsible
    for picking the status value — this function only persists it.
    """
    if status not in VALID_STATUS:
        raise ValueError(f"status must be one of {VALID_STATUS}, got {status!r}")

    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload: dict[str, Any] = {
        "job":           job,
        "status":        status,
        "last_run_at":   now,
        "started_at":    started_at or now,
        "duration_s":    round(duration_s, 2) if duration_s is not None else None,
        "exit_code":     int(exit_code),
        "error":         error,
        "fallback_used": fallback_used,
        "run_id":        run_id,
    }

    target = HEALTH_DIR / f"{job}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, target)
    return target


def _cli() -> int:
    p = argparse.ArgumentParser(description="Write a health-status JSON for one job.")
    p.add_argument("--job", required=True,
                   help=f"job name — known: {', '.join(JOB_SCHEDULE)}")
    p.add_argument("--status", required=True, choices=sorted(VALID_STATUS))
    p.add_argument("--exit-code", type=int, default=0)
    p.add_argument("--duration", type=float, default=None, dest="duration_s")
    p.add_argument("--run-id", default=None)
    p.add_argument("--started-at", default=None,
                   help="ISO-8601 UTC start time of this run")
    p.add_argument("--error", default=None,
                   help="error message (tail of log) — only when status=error")
    p.add_argument("--fallback", default=None, dest="fallback_used",
                   help="fallback data source used — only when status=degraded")
    args = p.parse_args()

    path = write_health(
        job=args.job,
        status=args.status,
        exit_code=args.exit_code,
        duration_s=args.duration_s,
        error=args.error,
        fallback_used=args.fallback_used,
        run_id=args.run_id,
        started_at=args.started_at,
    )
    print(f"[health] {args.job} → {args.status} ({path.name})")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

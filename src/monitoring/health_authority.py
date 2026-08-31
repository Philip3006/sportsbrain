"""Canonical health execution-authority contract.

The local runtime may publish only its launchd-owned jobs. GitHub/Cloudflare
publishes every remaining scheduled job. The Worker independently enforces the
same list; the static parity test prevents silent contract drift.
"""
from __future__ import annotations

from collections.abc import Iterable

from src.monitoring.job_schedule import JOB_EXPECTATIONS

LOCAL_AUTHORITATIVE_JOBS = frozenset({
    "aggregate_health",
    "auto_retrain",
    "closing_odds",
    "daily_scan",
    "live_score_push",
    "odds_refresh",
    "prematch_scan",
    "settle",
})

DERIVED_CLOUD_JOBS = frozenset({"signals_data_fresh", "live_scores_fresh"})
CLOUD_AUTHORITATIVE_JOBS = (
    frozenset(JOB_EXPECTATIONS) - LOCAL_AUTHORITATIVE_JOBS | DERIVED_CLOUD_JOBS
)


def jobs_for_authority(jobs: Iterable[dict], authority: str) -> list[dict]:
    """Return only jobs the requested execution plane is allowed to publish."""
    if authority == "local":
        allowed = LOCAL_AUTHORITATIVE_JOBS
    elif authority == "cloud":
        allowed = CLOUD_AUTHORITATIVE_JOBS
    else:
        raise ValueError(f"unsupported health authority: {authority!r}")
    return [job for job in jobs if job.get("job") in allowed]

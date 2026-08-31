"""Parity tests for the Worker-enforced health-authority contract."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.monitoring.health_authority import (
    CLOUD_AUTHORITATIVE_JOBS,
    DERIVED_CLOUD_JOBS,
    LOCAL_AUTHORITATIVE_JOBS,
    jobs_for_authority,
)
from src.monitoring.job_schedule import JOB_EXPECTATIONS

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "cloudflare" / "worker.js"


def test_authority_sets_partition_known_scheduled_jobs() -> None:
    assert LOCAL_AUTHORITATIVE_JOBS.isdisjoint(CLOUD_AUTHORITATIVE_JOBS)
    assert LOCAL_AUTHORITATIVE_JOBS | CLOUD_AUTHORITATIVE_JOBS == (
        set(JOB_EXPECTATIONS) | DERIVED_CLOUD_JOBS
    )


def test_authority_filter_excludes_foreign_jobs() -> None:
    jobs = [
        {"job": "daily_scan"},
        {"job": "tennis_scan"},
        {"job": "unknown_job"},
    ]

    assert jobs_for_authority(jobs, "local") == [{"job": "daily_scan"}]
    assert jobs_for_authority(jobs, "cloud") == [{"job": "tennis_scan"}]


def test_unknown_authority_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported health authority"):
        jobs_for_authority([], "unknown")


def test_worker_contains_every_python_authority_job() -> None:
    source = WORKER.read_text()

    for job in LOCAL_AUTHORITATIVE_JOBS | CLOUD_AUTHORITATIVE_JOBS:
        assert f"'{job}'" in source


def test_worker_keeps_health_separate_from_financial_signals_kv() -> None:
    source = WORKER.read_text()

    assert "health_v1" in source
    assert "_mergeHealthWithRetry" in source
    assert "canonical_health_baseline_missing" in source
    assert "compare-and-swap" in source
    assert "strict serializable guarantee" in source

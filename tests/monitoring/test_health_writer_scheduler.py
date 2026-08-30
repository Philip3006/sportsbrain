"""Tests for scheduler observability fields in health_writer (STAB-SCHED-AUTH-001)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.monitoring.health_writer import _coerce_scheduler_lag, write_health

# ── _coerce_scheduler_lag unit tests ─────────────────────────────────────────

def test_coerce_lag_none_returns_none():
    assert _coerce_scheduler_lag(None) is None


def test_coerce_lag_empty_string_returns_none():
    assert _coerce_scheduler_lag("") is None


def test_coerce_lag_valid_int():
    assert _coerce_scheduler_lag(45) == 45


def test_coerce_lag_valid_string_int():
    assert _coerce_scheduler_lag("45") == 45


def test_coerce_lag_zero():
    assert _coerce_scheduler_lag(0) == 0


def test_coerce_lag_negative_clamped_to_zero():
    assert _coerce_scheduler_lag(-5) == 0


def test_coerce_lag_non_numeric_string_returns_none():
    assert _coerce_scheduler_lag("abc") is None


def test_coerce_lag_bool_rejected():
    assert _coerce_scheduler_lag(True) is None
    assert _coerce_scheduler_lag(False) is None


# ── write_health scheduler field serialization ────────────────────────────────

@pytest.fixture()
def health_dir(tmp_path):
    with patch("src.monitoring.health_writer.HEALTH_DIR", tmp_path):
        yield tmp_path


def _read_health(health_dir: Path, job: str) -> dict:
    return json.loads((health_dir / f"{job}.json").read_text())


def test_scheduler_fields_absent_by_default(health_dir):
    write_health("tennis_settle", "ok", exit_code=0)
    payload = _read_health(health_dir, "tennis_settle")
    # Fields must be present in schema (None) — not missing entirely
    assert "scheduler" in payload
    assert payload["scheduler"] is None
    assert "scheduler_lag_s" in payload
    assert payload["scheduler_lag_s"] is None
    assert "scheduled_at" in payload
    assert payload["scheduled_at"] is None
    assert "idempotency_key" in payload
    assert payload["idempotency_key"] is None


def test_scheduler_metadata_serialized_correctly(health_dir):
    write_health(
        "tennis_settle",
        "ok",
        exit_code=0,
        scheduler="cloudflare_cron",
        scheduled_at="2026-09-05T08:15:00Z",
        scheduler_lag_s=42,
        idempotency_key="tennis_settle/2026-09-05T08:15",
    )
    payload = _read_health(health_dir, "tennis_settle")
    assert payload["scheduler"] == "cloudflare_cron"
    assert payload["scheduled_at"] == "2026-09-05T08:15:00Z"
    assert payload["scheduler_lag_s"] == 42
    assert payload["idempotency_key"] == "tennis_settle/2026-09-05T08:15"


def test_scheduler_gh_fallback_identifier(health_dir):
    write_health(
        "bundesliga2_live_push",
        "ok",
        exit_code=0,
        scheduler="gh_cron_fallback",
        scheduler_lag_s=0,
    )
    payload = _read_health(health_dir, "bundesliga2_live_push")
    assert payload["scheduler"] == "gh_cron_fallback"
    assert payload["scheduler_lag_s"] == 0


def test_invalid_scheduler_lag_stored_as_none(health_dir):
    write_health("tennis_settle", "ok", exit_code=0, scheduler_lag_s="not_a_number")
    payload = _read_health(health_dir, "tennis_settle")
    assert payload["scheduler_lag_s"] is None


def test_empty_scheduler_stored_as_none(health_dir):
    write_health("tennis_settle", "ok", exit_code=0, scheduler="")
    payload = _read_health(health_dir, "tennis_settle")
    assert payload["scheduler"] is None


# ── Existing MON-001 semantics unchanged ──────────────────────────────────────

def test_mon001_ok_with_zero_exit_code(health_dir):
    write_health("tennis_settle", "ok", exit_code=0)
    payload = _read_health(health_dir, "tennis_settle")
    assert payload["status"] == "ok"
    assert payload["exit_code"] == 0


def test_mon001_coerces_ok_to_error_on_nonzero_exit(health_dir):
    write_health("tennis_settle", "ok", exit_code=1)
    payload = _read_health(health_dir, "tennis_settle")
    assert payload["status"] == "error"
    assert payload["exit_code"] == 1
    assert "[MON-001]" in (payload["error"] or "")


def test_mon001_coerces_ok_to_error_on_unknown_exit(health_dir):
    write_health("tennis_settle", "ok", exit_code=True)  # bool rejected
    payload = _read_health(health_dir, "tennis_settle")
    assert payload["status"] == "error"
    assert "[MON-001]" in (payload["error"] or "")


def test_scheduler_fields_do_not_affect_mon001(health_dir):
    # Scheduler metadata must not interfere with MON-001 enforcement
    write_health(
        "tennis_settle",
        "ok",
        exit_code=1,
        scheduler="cloudflare_cron",
        scheduler_lag_s=30,
    )
    payload = _read_health(health_dir, "tennis_settle")
    assert payload["status"] == "error"  # MON-001 coercion still applies


# ── FIX 1 regressions: null lag for unverifiable scheduled_at ────────────────
# When no provable scheduled_at exists (GH fallback, manual dispatch), the workflow
# outputs lag_s="" (empty). health_writer must serialize scheduler_lag_s=null,
# not a fabricated zero. These tests document that contract.

def test_cf_dispatch_scheduled_at_persisted(health_dir):
    write_health(
        "bundesliga2_live_push",
        "ok",
        exit_code=0,
        scheduler="cloudflare_cron",
        scheduled_at="2026-09-05T20:14:00Z",
        scheduler_lag_s=45,
        idempotency_key="bl2_live_push/2026-09-05T20:14",
    )
    payload = _read_health(health_dir, "bundesliga2_live_push")
    assert payload["scheduled_at"] == "2026-09-05T20:14:00Z", "CF scheduled_at persisted"


def test_cf_dispatch_idempotency_key_persisted(health_dir):
    write_health(
        "bundesliga2_live_push",
        "ok",
        exit_code=0,
        scheduler="cloudflare_cron",
        idempotency_key="bl2_live_push/2026-09-05T20:14",
    )
    payload = _read_health(health_dir, "bundesliga2_live_push")
    assert payload["idempotency_key"] == "bl2_live_push/2026-09-05T20:14", "idempotency_key persisted"


def test_cf_dispatch_measured_lag_persisted(health_dir):
    write_health(
        "bundesliga2_live_push",
        "ok",
        exit_code=0,
        scheduler="cloudflare_cron",
        scheduler_lag_s=45,
    )
    payload = _read_health(health_dir, "bundesliga2_live_push")
    assert payload["scheduler_lag_s"] == 45, "measured scheduler_lag_s persisted"


def test_gh_fallback_without_scheduled_at_lag_is_null(health_dir):
    # GH schedule fallback: no provable scheduled_at → workflow passes lag_s=""
    # health_writer must produce scheduler_lag_s=null, not fabricated zero.
    write_health(
        "bundesliga2_live_push",
        "ok",
        exit_code=0,
        scheduler="gh_cron_fallback",
        # scheduled_at not passed (workflow outputs empty string → not passed as CLI arg)
        scheduler_lag_s="",  # empty string from workflow conditional
    )
    payload = _read_health(health_dir, "bundesliga2_live_push")
    assert payload["scheduler_lag_s"] is None, "GH fallback: unknown lag must be null, not zero"
    assert payload["scheduler"] == "gh_cron_fallback"


def test_manual_dispatch_without_scheduled_at_lag_is_null(health_dir):
    # workflow_dispatch without scheduled_at input: lag unknown → null.
    write_health(
        "tennis_settle",
        "ok",
        exit_code=0,
        scheduler="workflow_dispatch",
        # lag_s not passed at all (workflow conditional skips the flag)
    )
    payload = _read_health(health_dir, "tennis_settle")
    assert payload["scheduler_lag_s"] is None, "manual dispatch: unknown lag must be null, not zero"
    assert payload["scheduled_at"] is None


# ── Old callers remain unchanged ──────────────────────────────────────────────

def test_old_caller_without_scheduler_fields(health_dir):
    # Verify existing callers that don't pass scheduler fields still work unchanged
    write_health(
        "daily_scan",
        "ok",
        exit_code=0,
        duration_s=12.3,
        run_id="daily-scan-20260619T070000Z-123",
    )
    payload = _read_health(health_dir, "daily_scan")
    assert payload["status"] == "ok"
    assert payload["exit_code"] == 0
    assert payload["duration_s"] == 12.3
    assert payload["run_id"] == "daily-scan-20260619T070000Z-123"
    # Scheduler fields present as None — no error
    assert payload["scheduler"] is None
    assert payload["scheduler_lag_s"] is None

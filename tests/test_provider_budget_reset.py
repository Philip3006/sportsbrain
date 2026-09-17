"""Monthly The Odds API quota reset and external-state regression tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.data import odds_api
from src.signals import provider_budget

BEFORE_RESET = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
AT_RESET = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def _usage(path: Path, *, reset_at: str | None) -> None:
    payload = {"requests_used": 500, "requests_remaining": 0}
    if reset_at is not None:
        payload.update(
            {
                "observed_at": BEFORE_RESET.isoformat(),
                "reset_at": reset_at,
                "state": "QUOTA_EXHAUSTED",
                "source": "test_headers",
            }
        )
    path.write_text(json.dumps(payload))


def test_zero_quota_without_reset_evidence_remains_fail_closed(tmp_path, monkeypatch):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    _usage(usage, reset_at=None)
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)

    assert provider_budget.is_provider_available("the_odds_api", now=AT_RESET) is False
    assert (
        provider_budget.the_odds_api_quota_revalidation_eligible(now=AT_RESET) is False
    )


def test_zero_quota_stays_closed_before_monthly_reset(tmp_path, monkeypatch):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    _usage(usage, reset_at=AT_RESET.isoformat())
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)

    assert (
        provider_budget.is_provider_available("the_odds_api", now=BEFORE_RESET) is False
    )
    assert (
        provider_budget.the_odds_api_quota_revalidation_eligible(now=BEFORE_RESET)
        is False
    )
    persisted = json.loads(budget.read_text())
    assert persisted["the_odds_api"]["circuit_reset_at"].startswith("2026-10-01")


def test_monthly_reset_is_only_explicit_revalidation_opt_in(tmp_path, monkeypatch):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    _usage(usage, reset_at=AT_RESET.isoformat())
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)

    assert (
        provider_budget.the_odds_api_quota_revalidation_eligible(now=AT_RESET) is True
    )
    assert provider_budget.is_provider_available("the_odds_api", now=AT_RESET) is False
    assert (
        provider_budget.is_provider_available(
            "the_odds_api", allow_quota_revalidation=True, now=AT_RESET
        )
        is True
    )


def test_explicit_reset_revalidation_clears_legacy_no_reset_circuit(
    tmp_path, monkeypatch
):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    _usage(usage, reset_at=AT_RESET.isoformat())
    budget.write_text(
        json.dumps(
            {
                "the_odds_api": {
                    "circuit_open": True,
                    "circuit_reset_at": "",
                    "fallback_reason": "quota_exhausted",
                }
            }
        )
    )
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)

    assert (
        provider_budget.is_provider_available(
            "the_odds_api", allow_quota_revalidation=True, now=AT_RESET
        )
        is True
    )


def test_legacy_fetch_does_not_probe_at_reset_without_opt_in(tmp_path, monkeypatch):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    _usage(usage, reset_at=AT_RESET.isoformat())
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)
    monkeypatch.setattr(odds_api, "_load_stale_upcoming_cache", lambda: None)
    calls = []
    monkeypatch.setattr(
        odds_api,
        "_http_get_with_retry",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="circuit open"):
        odds_api.fetch_upcoming_matches(sport="soccer_test", force=True)
    assert calls == []


def test_usage_log_persists_fresh_monthly_reset_metadata_without_credentials(
    tmp_path, monkeypatch
):
    usage = tmp_path / "api_usage.json"
    monkeypatch.setattr(odds_api, "_USAGE_LOG", usage)
    odds_api._log_usage(12, 488, observed_at=BEFORE_RESET)

    payload = json.loads(usage.read_text())
    assert payload["requests_used"] == 12
    assert payload["requests_remaining"] == 488
    assert payload["observed_at"] == BEFORE_RESET.isoformat()
    assert payload["reset_at"] == "2026-10-01T00:00:00+00:00"
    assert "ODDS_API_KEY" not in usage.read_text()


@pytest.mark.parametrize("remaining", [1, 488])
def test_positive_quota_evidence_is_available(tmp_path, monkeypatch, remaining):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    usage.write_text(
        json.dumps(
            {
                "requests_used": 500 - remaining,
                "requests_remaining": remaining,
                "observed_at": BEFORE_RESET.isoformat(),
                "reset_at": AT_RESET.isoformat(),
                "state": "AVAILABLE",
                "source": "test_headers",
            }
        )
    )
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)

    assert (
        provider_budget.is_provider_available("the_odds_api", now=BEFORE_RESET) is True
    )

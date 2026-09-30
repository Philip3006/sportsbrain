"""Monthly The Odds API quota reset and external-state regression tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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


def _canonical_top5_usage(
    *, observed_at: str, reset_at: str, **overrides: object
) -> dict[str, object]:
    return {
        "schema_version": "the-odds-api-quota-evidence-v1",
        "provider": "the_odds_api",
        "requests_used": 12,
        "requests_remaining": 488,
        "observed_at": observed_at,
        "reset_at": reset_at,
        "state": "AVAILABLE",
        "source": "the_odds_api_response_headers",
        **overrides,
    }


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
    assert payload["schema_version"] == "the-odds-api-quota-evidence-v1"
    assert payload["provider"] == "the_odds_api"
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


def test_top5_gate_rejects_legacy_positive_quota_after_circuit_reset(
    tmp_path, monkeypatch
):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    usage.write_text(json.dumps({"requests_used": 459, "requests_remaining": 41}))
    budget.write_text(
        json.dumps(
            {
                "the_odds_api": {
                    "circuit_open": True,
                    "last_error_code": 401,
                    "fallback_reason": "http_401",
                    "last_error_ts": "2026-09-29T00:01:10Z",
                    "circuit_reset_at": "2026-09-30T00:00:00Z",
                }
            }
        )
    )
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)

    target = datetime(2026, 10, 9, 13, 22, 30, tzinfo=timezone.utc)
    assert provider_budget.is_top5_provider_available(now=target) is False
    assert json.loads(budget.read_text())["the_odds_api"]["circuit_open"] is True


def test_top5_gate_requires_quota_evidence_within_900_seconds(tmp_path, monkeypatch):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)
    target = datetime(2026, 10, 9, 13, 22, 30, tzinfo=timezone.utc)
    reset_at = "2026-11-01T00:00:00+00:00"

    for age, expected in ((900, True), (901, False)):
        observed = target - timedelta(seconds=age)
        usage.write_text(
            json.dumps(
                _canonical_top5_usage(
                    observed_at=observed.isoformat(), reset_at=reset_at
                )
            )
        )
        assert provider_budget.is_top5_provider_available(now=target) is expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"observed_at": None},
        {"observed_at": "not-a-timestamp"},
        {"reset_at": None},
        {"reset_at": "2026-12-01T00:00:00+00:00"},
        {"state": "QUOTA_EXHAUSTED"},
        {"requests_remaining": 0},
        {"requests_remaining": -1},
        {"provider": "another_provider"},
        {"provider": "the_odds_api/other-account"},
        {"schema_version": "unknown"},
        {"source": "the_odds_api_response_headers_extra"},
    ],
    ids=(
        "missing-observed-at",
        "invalid-observed-at",
        "missing-reset-at",
        "inconsistent-reset-at",
        "non-available-state",
        "zero-remaining",
        "negative-remaining",
        "wrong-provider",
        "malformed-provider-identity",
        "wrong-usage-schema",
        "noncanonical-source",
    ),
)
def test_top5_gate_rejects_incomplete_or_wrong_provider_quota_contract(
    tmp_path, monkeypatch, overrides
):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)
    target = datetime(2026, 10, 9, 13, 22, 30, tzinfo=timezone.utc)
    observed_at = (target - timedelta(seconds=30)).isoformat()
    values = _canonical_top5_usage(
        observed_at=observed_at,
        reset_at="2026-11-01T00:00:00+00:00",
    )
    values.update(overrides)
    usage.write_text(json.dumps(values))

    before = usage.read_bytes()
    assert provider_budget.is_top5_provider_available(now=target) is False
    assert usage.read_bytes() == before


def test_top5_gate_rejects_reset_that_expired_after_a_fresh_observation(
    tmp_path, monkeypatch
):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)
    observed = datetime(2026, 9, 30, 23, 59, 30, tzinfo=timezone.utc)
    after_reset = datetime(2026, 10, 1, 0, 0, 30, tzinfo=timezone.utc)
    usage.write_text(
        json.dumps(
            _canonical_top5_usage(
                observed_at=observed.isoformat(),
                reset_at="2026-10-01T00:00:00+00:00",
            )
        )
    )

    assert provider_budget.is_top5_provider_available(now=after_reset) is False


@pytest.mark.parametrize(
    "source",
    [
        "the_odds_api_response_headers",
        "the_odds_api_auth_revalidation_response_headers",
    ],
)
def test_top5_gate_accepts_both_canonical_odds_api_quota_sources(
    tmp_path, monkeypatch, source
):
    usage = tmp_path / "api_usage.json"
    budget = tmp_path / "provider_budget.json"
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget)
    target = datetime(2026, 10, 9, 13, 22, 30, tzinfo=timezone.utc)
    observed_at = target - timedelta(seconds=30)
    usage.write_text(
        json.dumps(
            _canonical_top5_usage(
                observed_at=observed_at.isoformat(),
                reset_at="2026-11-01T00:00:00+00:00",
                source=source,
            )
        )
    )

    assert provider_budget.is_top5_provider_available(now=target) is True

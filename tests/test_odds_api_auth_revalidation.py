"""Offline tests for the governed The Odds API auth revalidation seam."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from src.data import odds_api
from src.signals import provider_budget


def _open_auth_circuit(
    path: Path, *, code: int = 401, reason: str | None = None
) -> None:
    path.write_text(
        json.dumps(
            {
                "the_odds_api": {
                    "circuit_open": True,
                    "last_error_code": code,
                    "fallback_reason": reason or f"http_{code}",
                    "circuit_reset_at": "2099-01-01T00:00:00Z",
                }
            }
        )
    )


def _configure_budget(tmp_path, monkeypatch, *, code: int = 401) -> Path:
    budget_path = tmp_path / "provider_budget.json"
    _open_auth_circuit(budget_path, code=code)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", budget_path)
    return budget_path


def _response(status_code: int, headers: dict[str, str] | None = None) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.headers = headers or {}
    return response


def test_ordinary_caller_cannot_bypass_auth_circuit(tmp_path, monkeypatch):
    budget_path = _configure_budget(tmp_path, monkeypatch)
    calls: list[tuple[object, ...]] = []

    def fail_if_called(*args, **kwargs):
        calls.append(args)
        raise AssertionError("auth revalidation must require explicit opt-in")

    monkeypatch.setattr(odds_api.requests, "get", fail_if_called)

    with pytest.raises(RuntimeError, match="explicit opt-in"):
        odds_api.revalidate_the_odds_api_auth_once()

    assert calls == []
    assert json.loads(budget_path.read_text())["the_odds_api"]["circuit_open"] is True


def test_explicit_revalidation_makes_exactly_one_request_and_closes_circuit(
    tmp_path, monkeypatch
):
    budget_path = _configure_budget(tmp_path, monkeypatch)
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(odds_api, "get_api_key", lambda: "synthetic-test-key")

    def fake_get(url, *, params, timeout):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return _response(
            200,
            {
                "Content-Type": "application/json",
                "X-Requests-Remaining": "497",
                "Authorization": "must-not-be-persisted",
            },
        )

    monkeypatch.setattr(odds_api.requests, "get", fake_get)

    result = odds_api.revalidate_the_odds_api_auth_once(explicit_opt_in=True)
    state = json.loads(budget_path.read_text())["the_odds_api"]

    assert result["status"] == "verified"
    assert result["request_count"] == 1
    assert result["credential_access_count"] == 1
    assert result["retry_count"] == 0
    assert len(calls) == 1
    assert calls[0]["params"] == {"apiKey": "synthetic-test-key"}
    assert result["safe_headers"] == {
        "content-type": "application/json",
        "x-requests-remaining": "497",
    }
    assert state["circuit_open"] is False
    assert state["last_auth_revalidation"]["circuit_transition"] == "closed"
    assert state["last_auth_revalidation"]["request_count"] == 1
    assert state["last_auth_revalidation"]["credential_access_count"] == 1
    persisted = budget_path.read_text()
    assert "must-not-be-persisted" not in persisted
    assert "synthetic-test-key" not in persisted


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_failure_reopens_circuit_without_retry(tmp_path, monkeypatch, status_code):
    budget_path = _configure_budget(tmp_path, monkeypatch, code=status_code)
    calls = 0

    monkeypatch.setattr(odds_api, "get_api_key", lambda: "synthetic-test-key")

    def fake_get(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _response(status_code)

    monkeypatch.setattr(odds_api.requests, "get", fake_get)

    result = odds_api.revalidate_the_odds_api_auth_once(explicit_opt_in=True)
    state = json.loads(budget_path.read_text())["the_odds_api"]

    assert result["status"] == "failed_closed"
    assert result["http_status"] == status_code
    assert result["request_count"] == 1
    assert result["retry_count"] == 0
    assert calls == 1
    assert state["circuit_open"] is True
    assert state["last_auth_revalidation"]["circuit_transition"] == "reopened"


def test_transport_failure_is_audited_without_exception_text_or_retry(
    tmp_path, monkeypatch
):
    budget_path = _configure_budget(tmp_path, monkeypatch)
    monkeypatch.setattr(odds_api, "get_api_key", lambda: "synthetic-test-key")
    monkeypatch.setattr(
        odds_api.requests,
        "get",
        Mock(
            side_effect=requests.ConnectionError("secret URL should not be persisted")
        ),
    )

    result = odds_api.revalidate_the_odds_api_auth_once(explicit_opt_in=True)
    persisted = budget_path.read_text()

    assert result["status"] == "failed_closed"
    assert result["request_count"] == 1
    assert result["retry_count"] == 0
    assert result["audit"]["failure_class"] == "ConnectionError"
    assert "secret URL should not be persisted" not in persisted
    assert "synthetic-test-key" not in persisted


def test_quota_reset_revalidation_remains_separate(tmp_path, monkeypatch):
    budget_path = _configure_budget(tmp_path, monkeypatch)
    usage_path = tmp_path / "api_usage.json"
    usage_path.write_text(
        json.dumps(
            {
                "requests_remaining": 0,
                "observed_at": "2026-09-30T23:59:00+00:00",
                "reset_at": "2026-10-01T00:00:00+00:00",
            }
        )
    )
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", usage_path)

    assert (
        provider_budget.is_provider_available(
            "the_odds_api",
            now=provider_budget._parse_timestamp("2026-10-01T00:00:00+00:00"),
        )
        is False
    )

    with pytest.raises(RuntimeError, match="auth revalidation"):
        provider_budget.begin_auth_revalidation("the_odds_api", explicit_opt_in=True)

    assert provider_budget.the_odds_api_quota_revalidation_eligible(
        now=provider_budget._parse_timestamp("2026-10-01T00:00:00+00:00")
    )
    assert json.loads(budget_path.read_text())["the_odds_api"]["circuit_open"] is True


def test_only_the_odds_api_auth_circuit_is_eligible(tmp_path, monkeypatch):
    _configure_budget(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="provider is not permitted"):
        provider_budget.begin_auth_revalidation(
            "therundown_experimental", explicit_opt_in=True
        )

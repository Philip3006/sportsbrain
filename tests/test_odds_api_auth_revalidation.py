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
    monkeypatch.setattr(provider_budget, "_API_USAGE_PATH", tmp_path / "api_usage.json")
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
    usage_path = tmp_path / "api_usage.json"
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(odds_api, "get_api_key", lambda: "synthetic-test-key")
    real_record_success = provider_budget.record_success

    def assert_quota_is_durable_before_close(name, *, quota_remaining=None):
        quota = json.loads(usage_path.read_text())
        circuit = json.loads(budget_path.read_text())["the_odds_api"]
        assert quota["requests_used"] == 3
        assert quota["requests_remaining"] == 497
        assert circuit["circuit_open"] is True
        real_record_success(name, quota_remaining=quota_remaining)

    monkeypatch.setattr(
        provider_budget, "record_success", assert_quota_is_durable_before_close
    )

    def fake_get(url, *, params, timeout, allow_redirects):
        calls.append(
            {
                "url": url,
                "params": params,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        response = _response(
            200,
            {
                "Content-Type": "application/json",
                "X-Requests-Used": "3",
                "X-Requests-Remaining": "497",
                "X-Requests-Limit": "500",
                "Authorization": "must-not-be-persisted",
            },
        )
        response.text = "response body must not be persisted"
        return response

    monkeypatch.setattr(odds_api.requests, "get", fake_get)

    result = odds_api.revalidate_the_odds_api_auth_once(explicit_opt_in=True)
    state = json.loads(budget_path.read_text())["the_odds_api"]

    assert result["status"] == "verified"
    assert result["request_count"] == 1
    assert result["credential_access_count"] == 1
    assert result["retry_count"] == 0
    assert len(calls) == 1
    assert calls[0]["allow_redirects"] is False
    assert calls[0]["params"] == {"apiKey": "synthetic-test-key"}
    assert result["safe_headers"] == {
        "content-type": "application/json",
        "x-requests-used": "3",
        "x-requests-remaining": "497",
        "x-requests-limit": "500",
    }
    assert state["circuit_open"] is False
    assert state["last_auth_revalidation"]["circuit_transition"] == "closed"
    assert state["last_auth_revalidation"]["request_count"] == 1
    assert state["last_auth_revalidation"]["credential_access_count"] == 1
    quota = json.loads(usage_path.read_text())
    assert quota["requests_used"] == 3
    assert quota["requests_remaining"] == 497
    assert quota["state"] == "AVAILABLE"
    assert quota["source"] == "the_odds_api_auth_revalidation_response_headers"
    assert quota["observed_at"]
    assert quota["reset_at"]
    assert provider_budget.odds_api_quota_state()["requests_remaining"] == 497
    assert provider_budget.is_provider_available("the_odds_api") is True
    persisted = budget_path.read_text() + usage_path.read_text()
    assert "must-not-be-persisted" not in persisted
    assert "synthetic-test-key" not in persisted
    assert "response body must not be persisted" not in persisted


def _assert_auth_revalidation_stays_closed(budget_path: Path) -> None:
    state = json.loads(budget_path.read_text())["the_odds_api"]
    assert state["circuit_open"] is True
    assert state["last_auth_revalidation"]["circuit_transition"] == "remains_open"
    assert provider_budget.is_provider_available("the_odds_api") is False


def test_missing_remaining_header_fails_closed_without_quota_state(
    tmp_path, monkeypatch
):
    budget_path = _configure_budget(tmp_path, monkeypatch)
    usage_path = tmp_path / "api_usage.json"
    monkeypatch.setattr(odds_api, "get_api_key", lambda: "synthetic-test-key")
    calls = []
    monkeypatch.setattr(
        odds_api.requests,
        "get",
        lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or _response(
                200,
                {
                    "X-Requests-Used": "3",
                },
            )
        ),
    )

    result = odds_api.revalidate_the_odds_api_auth_once(explicit_opt_in=True)

    assert result["status"] == "failed_closed"
    assert result["request_count"] == 1
    assert len(calls) == 1
    assert not usage_path.exists()
    assert (
        json.loads(budget_path.read_text())["the_odds_api"]["last_auth_revalidation"][
            "failure_class"
        ]
        == "missing_x_requests_remaining"
    )
    _assert_auth_revalidation_stays_closed(budget_path)


@pytest.mark.parametrize("remaining", ["not-an-integer", "-1"])
def test_malformed_or_negative_remaining_header_fails_closed(
    tmp_path, monkeypatch, remaining
):
    budget_path = _configure_budget(tmp_path, monkeypatch)
    usage_path = tmp_path / "api_usage.json"
    monkeypatch.setattr(odds_api, "get_api_key", lambda: "synthetic-test-key")
    monkeypatch.setattr(
        odds_api.requests,
        "get",
        lambda *args, **kwargs: _response(
            200,
            {"X-Requests-Used": "3", "X-Requests-Remaining": remaining},
        ),
    )

    result = odds_api.revalidate_the_odds_api_auth_once(explicit_opt_in=True)

    assert result["status"] == "failed_closed"
    assert not usage_path.exists()
    _assert_auth_revalidation_stays_closed(budget_path)


def test_zero_remaining_is_recorded_but_provider_stays_unavailable(
    tmp_path, monkeypatch
):
    budget_path = _configure_budget(tmp_path, monkeypatch)
    usage_path = tmp_path / "api_usage.json"
    monkeypatch.setattr(odds_api, "get_api_key", lambda: "synthetic-test-key")
    monkeypatch.setattr(
        odds_api.requests,
        "get",
        lambda *args, **kwargs: _response(
            200,
            {
                "X-Requests-Used": "3",
                "X-Requests-Remaining": "0",
                "X-Requests-Limit": "500",
            },
        ),
    )

    result = odds_api.revalidate_the_odds_api_auth_once(explicit_opt_in=True)

    assert result["status"] == "failed_closed"
    quota = json.loads(usage_path.read_text())
    assert quota["requests_used"] == 3
    assert quota["requests_remaining"] == 0
    assert quota["state"] == "QUOTA_EXHAUSTED"
    assert quota["source"] == "the_odds_api_auth_revalidation_response_headers"
    assert (
        json.loads(budget_path.read_text())["the_odds_api"]["last_auth_revalidation"][
            "failure_class"
        ]
        == "quota_remaining_zero"
    )
    _assert_auth_revalidation_stays_closed(budget_path)


@pytest.mark.parametrize(
    ("used", "include_used"),
    [("3", False), ("not-an-integer", True), ("-1", True)],
)
def test_missing_or_invalid_used_header_fails_closed(
    tmp_path, monkeypatch, used, include_used
):
    budget_path = _configure_budget(tmp_path, monkeypatch)
    usage_path = tmp_path / "api_usage.json"
    headers = {"X-Requests-Remaining": "497"}
    if include_used:
        headers["X-Requests-Used"] = used
    monkeypatch.setattr(odds_api, "get_api_key", lambda: "synthetic-test-key")
    monkeypatch.setattr(
        odds_api.requests,
        "get",
        lambda *args, **kwargs: _response(200, headers),
    )

    result = odds_api.revalidate_the_odds_api_auth_once(explicit_opt_in=True)

    assert result["status"] == "failed_closed"
    assert not usage_path.exists()
    _assert_auth_revalidation_stays_closed(budget_path)


@pytest.mark.parametrize(
    "headers",
    [
        {
            "X-Requests-Used": "3",
            "X-Requests-Remaining": "497",
            "X-Requests-Limit": "0",
        },
        {
            "X-Requests-Used": "3",
            "X-Requests-Remaining": "497",
            "X-Requests-Limit": "bad",
        },
        {
            "X-Requests-Used": "3",
            "X-Requests-Remaining": "501",
            "X-Requests-Limit": "500",
        },
        {
            "X-Requests-Used": "501",
            "X-Requests-Remaining": "0",
            "X-Requests-Limit": "500",
        },
    ],
)
def test_invalid_limit_or_impossible_remaining_fails_closed(
    tmp_path, monkeypatch, headers
):
    budget_path = _configure_budget(tmp_path, monkeypatch)
    usage_path = tmp_path / "api_usage.json"
    monkeypatch.setattr(odds_api, "get_api_key", lambda: "synthetic-test-key")
    monkeypatch.setattr(
        odds_api.requests,
        "get",
        lambda *args, **kwargs: _response(200, headers),
    )

    result = odds_api.revalidate_the_odds_api_auth_once(explicit_opt_in=True)

    assert result["status"] == "failed_closed"
    assert not usage_path.exists()
    _assert_auth_revalidation_stays_closed(budget_path)


def test_quota_persistence_failure_keeps_circuit_open(tmp_path, monkeypatch):
    budget_path = _configure_budget(tmp_path, monkeypatch)
    usage_path = tmp_path / "api_usage.json"
    monkeypatch.setattr(odds_api, "get_api_key", lambda: "synthetic-test-key")
    monkeypatch.setattr(
        odds_api.requests,
        "get",
        lambda *args, **kwargs: _response(
            200,
            {"X-Requests-Used": "3", "X-Requests-Remaining": "497"},
        ),
    )
    real_atomic_write_json = provider_budget.atomic_write_json

    def fail_usage_write(path, payload, **kwargs):
        if Path(path) == usage_path:
            raise OSError("synthetic quota persistence failure")
        return real_atomic_write_json(path, payload, **kwargs)

    monkeypatch.setattr(provider_budget, "atomic_write_json", fail_usage_write)

    result = odds_api.revalidate_the_odds_api_auth_once(explicit_opt_in=True)

    assert result["status"] == "failed_closed"
    assert not usage_path.exists()
    assert (
        json.loads(budget_path.read_text())["the_odds_api"]["last_auth_revalidation"][
            "failure_class"
        ]
        == "quota_state_persistence_failed"
    )
    _assert_auth_revalidation_stays_closed(budget_path)


def test_quota_headers_cannot_close_without_one_credentialed_request(
    tmp_path, monkeypatch
):
    budget_path = _configure_budget(tmp_path, monkeypatch)

    audit = provider_budget.record_auth_revalidation(
        "the_odds_api",
        status_code=200,
        request_count=0,
        credential_access_count=0,
        safe_headers={
            "x-requests-used": "3",
            "x-requests-remaining": "497",
        },
    )

    assert audit["circuit_transition"] == "remains_open"
    assert audit["failure_class"] == "revalidation_request_counts_invalid"
    assert not (tmp_path / "api_usage.json").exists()
    assert json.loads(budget_path.read_text())["the_odds_api"]["circuit_open"] is True


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

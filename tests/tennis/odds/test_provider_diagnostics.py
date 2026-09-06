"""Deterministic sanitized diagnostics contracts for the direct tennis merger."""
from __future__ import annotations

from src.tennis.odds.base import OddsQuote
from src.tennis.odds import merger
from src.tennis.odds import oddsportal


def _quote(provider: str = "pinnacle", tier: int = 1) -> OddsQuote:
    return OddsQuote("Alpha", "Beta", 1.9, 2.0, provider, tier)


def test_mixed_provider_failure_preserves_success(monkeypatch) -> None:
    monkeypatch.setattr(merger, "ENABLED_SOURCES", [
        ("broken", 1, lambda _: (_ for _ in ()).throw(TimeoutError())),
        ("working", 2, lambda _: _quote("working", 2)),
    ])
    quote, outcomes = merger.fetch_best_odds_with_diagnostics({}, allow_implied=False)

    assert quote and quote.source == "working"
    assert {item["provider"]: item["status_class"] for item in outcomes} == {"broken": "timeout", "working": "success"}


def test_betfair_without_credentials_is_ineligible(monkeypatch) -> None:
    for key in ("BETFAIR_APP_KEY", "BETFAIR_USERNAME", "BETFAIR_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(merger, "ENABLED_SOURCES", [("betfair", 1, lambda _: None)])

    _, outcomes = merger.fetch_best_odds_with_diagnostics({}, allow_implied=False)

    assert outcomes == [{"provider": "betfair", "attempted": False, "eligible": False, "status_class": "ineligible", "http_status": None, "result": "no_quote", "error_class": None}]


def test_diagnostics_never_include_environment_secret(monkeypatch) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "not-for-output")
    monkeypatch.setattr(merger, "ENABLED_SOURCES", [("working", 1, lambda _: _quote())])
    _, outcomes = merger.fetch_best_odds_with_diagnostics({}, allow_implied=False)

    assert "not-for-output" not in repr(outcomes)
    assert "Authorization" not in repr(outcomes)


def test_oddsportal_http_statuses_are_sanitized(monkeypatch) -> None:
    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code
            self.text = "secret response body"

    for status in (403, 429):
        oddsportal._BULK.clear()
        oddsportal._TS.clear()
        monkeypatch.setattr(oddsportal.requests, "get", lambda *_, status=status, **__: Response(status))
        quote, outcome = oddsportal.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta", "commence_time": "2026-09-06T19:00:00Z"})
        assert quote is None
        assert outcome.status_class == "http_error"
        assert outcome.http_status == status
        assert "secret" not in repr(outcome)


def test_oddsportal_timeout_and_request_exception(monkeypatch) -> None:
    import requests

    oddsportal._BULK.clear()
    monkeypatch.setattr(oddsportal.requests, "get", lambda *_, **__: (_ for _ in ()).throw(requests.Timeout()))
    _, outcome = oddsportal.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta", "commence_time": "2026-09-06T19:00:00Z"})
    assert outcome.status_class == "timeout"

    monkeypatch.setattr(oddsportal.requests, "get", lambda *_, **__: (_ for _ in ()).throw(requests.ConnectionError("secret")))
    _, outcome = oddsportal.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta", "commence_time": "2026-09-06T19:00:00Z"})
    assert outcome.status_class == "exception"
    assert outcome.error_class == "ConnectionError"
    assert "secret" not in repr(outcome)


def test_oddsportal_no_match_invalid_and_success(monkeypatch) -> None:
    success = oddsportal.ProviderOutcome(oddsportal.name, True, True, "success")
    monkeypatch.setattr(oddsportal, "_fetch_day_with_diagnostics", lambda _: ([], success))
    _, outcome = oddsportal.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta"})
    assert outcome.status_class == "no_match"

    invalid = [{"player_a": "Alpha", "player_b": "Beta", "h2h_a": 1.4, "h2h_b": 1.4}]
    monkeypatch.setattr(oddsportal, "_fetch_day_with_diagnostics", lambda _: (invalid, success))
    quote, outcome = oddsportal.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta"})
    assert quote is not None and outcome.status_class == "invalid_quote"

    valid = [{"player_a": "Alpha", "player_b": "Beta", "h2h_a": 1.9, "h2h_b": 2.0}]
    monkeypatch.setattr(oddsportal, "_fetch_day_with_diagnostics", lambda _: (valid, success))
    quote, outcome = oddsportal.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta"})
    assert quote is not None and outcome.status_class == "success" and outcome.result == "usable_quote"

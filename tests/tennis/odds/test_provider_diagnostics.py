"""Deterministic sanitized diagnostics contracts for the direct tennis merger."""
from __future__ import annotations

from src.tennis.odds.base import OddsQuote
from src.tennis.odds import merger
from src.tennis.odds import oddsportal
from src.tennis.odds import pinnacle
from src.tennis.odds import tennis_explorer


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


def test_oddsportal_cached_http_failure_is_preserved(monkeypatch) -> None:
    class Response:
        text = ""

        def __init__(self, status_code: int):
            self.status_code = status_code

    oddsportal._BULK.clear()
    oddsportal._TS.clear()
    oddsportal._OUTCOME_CACHE.clear()
    calls = []
    hint = {"player_a": "Alpha", "player_b": "Beta", "commence_time": "2026-09-06T19:00:00Z"}
    for status in (403, 429):
        oddsportal._BULK.clear()
        oddsportal._TS.clear()
        oddsportal._OUTCOME_CACHE.clear()
        calls.clear()
        monkeypatch.setattr(oddsportal.requests, "get", lambda *_, status=status, **__: calls.append(1) or Response(status))
        _, first = oddsportal.fetch_with_diagnostics(hint)
        _, second = oddsportal.fetch_with_diagnostics(hint)
        assert (first.status_class, first.http_status) == ("http_error", status)
        assert (second.status_class, second.http_status) == ("http_error", status)
        assert len(calls) == 1


def test_tennis_explorer_internal_failures_are_not_no_match(monkeypatch) -> None:
    tennis_explorer._BULK = []
    tennis_explorer._TS = 0.0
    monkeypatch.setattr("src.data.tennis_secondary_odds.fetch_te_upcoming_matches", lambda **_: (_ for _ in ()).throw(TimeoutError()))
    _, timeout = tennis_explorer.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta"})
    assert timeout.status_class == "timeout"
    monkeypatch.setattr("src.data.tennis_secondary_odds.fetch_te_upcoming_matches", lambda **_: (_ for _ in ()).throw(ConnectionError()))
    _, failure = tennis_explorer.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta"})
    assert failure.status_class == "exception"


def test_tennis_explorer_empty_success_is_no_match(monkeypatch) -> None:
    tennis_explorer._BULK = []
    tennis_explorer._TS = 0.0
    monkeypatch.setattr("src.data.tennis_secondary_odds.fetch_te_upcoming_matches", lambda **_: [])
    _, outcome = tennis_explorer.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta"})
    assert outcome.status_class == "no_match"


def test_pinnacle_diagnostics_do_not_leak_between_calls(monkeypatch) -> None:
    def failed_fetch(_):
        pinnacle._record_outcome(pinnacle.ProviderOutcome("pinnacle", True, True, "http_error", http_status=403))
        return None

    monkeypatch.setattr(pinnacle, "fetch", failed_fetch)
    _, failed = pinnacle.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta"})
    assert failed.status_class == "http_error"

    monkeypatch.setattr(pinnacle, "fetch", lambda _: None)
    _, next_call = pinnacle.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta"})
    assert next_call.status_class == "no_match"


def test_pinnacle_irrelevant_failure_does_not_override_no_match(monkeypatch) -> None:
    def fetch_with_irrelevant_failure(_):
        pinnacle._record_outcome(pinnacle.ProviderOutcome("pinnacle", True, True, "http_error", http_status=503))
        pinnacle._record_successful_response()
        return None

    monkeypatch.setattr(pinnacle, "fetch", fetch_with_irrelevant_failure)
    _, outcome = pinnacle.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta"})
    assert outcome.status_class == "no_match"


def test_pinnacle_matched_fixture_error_remains_causal(monkeypatch) -> None:
    def fetch_with_matched_fixture_error(_):
        pinnacle._record_successful_response()
        pinnacle._record_match_found()
        pinnacle._record_outcome(pinnacle.ProviderOutcome("pinnacle", True, True, "timeout", error_class="Timeout"))
        return None

    monkeypatch.setattr(pinnacle, "fetch", fetch_with_matched_fixture_error)
    _, outcome = pinnacle.fetch_with_diagnostics({"player_a": "Alpha", "player_b": "Beta"})
    assert outcome.status_class == "timeout"


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

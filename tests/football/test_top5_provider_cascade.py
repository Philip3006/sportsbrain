from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import Fixture, ProductionContractError
from src.football.provider_cascade import (
    MARKET_PREMATCH_1X2,
    AdapterResult,
    ApiFootballAdapter,
    BetfairDelayedAdapter,
    CascadeResult,
    NormalizedOddsObservation,
    OddsApiIoAdapter,
    ProviderCascadeConfig,
    ProviderCascadeRouter,
    ProviderComparisonReport,
    ProviderConfig,
    ProviderState,
    QuotaSnapshot,
    RawProviderResponse,
    RequestBudgetManager,
    TheOddsAPIAdapter,
    accepted_for_builder1,
    compare_provider_results,
)
from src.football.top5_real_shadow_provider import ProviderResponse

NOW = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
FIXTURE = Fixture(
    "canonical-fixture", "EPL", "Home FC", "Away FC", NOW + timedelta(minutes=120)
)


def _provider(
    name: str,
    *,
    enabled: bool = True,
    candidate_only: bool = True,
    quota: int | None = 10,
    reserve: int = 0,
    request_budget: int = 1,
    credentials_required: bool = False,
    credential_available: bool | None = True,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        enabled=enabled,
        candidate_only=candidate_only,
        bookmakers=("Bet365",),
        credentials_required=credentials_required,
        credential_available=credential_available,
        initial_quota=QuotaSnapshot(remaining=quota),
        quota_reserve=reserve,
        request_budget=request_budget,
        adapter_version=f"{name}:test",
    )


def _config(*names: str, **overrides: object) -> ProviderCascadeConfig:
    providers = {
        name: _provider(name, reserve=int(overrides.get("reserve", 0)))
        for name in names
    }
    return ProviderCascadeConfig(
        provider_order=names,
        providers=providers,
        global_request_budget=int(overrides.get("global_request_budget", len(names))),
        per_run_cap=int(overrides.get("per_run_cap", len(names))),
        allow_candidate_only=bool(overrides.get("allow_candidate_only", True)),
    )


def _observation(
    provider: str = "fake", *, captured: datetime = NOW
) -> NormalizedOddsObservation:
    return NormalizedOddsObservation(
        league_code=FIXTURE.league_code,
        fixture_key=FIXTURE.fixture_key,
        provider_fixture_id=f"{provider}-fixture",
        home_team=FIXTURE.home_team,
        away_team=FIXTURE.away_team,
        kickoff_utc=FIXTURE.kickoff,
        market_type=MARKET_PREMATCH_1X2,
        home_odds=2.0,
        draw_odds=3.5,
        away_odds=4.0,
        provider_identity=provider,
        bookmaker_identity="test-book",
        source_timestamp=captured,
        captured_at=captured,
        request_identity=f"request-{provider}",
        request_started_at=NOW,
        request_completed_at=NOW,
        latency_ms=4,
        provider_priority=0,
        fallback_depth=0,
        source_provenance="injected:test",
        raw_record_digest="a" * 64,
        adapter_version=f"{provider}:test",
    )


class FakeAdapter:
    def __init__(self, result: AdapterResult):
        self.result = result
        self.calls = 0

    def fetch(self, fixture, config, **kwargs):
        self.calls += 1
        return self.result


def _result(
    provider: str,
    state: ProviderState = ProviderState.AVAILABLE,
    *,
    observation=None,
    reason="injected",
):
    return AdapterResult(
        state=state,
        reason=reason,
        observation=observation
        if observation is not None
        else (_observation(provider) if state is ProviderState.AVAILABLE else None),
        status_code=200 if state is ProviderState.AVAILABLE else 503,
        latency_ms=7,
        network_called=True,
    )


def test_default_order_is_configurable_and_defaults_to_requested_shadow_order():
    config = ProviderCascadeConfig.default()
    config.validate()
    assert config.provider_order == (
        "the_odds_api",
        "odds_api_io",
        "api_football",
        "betfair_delayed",
    )
    legacy = ProviderCascadeConfig(
        provider_order=("the_odds_api",),
        providers={"the_odds_api": _provider("the_odds_api")},
        global_request_budget=1,
        per_run_cap=1,
    )
    legacy.validate()


@pytest.mark.parametrize(
    "config",
    [
        ProviderCascadeConfig(
            provider_order=("a", "a"), providers={"a": _provider("a")}
        ),
        ProviderCascadeConfig(
            provider_order=("unknown",), providers={"a": _provider("a")}
        ),
        ProviderCascadeConfig(
            provider_order=("a",), providers={"a": _provider("a"), "b": _provider("b")}
        ),
    ],
)
def test_invalid_provider_configuration_fails_closed(config):
    with pytest.raises(ProductionContractError):
        config.validate()


def test_missing_credentials_are_rejected_before_network():
    config = ProviderCascadeConfig(
        provider_order=("a",),
        providers={
            "a": _provider(
                "a", credentials_required=True, credential_available=False, quota=10
            ),
        },
        global_request_budget=1,
        per_run_cap=1,
    )
    adapter = FakeAdapter(_result("a"))
    result = ProviderCascadeRouter(config, adapters={"a": adapter}, now=NOW).route(
        FIXTURE, now=NOW
    )
    assert result.trace.fail_closed is True
    assert result.trace.attempts[0].state is ProviderState.CREDENTIAL_MISSING
    assert result.trace.attempts[0].network_called is False
    assert adapter.calls == 0


def test_exhausted_odds_api_preflight_never_calls_network():
    config = ProviderCascadeConfig(
        provider_order=("the_odds_api",),
        providers={
            "the_odds_api": ProviderConfig(
                name="the_odds_api",
                credentials_required=False,
                credential_available=True,
                initial_quota=QuotaSnapshot(used=500, remaining=0),
                adapter_version="the-odds-api-v4:test",
            ),
        },
        global_request_budget=1,
        per_run_cap=1,
    )
    adapter = FakeAdapter(_result("the_odds_api"))
    result = ProviderCascadeRouter(
        config, adapters={"the_odds_api": adapter}, now=NOW
    ).route(FIXTURE, now=NOW)
    assert result.observation is None
    assert result.trace.attempts[0].state is ProviderState.QUOTA_EXHAUSTED
    assert (
        result.trace.attempts[0].reason
        == "preflight_rejected:quota_exhausted_or_reserve"
    )
    assert result.trace.attempts[0].network_called is False
    assert adapter.calls == 0


def test_builtin_adapters_do_not_make_live_calls_without_controlled_authorization():
    config = ProviderCascadeConfig(
        provider_order=("odds_api_io",),
        providers={
            "odds_api_io": _provider(
                "odds_api_io", quota=10, credentials_required=False
            ),
        },
        global_request_budget=1,
        per_run_cap=1,
    )
    router = ProviderCascadeRouter(config, now=NOW)
    result = router.route(
        FIXTURE,
        now=NOW,
        provider_fixture_ids={"odds_api_io": "event-123"},
    )
    assert result.observation is None
    assert result.trace.fail_closed is True
    assert result.trace.attempts[0].state is ProviderState.HEALTH_UNKNOWN
    assert result.trace.attempts[0].reason == "live_calls_not_authorized"
    assert result.trace.attempts[0].network_called is False
    assert router.budget.global_requests_attempted == 0
    assert router.budget.counters("odds_api_io").requests_attempted == 0


def test_mapping_config_preserves_quota_and_requires_run_reference_for_live_calls():
    config = ProviderCascadeConfig.from_mapping(
        {
            "provider_order": ["the_odds_api"],
            "providers": {
                "the_odds_api": {
                    "credentials_required": False,
                    "initial_quota": {"used": 7, "remaining": 3},
                }
            },
            "global_request_budget": 1,
            "per_run_cap": 1,
        }
    )
    assert config.providers["the_odds_api"].initial_quota.remaining == 3
    with pytest.raises(ProductionContractError):
        ProviderCascadeConfig.from_mapping(
            {
                "provider_order": ["the_odds_api"],
                "providers": {"the_odds_api": {"credentials_required": False}},
                "global_request_budget": 1,
                "per_run_cap": 1,
                "live_calls_authorized": True,
            }
        )


def test_reserve_and_caps_reject_without_network():
    config = _config("a", reserve=2)
    budget = RequestBudgetManager(config, now=NOW)
    decision = budget.preflight("a", now=NOW)
    assert decision.allowed is True
    low_quota = RequestBudgetManager(
        _config("a", reserve=2),
        quota_overrides={"a": QuotaSnapshot(remaining=2)},
        now=NOW,
    )
    assert low_quota.preflight("a", now=NOW).allowed is False
    capped = RequestBudgetManager(
        _config("a", global_request_budget=0, per_run_cap=0), now=NOW
    )
    assert capped.preflight("a", now=NOW).state is ProviderState.QUOTA_EXHAUSTED


def test_provider_one_success_is_selected_without_fallback():
    config = _config("a", "b")
    first = FakeAdapter(_result("a"))
    second = FakeAdapter(_result("b"))
    result = ProviderCascadeRouter(
        config, adapters={"a": first, "b": second}, now=NOW
    ).route(FIXTURE, now=NOW)
    assert result.accepted
    assert result.observation.provider_identity == "a"
    assert result.trace.fallback_depth == 0
    assert len(result.trace.attempts) == 1
    assert first.calls == 1 and second.calls == 0


@pytest.mark.parametrize(
    "first_state",
    [
        ProviderState.QUOTA_EXHAUSTED,
        ProviderState.RATE_LIMITED,
        ProviderState.TEMPORARILY_UNAVAILABLE,
        ProviderState.MALFORMED,
        ProviderState.UNSUPPORTED_FIXTURE,
        ProviderState.QUALITY_REJECTED,
        ProviderState.STALE,
    ],
)
def test_provider_failure_moves_to_next_provider(first_state):
    config = _config("a", "b")
    first = FakeAdapter(_result("a", first_state, reason=first_state.value))
    second = FakeAdapter(_result("b"))
    result = ProviderCascadeRouter(
        config, adapters={"a": first, "b": second}, now=NOW
    ).route(FIXTURE, now=NOW)
    assert result.accepted
    assert result.observation.provider_identity == "b"
    assert result.trace.fallback_depth == 1
    assert [attempt.provider for attempt in result.trace.attempts] == ["a", "b"]
    assert first.calls == second.calls == 1


def test_stale_observation_is_rejected_and_falls_back():
    config = _config("a", "b")
    stale = _observation("a", captured=NOW - timedelta(seconds=901))
    first = FakeAdapter(_result("a", observation=stale))
    second = FakeAdapter(_result("b"))
    result = ProviderCascadeRouter(
        config, adapters={"a": first, "b": second}, now=NOW, max_odds_age_seconds=900
    ).route(FIXTURE, now=NOW)
    assert result.observation.provider_identity == "b"
    assert result.trace.attempts[0].state is ProviderState.STALE


def test_all_providers_fail_closed_with_complete_trace():
    config = _config("a", "b", "c", "d")
    adapters = {
        name: FakeAdapter(
            _result(
                name, ProviderState.TEMPORARILY_UNAVAILABLE, reason=f"{name}_timeout"
            )
        )
        for name in config.provider_order
    }
    result = ProviderCascadeRouter(config, adapters=adapters, now=NOW).route(
        FIXTURE, now=NOW
    )
    assert result.observation is None
    assert result.trace.fail_closed is True
    assert result.trace.selected_provider is None
    assert result.trace.fallback_depth is None
    assert len(result.trace.attempts) == 4
    assert all(attempt.network_called for attempt in result.trace.attempts)


def test_disabled_provider_and_candidate_gate_are_explicit():
    config = ProviderCascadeConfig(
        provider_order=("disabled", "candidate"),
        providers={
            "disabled": _provider("disabled", enabled=False),
            "candidate": _provider("candidate"),
        },
        global_request_budget=2,
        per_run_cap=2,
        allow_candidate_only=False,
    )
    disabled = FakeAdapter(_result("disabled"))
    candidate = FakeAdapter(_result("candidate"))
    result = ProviderCascadeRouter(
        config, adapters={"disabled": disabled, "candidate": candidate}, now=NOW
    ).route(FIXTURE, now=NOW)
    assert result.trace.fail_closed
    assert [attempt.state for attempt in result.trace.attempts] == [
        ProviderState.CONFIG_DISABLED,
        ProviderState.CANDIDATE_ONLY,
    ]
    assert disabled.calls == candidate.calls == 0


def test_router_decision_is_deterministic_for_same_inputs_and_config():
    config = _config("a", "b")
    make_adapters = lambda: {
        "a": FakeAdapter(_result("a", ProviderState.RATE_LIMITED)),
        "b": FakeAdapter(_result("b")),
    }
    first = (
        ProviderCascadeRouter(config, adapters=make_adapters(), now=NOW)
        .route(FIXTURE, now=NOW)
        .as_payload()
    )
    second = (
        ProviderCascadeRouter(config, adapters=make_adapters(), now=NOW)
        .route(FIXTURE, now=NOW)
        .as_payload()
    )
    assert first == second


def test_provider_comparison_is_non_authoritative_and_preserves_failure_state():
    report = compare_provider_results(
        FIXTURE,
        {
            "b": _result("b", ProviderState.RATE_LIMITED, reason="http_429"),
            "a": _result("a"),
        },
        now=NOW,
    )
    assert isinstance(report, ProviderComparisonReport)
    assert [metric.provider for metric in report.metrics] == ["a", "b"]
    assert report.metrics[0].coverage is True
    assert report.metrics[1].coverage is False
    assert report.metrics[1].state is ProviderState.RATE_LIMITED
    assert report.metrics[1].provider_error == "http_429"
    assert report.as_payload()["routing_authority"] == "none"


def _raw(payload, *, status=200, headers=None, completed=NOW, error_code=None):
    return RawProviderResponse(
        status, payload, headers or {}, NOW, completed, 11, False, error_code
    )


def _event(
    sport="soccer_epl",
    *,
    event_id="provider-event",
    home="Home FC",
    away="Away FC",
    kickoff=None,
    source=NOW,
):
    return {
        "id": event_id,
        "sport_key": sport,
        "commence_time": (kickoff or FIXTURE.kickoff).isoformat(),
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": "book-a",
                "last_update": source.isoformat() if source else None,
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": source.isoformat() if source else None,
                        "outcomes": [
                            {"name": home, "price": 2.0},
                            {"name": "Draw", "price": 3.5},
                            {"name": away, "price": 4.0},
                        ],
                    }
                ],
            }
        ],
    }


def _adapter_config(name, *, candidate_only=True):
    return _provider(name, candidate_only=candidate_only)


def test_the_odds_api_adapter_normalizes_injected_http_transport():
    calls = []

    def transport(request, timeout):
        calls.append((request, timeout))
        return _raw(
            [_event()], headers={"x-requests-used": "501", "x-requests-remaining": "9"}
        )

    adapter = TheOddsAPIAdapter(transport=transport)
    result = adapter.fetch(
        FIXTURE,
        _adapter_config("the_odds_api"),
        request_identity="request-1",
        requested_at=NOW,
        provider_priority=0,
    )
    assert result.state is ProviderState.AVAILABLE
    assert result.observation.provider_fixture_id == "provider-event"
    assert result.observation.market_type == MARKET_PREMATCH_1X2
    assert result.observation.bookmaker_identity == "book-a"
    assert result.observation.quota_state_after.remaining == 9
    assert calls[0][0].params["apiKey"] == ""
    assert result.observation.candidate_only is True


def test_the_odds_api_adapter_defaults_to_existing_sportsbrain_transport(monkeypatch):
    calls = []

    def legacy_transport(sport, markets, regions, api_key, timeout):
        calls.append((sport, markets, regions, api_key, timeout))
        return ProviderResponse(200, [_event()], {}, NOW, 11)

    monkeypatch.setattr(
        "src.football.provider_cascade.adapters.legacy_requests_transport",
        legacy_transport,
    )
    adapter = TheOddsAPIAdapter()
    result = adapter.fetch(
        FIXTURE,
        _adapter_config("the_odds_api"),
        request_identity="request-legacy",
        requested_at=NOW,
        provider_priority=0,
    )
    assert result.state is ProviderState.AVAILABLE
    assert calls == [("soccer_epl", ("h2h",), ("eu",), "", 5.0)]


def test_odds_api_io_adapter_parses_documented_ml_payload():
    payload = [
        {
            "id": 123,
            "home": "Home FC",
            "away": "Away FC",
            "date": FIXTURE.kickoff.isoformat(),
            "status": "pending",
            "bookmakers": {
                "Bet365": [
                    {
                        "name": "ML",
                        "odds": [{"home": "2.1", "draw": "3.4", "away": "3.2"}],
                        "updatedAt": NOW.isoformat(),
                    }
                ]
            },
        }
    ]
    adapter = OddsApiIoAdapter(transport=lambda request, timeout: _raw(payload))
    result = adapter.fetch(
        FIXTURE,
        _adapter_config("odds_api_io"),
        request_identity="request-2",
        requested_at=NOW,
        provider_priority=1,
        provider_fixture_id="123",
    )
    assert result.state is ProviderState.AVAILABLE
    assert result.observation.home_odds == pytest.approx(2.1)
    assert result.observation.provider_identity == "odds_api_io"


def test_api_football_adapter_separates_fixture_and_odds_and_requires_source_timestamp():
    payload = {
        "paging": {"current": 1, "total": 1},
        "response": [
            {
                "fixture": {"id": 999, "date": FIXTURE.kickoff.isoformat()},
                "teams": {
                    "home": {"id": 1, "name": "Home FC"},
                    "away": {"id": 2, "name": "Away FC"},
                },
                "bookmakers": [
                    {
                        "id": 8,
                        "name": "Book",
                        "bets": [
                            {
                                "id": 1,
                                "name": "Match Winner",
                                "values": [
                                    {"value": "Home", "odd": "2.0"},
                                    {"value": "Draw", "odd": "3.5"},
                                    {"value": "Away", "odd": "4.0"},
                                ],
                            }
                        ],
                    }
                ],
                "updatedAt": NOW.isoformat(),
            }
        ],
    }
    adapter = ApiFootballAdapter(transport=lambda request, timeout: _raw(payload))
    result = adapter.fetch(
        FIXTURE,
        _adapter_config("api_football"),
        request_identity="request-3",
        requested_at=NOW,
        provider_priority=2,
        provider_fixture_id="999",
    )
    assert result.state is ProviderState.AVAILABLE
    assert result.observation.provider_identity == "api_football"
    missing = dict(payload)
    missing["response"] = [{**payload["response"][0], "updatedAt": None}]
    rejected = ApiFootballAdapter(
        transport=lambda request, timeout: _raw(missing)
    ).fetch(
        FIXTURE,
        _adapter_config("api_football"),
        request_identity="request-4",
        requested_at=NOW,
        provider_priority=2,
        provider_fixture_id="999",
    )
    assert rejected.state is ProviderState.STALE
    assert rejected.observation is None


def test_betfair_delayed_adapter_preserves_delayed_semantics():
    payload = {
        "jsonrpc": "2.0",
        "result": [
            {
                "marketId": "1.123",
                "isMarketDataDelayed": True,
                "publishTime": NOW.isoformat(),
                "marketDefinition": {
                    "runners": [
                        {"id": 1, "name": "Home FC"},
                        {"id": 2, "name": "Away FC"},
                        {"id": 3, "name": "Draw"},
                    ]
                },
                "runners": [
                    {"selectionId": 1, "ex": {"availableToBack": [{"price": 2.0}]}},
                    {"selectionId": 2, "ex": {"availableToBack": [{"price": 4.0}]}},
                    {"selectionId": 3, "ex": {"availableToBack": [{"price": 3.5}]}},
                ],
            }
        ],
    }
    adapter = BetfairDelayedAdapter(transport=lambda request, timeout: _raw(payload))
    result = adapter.fetch(
        FIXTURE,
        _adapter_config("betfair_delayed"),
        request_identity="request-5",
        requested_at=NOW,
        provider_priority=3,
        provider_fixture_id="1.123",
    )
    assert result.state is ProviderState.AVAILABLE
    assert result.observation.delayed is True
    assert "Delayed App Key" in result.observation.metadata["delay_semantics"]
    assert result.observation.provider_identity == "betfair_delayed"


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (401, ProviderState.AUTH_FAILED),
        (403, ProviderState.AUTH_FAILED),
        (404, ProviderState.UNSUPPORTED_FIXTURE),
        (422, ProviderState.UNSUPPORTED_MARKET),
        (429, ProviderState.RATE_LIMITED),
        (500, ProviderState.TEMPORARILY_UNAVAILABLE),
        (502, ProviderState.TEMPORARILY_UNAVAILABLE),
        (503, ProviderState.TEMPORARILY_UNAVAILABLE),
    ],
)
def test_http_failure_taxonomy_is_deterministic(status, state):
    adapter = TheOddsAPIAdapter(
        transport=lambda request, timeout: _raw(None, status=status)
    )
    result = adapter.fetch(
        FIXTURE,
        _adapter_config("the_odds_api"),
        request_identity="request-http",
        requested_at=NOW,
        provider_priority=0,
    )
    assert result.state is state
    assert result.network_called is True


def test_chaos_inputs_fail_closed_without_payload_or_secret_leakage():
    for payload, error_code in [
        ("<html>500</html>", "malformed_json"),
        (None, "malformed_json"),
        ([], None),
    ]:
        adapter = TheOddsAPIAdapter(
            transport=lambda request, timeout, payload=payload, error_code=error_code: (
                _raw(payload, error_code=error_code)
            )
        )
        result = adapter.fetch(
            FIXTURE,
            _adapter_config("the_odds_api"),
            request_identity="request-chaos",
            requested_at=NOW,
            provider_priority=0,
        )
        assert result.observation is None
        assert result.state in {
            ProviderState.MALFORMED,
            ProviderState.UNSUPPORTED_FIXTURE,
        }
    request = TheOddsAPIAdapter(transport=lambda request, timeout: _raw([]))._transport
    del request
    secret_request = __import__(
        "src.football.provider_cascade.adapters", fromlist=["ProviderRequest"]
    ).ProviderRequest(
        "the_odds_api",
        "https://example.test/odds",
        {"apiKey": "do-not-print"},
        {"X-Authentication": "secret"},
    )
    safe = str(secret_request.safe_payload())
    assert "do-not-print" not in safe and "secret" not in safe


@pytest.mark.parametrize(
    ("home", "away", "expected"),
    [
        ("Away FC", "Home FC", "swapped_home_away"),
        ("Other FC", "Away FC", "wrong_fixture"),
    ],
)
def test_fixture_identity_rejects_swaps_and_similar_names(home, away, expected):
    adapter = TheOddsAPIAdapter(
        transport=lambda request, timeout: _raw([_event(home=home, away=away)])
    )
    result = adapter.fetch(
        FIXTURE,
        _adapter_config("the_odds_api"),
        request_identity="request-identity",
        requested_at=NOW,
        provider_priority=0,
    )
    assert result.observation is None
    assert expected in result.reason or result.state in {
        ProviderState.QUALITY_REJECTED,
        ProviderState.UNSUPPORTED_FIXTURE,
    }


def test_market_and_timestamp_boundaries_reject_partial_future_and_inplay_data():
    partial = _event()
    partial["bookmakers"][0]["markets"][0]["outcomes"] = partial["bookmakers"][0][
        "markets"
    ][0]["outcomes"][:2]
    result = TheOddsAPIAdapter(
        transport=lambda request, timeout: _raw([partial])
    ).fetch(
        FIXTURE,
        _adapter_config("the_odds_api"),
        request_identity="request-partial",
        requested_at=NOW,
        provider_priority=0,
    )
    assert result.state is ProviderState.QUALITY_REJECTED
    future = _event(source=NOW + timedelta(seconds=1))
    result = TheOddsAPIAdapter(transport=lambda request, timeout: _raw([future])).fetch(
        FIXTURE,
        _adapter_config("the_odds_api"),
        request_identity="request-future",
        requested_at=NOW,
        provider_priority=0,
    )
    assert result.state is ProviderState.QUALITY_REJECTED
    inplay = _event(kickoff=NOW - timedelta(minutes=1))
    result = TheOddsAPIAdapter(transport=lambda request, timeout: _raw([inplay])).fetch(
        FIXTURE,
        _adapter_config("the_odds_api"),
        request_identity="request-inplay",
        requested_at=NOW,
        provider_priority=0,
    )
    assert result.state is ProviderState.UNSUPPORTED_FIXTURE


def test_budget_tracks_attempts_successes_preflight_rejections_and_quota():
    config = _config("a", "b")
    budget = RequestBudgetManager(config, now=NOW)
    assert budget.preflight("a", now=NOW).allowed
    budget.record_attempt("a", at=NOW)
    budget.record_result(
        "a",
        state=ProviderState.AVAILABLE,
        at=NOW,
        quota_after=QuotaSnapshot(remaining=8),
    )
    rejected = budget.preflight("a", now=NOW)
    assert rejected.allowed is False
    payload = budget.as_payload()
    assert payload["global_requests_attempted"] == 1
    assert payload["providers"]["a"]["requests_successful"] == 1
    assert payload["providers"]["a"]["rejected_before_network"] == 1
    assert payload["providers"]["a"]["quota"]["remaining"] == 8


def test_builder1_seam_only_accepts_non_fail_closed_normalized_observation():
    config = _config("a")
    result = ProviderCascadeRouter(
        config, adapters={"a": FakeAdapter(_result("a"))}, now=NOW
    ).route(FIXTURE, now=NOW)
    input_value = accepted_for_builder1(result)
    fixture, snapshot = input_value.model_inputs()
    assert fixture.fixture_key == FIXTURE.fixture_key
    assert snapshot.odds["draw"] == 3.5
    failed = CascadeResult(
        None, replace(result.trace, selected_provider=None, fail_closed=True)
    )
    with pytest.raises(ValueError, match="failed closed"):
        accepted_for_builder1(failed)


def test_health_payload_is_secret_free_and_tracks_candidate_state():
    config = _config("a")
    adapter = FakeAdapter(_result("a"))
    router = ProviderCascadeRouter(config, adapters={"a": adapter}, now=NOW)
    router.route(FIXTURE, now=NOW)
    health = router.health.as_payload()["a"]
    assert health["candidate_only"] is True
    assert health["validated"] is False
    assert health["rolling_availability"] == [True]
    assert "secret" not in str(health)

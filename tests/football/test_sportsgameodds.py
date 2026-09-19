from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import Fixture
from src.football.provider_cascade import (
    MARKET_PREMATCH_1X2,
    CascadeTimingPolicy,
    NetworkAuthorizationContract,
    ProviderState,
    RawProviderResponse,
    SportsGameOddsAdapter,
    SportsGameOddsDiagnosticClient,
    SportsGameOddsError,
    build_sportsgameodds_provider_config,
)
from src.football.provider_cascade.contracts import FOOTBALL_PROVIDER_REPERTOIRE

UTC = timezone.utc
NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
KICKOFF = NOW + timedelta(hours=2)
FIXTURE = Fixture("cl-fixture-1", "CL", "Paris Saint-Germain", "Arsenal", KICKOFF)
TIMING = CascadeTimingPolicy(900, 90)
AUTH = NetworkAuthorizationContract(
    controlled_shadow_run_ref="app-b3-diagnostic-test",
    authorized_providers=("sportsgameodds",),
)


def _config(*, enabled: bool = True, remaining: int | None = 10):
    base = build_sportsgameodds_provider_config(enabled=enabled)
    return replace(
        base,
        credentials_required=False,
        credential_available=True,
        initial_quota=replace(base.initial_quota, remaining=remaining),
    )


def _odd(
    market_id: str,
    side: str,
    *,
    bookmaker_rows: dict[str, dict] | list[dict] | None = None,
    period: str = "reg",
    bet_type: str = "ml3way",
):
    return {
        "oddID": market_id,
        "statID": "points",
        "statEntityID": "all" if side == "draw" else side,
        "periodID": period,
        "betTypeID": bet_type,
        "sideID": side,
        "marketName": "3-Way Moneyline (Regulation)",
        "byBookmaker": bookmaker_rows
        if bookmaker_rows is not None
        else {
            "bet365": {
                "bookmakerID": "bet365",
                "odds": "+125"
                if side == "home"
                else "+250"
                if side == "draw"
                else "+210",
                "available": True,
                "lastUpdatedAt": (NOW - timedelta(seconds=30)).isoformat(),
            }
        },
    }


def _event(
    *,
    league: str = "UEFA_CHAMPIONS_LEAGUE",
    home: str = "Paris Saint-Germain",
    away: str = "Arsenal",
    event_id: str = "sgo-cl-event-1",
    odds: dict | None = None,
    status: dict | None = None,
):
    return {
        "eventID": event_id,
        "sportID": "SOCCER",
        "leagueID": league,
        "type": "match",
        "teams": {
            "home": {"teamID": "PSG_UEFA_CHAMPIONS_LEAGUE", "names": {"long": home}},
            "away": {
                "teamID": "ARSENAL_UEFA_CHAMPIONS_LEAGUE",
                "names": {"long": away},
            },
        },
        "status": {
            "started": False,
            "live": False,
            "cancelled": False,
            "startsAt": KICKOFF.isoformat(),
            **(status or {}),
        },
        "odds": odds
        if odds is not None
        else {
            "points-home-reg-ml3way-home": _odd("points-home-reg-ml3way-home", "home"),
            "points-all-reg-ml3way-draw": _odd("points-all-reg-ml3way-draw", "draw"),
            "points-away-reg-ml3way-away": _odd("points-away-reg-ml3way-away", "away"),
        },
    }


def _normalize(event=None, *, adapter=None, **kwargs):
    return (adapter or SportsGameOddsAdapter()).normalize_event(
        FIXTURE,
        event or _event(),
        config=_config(),
        requested_at=NOW - timedelta(seconds=1),
        captured_at=NOW,
        **kwargs,
    )


def _raw(payload: object, *, status: int = 200) -> RawProviderResponse:
    return RawProviderResponse(status, payload, {}, NOW, NOW, 0)


def test_valid_champions_league_regulation_1x2_normalizes_to_existing_contract():
    observations = _normalize()
    assert len(observations) == 1
    observation = observations[0]
    observation.validate(now=NOW)
    assert observation.league_code == "CL"
    assert observation.provider_fixture_id == "sgo-cl-event-1"
    assert observation.market_type == MARKET_PREMATCH_1X2
    assert observation.bookmaker_identity == "bet365"
    assert observation.home_odds == pytest.approx(2.25)
    assert observation.draw_odds == pytest.approx(3.5)
    assert observation.away_odds == pytest.approx(3.1)
    assert observation.source_timestamp == NOW - timedelta(seconds=30)
    assert observation.candidate_only is True
    assert (
        observation.metadata["regulation_semantics"] == "90_minutes_plus_stoppage_time"
    )


def test_multiple_bookmakers_are_sorted_and_coverage_is_retained():
    rows = {
        "pinnacle": {
            "bookmakerID": "pinnacle",
            "odds": "2.10",
            "available": True,
            "lastUpdatedAt": (NOW - timedelta(seconds=10)).isoformat(),
        },
        "bet365": {
            "bookmakerID": "bet365",
            "odds": "+125",
            "available": True,
            "lastUpdatedAt": (NOW - timedelta(seconds=20)).isoformat(),
        },
    }
    odds = {
        "points-home-reg-ml3way-home": _odd(
            "points-home-reg-ml3way-home", "home", bookmaker_rows=rows
        ),
        "points-all-reg-ml3way-draw": _odd(
            "points-all-reg-ml3way-draw", "draw", bookmaker_rows=rows
        ),
        "points-away-reg-ml3way-away": _odd(
            "points-away-reg-ml3way-away", "away", bookmaker_rows=rows
        ),
    }
    observations = _normalize(_event(odds=odds))
    assert [item.bookmaker_identity for item in observations] == ["bet365", "pinnacle"]
    assert observations[0].metadata["bookmaker_count"] == 2
    assert observations[0].metadata["available_bookmakers"] == ["bet365", "pinnacle"]


def test_team_aliases_and_provider_team_ids_are_preserved():
    adapter = SportsGameOddsAdapter(aliases={"psg": "paris saint germain"})
    event = _event(home="PSG")
    observations = _normalize(event, adapter=adapter)
    assert observations[0].home_team == FIXTURE.home_team
    assert observations[0].metadata["team_ids"]["home"] == "PSG_UEFA_CHAMPIONS_LEAGUE"


def test_full_game_market_is_not_confused_with_regulation_1x2():
    full_game = {
        "points-home-game-ml3way-home": _odd(
            "points-home-game-ml3way-home", "home", period="game"
        ),
        "points-all-game-ml3way-draw": _odd(
            "points-all-game-ml3way-draw", "draw", period="game"
        ),
        "points-away-game-ml3way-away": _odd(
            "points-away-game-ml3way-away", "away", period="game"
        ),
    }
    with pytest.raises(SportsGameOddsError, match="full_game_market_not_regulation"):
        _normalize(_event(odds=full_game))


@pytest.mark.parametrize(
    ("odds", "reason"),
    [
        (
            {
                "points-home-reg-ml3way-home": _odd(
                    "points-home-reg-ml3way-home", "home"
                ),
                "points-away-reg-ml3way-away": _odd(
                    "points-away-reg-ml3way-away", "away"
                ),
            },
            "missing_regulation_1x2",
        ),
        (
            {
                "points-home-reg-ml3way-home": _odd(
                    "points-home-reg-ml3way-home", "home"
                ),
                "points-all-reg-ml3way-draw": _odd(
                    "points-all-reg-ml3way-draw",
                    "draw",
                    bookmaker_rows={
                        "bet365": {
                            "bookmakerID": "bet365",
                            "odds": "not-a-price",
                            "available": True,
                            "lastUpdatedAt": NOW.isoformat(),
                        }
                    },
                ),
                "points-away-reg-ml3way-away": _odd(
                    "points-away-reg-ml3way-away", "away"
                ),
            },
            "malformed_or_incomplete_1x2",
        ),
    ],
)
def test_missing_draw_and_malformed_price_fail_closed(odds, reason):
    with pytest.raises(SportsGameOddsError, match=reason):
        _normalize(_event(odds=odds))


def test_stale_source_timestamp_fails_closed():
    stale = (NOW - timedelta(hours=2)).isoformat()
    odds = {
        market_id: _odd(
            market_id,
            side,
            bookmaker_rows={
                "bet365": {
                    "bookmakerID": "bet365",
                    "odds": "2.2",
                    "available": True,
                    "lastUpdatedAt": stale,
                }
            },
        )
        for market_id, side in (
            ("points-home-reg-ml3way-home", "home"),
            ("points-all-reg-ml3way-draw", "draw"),
            ("points-away-reg-ml3way-away", "away"),
        )
    }
    with pytest.raises(SportsGameOddsError, match="stale_observation"):
        _normalize(_event(odds=odds))


@pytest.mark.parametrize(
    ("league", "home", "away", "reason"),
    [
        ("EPL", "Paris Saint-Germain", "Arsenal", "wrong_league"),
        ("UEFA_CHAMPIONS_LEAGUE", "Bayern", "Arsenal", "fixture_mismatch"),
    ],
)
def test_wrong_league_or_fixture_fails_closed(league, home, away, reason):
    with pytest.raises(SportsGameOddsError, match=reason):
        _normalize(_event(league=league, home=home, away=away))


def test_duplicate_bookmaker_outcome_fails_closed():
    duplicate = [
        {
            "bookmakerID": "bet365",
            "odds": "2.2",
            "available": True,
            "lastUpdatedAt": NOW.isoformat(),
        },
        {
            "bookmakerID": "bet365",
            "odds": "2.3",
            "available": True,
            "lastUpdatedAt": NOW.isoformat(),
        },
    ]
    odds = {
        "points-home-reg-ml3way-home": _odd(
            "points-home-reg-ml3way-home", "home", bookmaker_rows=duplicate
        ),
        "points-all-reg-ml3way-draw": _odd("points-all-reg-ml3way-draw", "draw"),
        "points-away-reg-ml3way-away": _odd("points-away-reg-ml3way-away", "away"),
    }
    with pytest.raises(SportsGameOddsError, match="duplicate_bookmaker_outcome"):
        _normalize(_event(odds=odds))


def test_injected_evidence_is_explicitly_non_authoritative():
    observation = _normalize(evidence_kind="TEST_INJECTED")[0]
    assert observation.candidate_only is True
    assert observation.metadata["evidence_kind"] == "TEST_INJECTED"
    assert "sportsgameodds" not in FOOTBALL_PROVIDER_REPERTOIRE


def test_adapter_fetch_fails_closed_for_auth_and_quota_before_network():
    calls = []
    adapter = SportsGameOddsAdapter(
        transport=lambda request, timeout: calls.append(request)
    )
    result = adapter.fetch(
        FIXTURE,
        _config(remaining=0),
        request_identity="request-1",
        requested_at=NOW,
        provider_priority=0,
        timing_policy=TIMING,
        authorization=AUTH,
    )
    assert result.state is ProviderState.QUOTA_EXHAUSTED
    assert result.network_called is False
    assert calls == []
    result = adapter.fetch(
        FIXTURE,
        _config(remaining=10),
        request_identity="request-2",
        requested_at=NOW,
        provider_priority=0,
        timing_policy=TIMING,
        authorization=None,
    )
    assert result.state is ProviderState.HEALTH_UNKNOWN
    assert result.network_called is False


def test_adapter_fetch_classifies_auth_failure_without_exposing_request_secret():
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return _raw({"success": False, "error": "bad key"}, status=401)

    adapter = SportsGameOddsAdapter(transport=transport)
    config = _config(remaining=10)
    result = adapter.fetch(
        FIXTURE,
        config,
        request_identity="request-3",
        requested_at=NOW,
        provider_priority=0,
        timing_policy=TIMING,
        authorization=AUTH,
    )
    assert result.state is ProviderState.AUTH_FAILED
    assert calls[0].safe_payload()["headers"]["X-Api-Key"] == "[REDACTED]"


def test_adapter_fetch_uses_existing_network_contract_but_returns_candidate_only():
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return _raw({"success": True, "data": [_event()]})

    adapter = SportsGameOddsAdapter(transport=transport)
    result = adapter.fetch(
        FIXTURE,
        _config(remaining=10),
        request_identity="request-4",
        requested_at=NOW,
        provider_priority=0,
        provider_fixture_id="sgo-cl-event-1",
        timing_policy=TIMING,
        authorization=AUTH,
    )
    assert result.state is ProviderState.AVAILABLE
    assert result.observation is not None
    assert result.observation.candidate_only is True
    assert calls[0].params["leagueID"] == "UEFA_CHAMPIONS_LEAGUE"
    assert calls[0].params["oddID"] == (
        "points-home-reg-ml3way-home,"
        "points-all-reg-ml3way-draw,"
        "points-away-reg-ml3way-away"
    )


def _usage(entities: int):
    return {
        "success": True,
        "data": {
            "tier": "amateur",
            "isActive": True,
            "email": "must-not-escape@example.invalid",
            "keyID": "must-not-escape",
            "rateLimits": {
                "per-month": {
                    "max-requests": "unlimited",
                    "current-requests": 3,
                    "max-entities": 2500,
                    "current-entities": entities,
                }
            },
        },
    }


def test_bounded_diagnostic_uses_five_requests_and_redacts_usage():
    payloads = [
        _usage(10),
        {"success": True, "data": [{"leagueID": "UEFA_CHAMPIONS_LEAGUE"}]},
        {
            "success": True,
            "data": [{"periodID": "reg", "betTypeID": "ml3way", "sideID": "draw"}],
        },
        {"success": True, "data": [_event()]},
        _usage(11),
    ]
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return _raw(payloads[len(calls) - 1])

    result = SportsGameOddsDiagnosticClient(
        api_key="test-only-key", transport=transport, max_requests=5
    ).run(now=NOW)
    assert result.status == "COMPLETED"
    assert result.request_count == 5
    assert result.objects_returned == 1
    assert result.objects_consumed == 1
    assert result.league_available is True
    assert result.market_available is True
    assert result.bookmakers == ("bet365",)
    assert result.usage_before == {
        "tier": "amateur",
        "is_active": True,
        "rate_limits": {
            "per-month": {
                "max-requests": "unlimited",
                "current-requests": 3,
                "max-entities": 2500,
                "current-entities": 10,
            }
        },
    }
    assert "email" not in result.usage_before
    assert calls[0].safe_payload()["headers"]["X-Api-Key"] == "[REDACTED]"


def test_diagnostic_without_environment_key_is_real_test_ready_and_makes_zero_calls():
    calls = []
    result = SportsGameOddsDiagnosticClient(
        api_key="", transport=lambda request, timeout: calls.append(request)
    ).run(now=NOW)
    assert result.status == "REAL_TEST_READY"
    assert result.request_count == 0
    assert calls == []


def test_disabled_config_is_not_an_active_provider_registration():
    config = build_sportsgameodds_provider_config()
    assert config.enabled is False
    assert config.shadow_only is True
    assert config.candidate_only is True
    assert config.quality_eligible is False
    assert config.market_allowlist == (MARKET_PREMATCH_1X2,)

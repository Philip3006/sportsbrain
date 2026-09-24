from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_the_odds_api_fixture_source import (
    DISCOVERY_LEAGUE_ORDER,
    THE_ODDS_API_FIXTURE_MARKETS,
    THE_ODDS_API_FIXTURE_REGIONS,
    THE_ODDS_API_FIXTURE_SOURCE,
    TOP5_SPORT_KEYS,
    TheOddsApiCurrentFixtureAcquisitionV1,
    TheOddsApiFixtureSourceError,
    fetch_current_top5_fixture_acquisition,
    fixture_from_the_odds_api_record,
)
from src.football.top5_therundown_event_discovery import (
    EventDiscoveryExecutionBlocked,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _record(league: str, *, kickoff: datetime = NOW + timedelta(hours=2)) -> dict:
    return {
        "id": f"odds-api-{league}",
        "sport_key": TOP5_SPORT_KEYS[league],
        "home_team": f"Home {league}",
        "away_team": f"Away {league}",
        "commence_time": kickoff.isoformat().replace("+00:00", "Z"),
    }


def test_odds_api_record_maps_to_canonical_fixture_key_and_utc_kickoff():
    fixture = fixture_from_the_odds_api_record(_record("EPL"), league="EPL")
    assert fixture.league_code == "EPL"
    assert fixture.home_team == "Home EPL"
    assert fixture.away_team == "Away EPL"
    assert fixture.kickoff.tzinfo is not None
    assert fixture.fixture_key == make_fixture_key(
        "EPL", "Home EPL", "Away EPL", NOW + timedelta(hours=2)
    )


@pytest.mark.parametrize(
    "record, league, match",
    [
        ({**_record("EPL"), "commence_time": "not-a-time"}, "EPL", "commence_time"),
        ({**_record("EPL"), "home_team": ""}, "EPL", "home team"),
        ({**_record("EPL"), "away_team": None}, "EPL", "away team"),
        ({**_record("EPL"), "sport_key": "soccer_spain_la_liga"}, "EPL", "sport key"),
        (_record("EPL"), "LL", "sport key"),
    ],
)
def test_malformed_or_wrong_league_fixture_records_fail_closed(record, league, match):
    with pytest.raises(TheOddsApiFixtureSourceError, match=match):
        fixture_from_the_odds_api_record(record, league=league)


def test_acquisition_reuses_existing_odds_api_path_and_has_exact_five_logical_calls(
    monkeypatch,
):
    calls: list[dict] = []

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        league = next(
            code for code, key in TOP5_SPORT_KEYS.items() if key == kwargs["sport"]
        )
        return [_record(league)]

    monkeypatch.setattr(
        "src.football.top5_the_odds_api_fixture_source.fetch_upcoming_matches",
        fake_fetch,
    )
    acquisition = fetch_current_top5_fixture_acquisition(api_key="injected-only")
    assert acquisition.request_count == 5
    assert [call["sport"] for call in calls] == [
        TOP5_SPORT_KEYS[league] for league in DISCOVERY_LEAGUE_ORDER
    ]
    assert all(call["markets"] == THE_ODDS_API_FIXTURE_MARKETS for call in calls)
    assert all(call["regions"] == THE_ODDS_API_FIXTURE_REGIONS for call in calls)
    assert all(call["force"] is True for call in calls)
    assert all(call["api_key"] == "injected-only" for call in calls)


def test_acquisition_stops_on_first_source_failure(monkeypatch):
    calls: list[str] = []

    def fake_fetch(**kwargs):
        calls.append(kwargs["sport"])
        if len(calls) == 2:
            raise RuntimeError("injected source failure")
        league = next(
            code for code, key in TOP5_SPORT_KEYS.items() if key == kwargs["sport"]
        )
        return [_record(league)]

    monkeypatch.setattr(
        "src.football.top5_the_odds_api_fixture_source.fetch_upcoming_matches",
        fake_fetch,
    )
    with pytest.raises(RuntimeError, match="injected source failure"):
        fetch_current_top5_fixture_acquisition()
    assert calls == [TOP5_SPORT_KEYS["EPL"], TOP5_SPORT_KEYS["BL1"]]


def test_acquisition_batch_builds_manifest_without_provider_ids():
    fixtures = tuple(
        fixture_from_the_odds_api_record(_record(league), league=league)
        for league in DISCOVERY_LEAGUE_ORDER
    )
    acquisition = TheOddsApiCurrentFixtureAcquisitionV1(
        fixtures=fixtures,
        observed_at=NOW - timedelta(minutes=1),
        source_provenance=THE_ODDS_API_FIXTURE_SOURCE,
        source_release_sha="a" * 40,
        runtime_data_sha=None,
        request_count=5,
    )
    manifest = acquisition.build_manifest(now=NOW)
    assert [target.league for target in manifest.targets] == list(
        DISCOVERY_LEAGUE_ORDER
    )
    assert all(target.home_participant_id is None for target in manifest.targets)
    assert all(target.away_participant_id is None for target in manifest.targets)


def test_acquisition_rejects_wrong_request_scope():
    fixtures = tuple(
        fixture_from_the_odds_api_record(_record(league), league=league)
        for league in DISCOVERY_LEAGUE_ORDER
    )
    acquisition = TheOddsApiCurrentFixtureAcquisitionV1(
        fixtures=fixtures,
        observed_at=NOW - timedelta(minutes=1),
        source_provenance=THE_ODDS_API_FIXTURE_SOURCE,
        source_release_sha="a" * 40,
        runtime_data_sha=None,
        request_count=4,
    )
    with pytest.raises(EventDiscoveryExecutionBlocked, match="exactly five"):
        acquisition.validate()

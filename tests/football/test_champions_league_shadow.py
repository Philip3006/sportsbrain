"""Offline/replay-only coverage for the Champions League shadow seam."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.provider_cascade.champions_league_shadow import (
    CHAMPIONS_LEAGUE_CODE,
    CHAMPIONS_LEAGUE_SPORT_KEY,
    CL_SOURCE_MATRIX,
    CLEventState,
    CLFixture,
    CLReplayError,
    CLQuotaMetadata,
    CLRejectionReason,
    CLProvenance,
    CLShadowObservation,
    CLShadowPolicy,
    CLSourceStatus,
    REGULATION_1X2_MARKET,
    replay_cl_shadow,
)

BASE = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
KICKOFF = BASE + timedelta(hours=4)
CAPTURED = BASE + timedelta(minutes=30)
POLICY = CLShadowPolicy(maximum_odds_age_seconds=900, kickoff_tolerance_seconds=120)


def _fixture(key: str = "ucl:2026:001") -> CLFixture:
    return CLFixture(
        fixture_key=key,
        provider_event_id="toa-ucl-001",
        home_team="Paris SG",
        away_team="Arsenal",
        kickoff_utc=KICKOFF,
    )


def _observation(fixture: CLFixture | None = None, *, bookmaker: str = "pinnacle") -> CLShadowObservation:
    fixture = fixture or _fixture()
    return CLShadowObservation(
        fixture_key=fixture.fixture_key,
        competition_code=CHAMPIONS_LEAGUE_CODE,
        provider_event_id=fixture.provider_event_id or "toa-ucl-001",
        home_team="Paris Saint-Germain",
        away_team="Arsenal",
        kickoff_utc=fixture.kickoff_utc,
        market_type=REGULATION_1X2_MARKET,
        home_odds=2.10,
        draw_odds=3.50,
        away_odds=3.20,
        bookmaker_identity=bookmaker,
        source_timestamp=CAPTURED - timedelta(minutes=2),
        captured_at=CAPTURED,
        provider_identity="the_odds_api",
        request_identity=f"replay:{fixture.fixture_key}:{bookmaker}",
        provenance=CLProvenance(
            source="the_odds_api",
            source_uri="https://api.the-odds-api.com/v4/sports/soccer_uefa_champs_league/odds",
            raw_record_id=f"toa:{fixture.provider_event_id}:{bookmaker}",
            adapter_version="cl-shadow-replay-v1",
            payload_digest="a" * 64,
        ),
        quota=CLQuotaMetadata(
            provider="the_odds_api",
            quota_before_remaining=20,
            quota_after_remaining=19,
            quota_used=501,
            rate_limit_remaining=9,
            request_cost_units=1.0,
            quota_reset_at=BASE + timedelta(days=12),
            network_request_count=0,
        ),
    )


def _reasons(result):
    return {reason for item in result.rejected_observations for reason in item.reasons}


def test_matrix_identifies_the_only_current_ucl_candidate_without_authorizing_it():
    by_source = {row.source: row for row in CL_SOURCE_MATRIX}
    assert by_source["the_odds_api"].status is CLSourceStatus.IMPLEMENTED_CANDIDATE
    assert by_source["the_odds_api"].competition_key == CHAMPIONS_LEAGUE_SPORT_KEY
    assert by_source["the_odds_api"].regulation_1x2 is True
    assert by_source["api_football"].status is CLSourceStatus.IMPLEMENTED_NOT_UCL_MAPPED
    assert by_source["odds_api_io"].status is CLSourceStatus.DECOMMISSIONED


def test_valid_replay_retains_each_bookmaker_and_quota_metadata():
    fixture = _fixture()
    observations = (_observation(fixture), _observation(fixture, bookmaker="bet365"))
    result = replay_cl_shadow([fixture], observations, policy=POLICY)

    assert result.network_called is False
    assert result.coverage["accepted_count"] == 2
    assert result.retained_bookmakers[fixture.fixture_key] == ("bet365", "pinnacle")
    assert {item.bookmaker_identity for item in result.accepted_observations} == {"bet365", "pinnacle"}
    assert result.accepted_observations[0].quota.quota_after_remaining == 19
    assert result.as_payload()["accepted_observations"][0]["quota"]["request_cost_units"] == 1.0


def test_replay_digest_is_stable_when_fixture_and_observation_order_changes():
    fixture_a = _fixture("ucl:2026:001")
    fixture_b = replace(_fixture("ucl:2026:002"), provider_event_id="toa-ucl-002")
    rows = (_observation(fixture_a), _observation(fixture_b, bookmaker="bet365"))
    first = replay_cl_shadow([fixture_a, fixture_b], rows, policy=POLICY)
    second = replay_cl_shadow([fixture_b, fixture_a], tuple(reversed(rows)), policy=POLICY)
    assert first.as_payload() == second.as_payload()


def test_exact_provider_fixture_identity_is_required():
    fixture = _fixture()
    result = replay_cl_shadow(
        [fixture], [replace(_observation(fixture), provider_event_id="toa-other")], policy=POLICY
    )
    assert CLRejectionReason.FIXTURE_ID_MISMATCH in _reasons(result)


def test_participant_mismatch_is_rejected_even_when_fixture_key_matches():
    fixture = _fixture()
    result = replay_cl_shadow(
        [fixture], [replace(_observation(fixture), away_team="Real Madrid")], policy=POLICY
    )
    assert CLRejectionReason.PARTICIPANT_MISMATCH in _reasons(result)


def test_complete_regulation_1x2_is_required():
    fixture = _fixture()
    result = replay_cl_shadow(
        [fixture], [replace(_observation(fixture), draw_odds=None)], policy=POLICY
    )
    assert CLRejectionReason.INCOMPLETE_REGULATION_1X2 in _reasons(result)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"event_state": CLEventState.STALE}, CLRejectionReason.STALE_EVENT),
        ({"in_play": True}, CLRejectionReason.IN_PLAY),
        ({"source_timestamp": CAPTURED - timedelta(hours=1)}, CLRejectionReason.STALE_EVENT),
        ({"captured_at": KICKOFF + timedelta(seconds=1)}, CLRejectionReason.IN_PLAY),
    ],
)
def test_stale_and_in_play_events_fail_closed(changes, reason):
    fixture = _fixture()
    result = replay_cl_shadow([fixture], [replace(_observation(fixture), **changes)], policy=POLICY)
    assert reason in _reasons(result)


def test_timestamps_provenance_and_quota_metadata_are_required():
    fixture = _fixture()
    malformed = replace(_observation(fixture), source_timestamp=None, provenance=None, quota=None)
    result = replay_cl_shadow([fixture], [malformed], policy=POLICY)
    reasons = _reasons(result)
    assert CLRejectionReason.MISSING_TIMESTAMP in reasons
    assert CLRejectionReason.MISSING_PROVENANCE in reasons
    assert CLRejectionReason.MISSING_QUOTA_METADATA in reasons


def test_duplicate_fixture_definitions_fail_closed():
    fixture = _fixture()
    with pytest.raises(CLReplayError, match="duplicate fixture"):
        replay_cl_shadow([fixture, fixture], [_observation(fixture)], policy=POLICY)


def test_duplicate_observation_is_reported_without_overwriting_first_record():
    fixture = _fixture()
    row = _observation(fixture)
    result = replay_cl_shadow([fixture], [row, row], policy=POLICY)
    assert result.coverage["accepted_count"] == 1
    assert result.coverage["duplicate_rejections"] == 1
    assert CLRejectionReason.DUPLICATE_OBSERVATION in _reasons(result)

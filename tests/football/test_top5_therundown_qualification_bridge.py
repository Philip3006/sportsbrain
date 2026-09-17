"""No-network tests for the APP-B1 TheRundown evidence bridge."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import Fixture, ProductionContractError
from src.football.provider_cascade.adapters import AdapterResult
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    NormalizedOddsObservation,
    ProviderState,
    QuotaSnapshot,
    TimingProvenance,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ObservationEvidenceKind,
)
from src.football.top5_therundown_qualification import (
    TheRundownQualificationStatus,
    evaluate_therundown_qualification,
)
from src.football.top5_therundown_qualification_bridge import (
    TheRundownBridgeError,
    bridge_therundown_observation,
)

UTC = timezone.utc
CAPTURED_AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
KICKOFF = CAPTURED_AT + timedelta(hours=2)
FIXTURE = Fixture(
    fixture_key="EPL|Home FC|Away FC|2026-09-18T14:00:00+00:00",
    league_code="EPL",
    home_team="Home FC",
    away_team="Away FC",
    kickoff=KICKOFF,
)
BEFORE = QuotaSnapshot(used=10, remaining=490, rate_limit=10, rate_remaining=9)
AFTER = QuotaSnapshot(used=11, remaining=489, rate_limit=10, rate_remaining=8)


def _authorization(*, network_execution: bool = True) -> dict[str, object]:
    return {
        "controlled_shadow_run_id": "controlled-run-1",
        "qualification_session_id": "qualification-session-1",
        "ceo_authorization_id": "ceo-authorization-1",
        "provider_identity": "therundown_experimental",
        "canonical_league": FIXTURE.league_code,
        "fixture_key": FIXTURE.fixture_key,
        "provider_event_id": "therundown-event-1",
        "provider_request_id": "therundown-request-1",
        "provider_scope": ["therundown_experimental"],
        "league_scope": [FIXTURE.league_code],
        "fixture_scope": [FIXTURE.fixture_key],
        "network_execution": network_execution,
        "no_bet": True,
        "publication_enabled": False,
        "monetary_spend_authorized": False,
    }


def _result(
    *,
    observation: NormalizedOddsObservation | None = None,
    network_called: bool = True,
    status_code: int | None = 200,
) -> AdapterResult:
    observation = observation or NormalizedOddsObservation(
        league_code=FIXTURE.league_code,
        fixture_key=FIXTURE.fixture_key,
        provider_fixture_id="therundown-event-1",
        home_team=FIXTURE.home_team,
        away_team=FIXTURE.away_team,
        kickoff_utc=FIXTURE.kickoff,
        market_type=MARKET_PREMATCH_1X2,
        home_odds=2.1,
        draw_odds=3.4,
        away_odds=3.2,
        provider_identity="therundown_experimental",
        bookmaker_identity="affiliate:bookmaker-1",
        source_timestamp=CAPTURED_AT - timedelta(minutes=2),
        captured_at=CAPTURED_AT,
        request_identity="therundown-request-1",
        request_started_at=CAPTURED_AT - timedelta(seconds=3),
        request_completed_at=CAPTURED_AT,
        latency_ms=3000,
        provider_priority=0,
        fallback_depth=0,
        quota_state_before=BEFORE,
        quota_state_after=AFTER,
        rate_limit_state=AFTER,
        source_provenance="therundown:v2:event-1;affiliate=bookmaker-1",
        raw_record_digest="d" * 64,
        adapter_version="therundown-v2-experimental:1",
        source_timing_provenance=TimingProvenance.SOURCE_TIMESTAMP,
    )
    return AdapterResult(
        state=ProviderState.AVAILABLE,
        reason="accepted_candidate_only",
        observation=observation,
        status_code=status_code,
        network_called=network_called,
        latency_ms=3000,
        quota_after=AFTER,
        rate_limit_state=AFTER,
        raw_response_digest="a" * 64,
        normalized_record_digest="b" * 64,
    )


def _bridge(
    result: AdapterResult | None = None,
    *,
    evidence_id: str = "b1-evidence-1",
    observation_id: str = "observation-1",
    adapter_source_sha: str = "c" * 40,
    evidence_kind: ObservationEvidenceKind
    | str = ObservationEvidenceKind.REAL_OBSERVED,
    synthetic_reconstruction: bool = False,
    network_request_count: int = 1,
    authorization_metadata: dict[str, object] | None = None,
    **changes: object,
) -> dict[str, object]:
    return bridge_therundown_observation(
        result or _result(network_called=network_request_count == 1),
        expected_fixture=FIXTURE,
        evidence_id=evidence_id,
        observation_id=observation_id,
        provider_league_code="EPL",
        provider_league_identity_verified=True,
        evidence_kind=evidence_kind,
        synthetic_reconstruction=synthetic_reconstruction,
        network_request_count=network_request_count,
        maximum_odds_age_seconds=300,
        quota_cost_units=1.0,
        adapter_source_sha=adapter_source_sha,
        authorization_metadata=authorization_metadata
        or _authorization(
            network_execution=evidence_kind == ObservationEvidenceKind.REAL_OBSERVED
        ),
        **changes,
    )


def test_real_adapter_observation_maps_to_qualified_b1_evidence() -> None:
    evidence = _bridge()
    report = evaluate_therundown_qualification(
        {
            "schema_version": evidence["schema_version"],
            "provider_identity": evidence["provider_identity"],
            "evidence": [evidence],
        },
        maximum_odds_age_seconds=300,
    )

    assert report.status is TheRundownQualificationStatus.PARTIAL_EVIDENCE
    league = next(item for item in report.leagues if item.league == "EPL")
    assert league.status is TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
    assert evidence["provider_event_id"] == "therundown-event-1"
    assert evidence["provider_request_id"] == "therundown-request-1"
    assert evidence["raw_record_digest"] == "a" * 64
    assert evidence["provider_record_digest"] == "d" * 64
    assert evidence["normalized_record_digest"] == "b" * 64
    assert evidence["adapter_source_sha"] == "c" * 40
    assert evidence["authorization_metadata"] == _authorization()


def test_test_fixture_is_explicit_and_cannot_qualify() -> None:
    evidence = _bridge(
        network_request_count=0,
        evidence_kind=ObservationEvidenceKind.TEST_FIXTURE,
        synthetic_reconstruction=True,
    )
    report = evaluate_therundown_qualification(
        {
            "schema_version": evidence["schema_version"],
            "provider_identity": evidence["provider_identity"],
            "evidence": [evidence],
        },
        maximum_odds_age_seconds=300,
    )

    league = next(item for item in report.leagues if item.league == "EPL")
    assert evidence["evidence_kind"] == "TEST_FIXTURE"
    assert league.status is TheRundownQualificationStatus.FAILED
    assert not league.qualified_evidence_ids


@pytest.mark.parametrize(
    "field",
    [
        "bookmaker_identity",
        "source_timestamp",
        "raw_response_digest",
        "normalized_record_digest",
        "adapter_source_sha",
    ],
)
def test_missing_bridge_provenance_fails_closed(field: str) -> None:
    result = _result()
    if field == "bookmaker_identity":
        observation = replace(result.observation, bookmaker_identity="")
        result = replace(result, observation=observation)
    elif field == "source_timestamp":
        observation = replace(result.observation, source_timestamp=None)
        result = replace(result, observation=observation)
    elif field == "raw_response_digest":
        result = replace(result, raw_response_digest="")
    elif field == "normalized_record_digest":
        result = replace(result, normalized_record_digest="")

    with pytest.raises((TheRundownBridgeError, ProductionContractError)):
        if field == "adapter_source_sha":
            _bridge(adapter_source_sha="bad")
        else:
            _bridge(result=result)


def test_stale_odds_and_incomplete_1x2_fail_closed() -> None:
    stale = replace(
        _result().observation,
        source_timestamp=CAPTURED_AT - timedelta(minutes=6),
    )
    with pytest.raises(TheRundownBridgeError):
        _bridge(result=_result(observation=stale))

    incomplete = replace(_result().observation, draw_odds=None)
    with pytest.raises((TheRundownBridgeError, ProductionContractError)):
        _bridge(result=_result(observation=incomplete))


def test_fixture_provider_and_authorization_bindings_cannot_be_inferred() -> None:
    with pytest.raises(TheRundownBridgeError):
        _bridge(
            result=_result(
                observation=replace(_result().observation, provider_identity="other")
            )
        )

    mismatched_auth = _authorization()
    mismatched_auth["provider_request_id"] = "different-request"
    with pytest.raises(TheRundownBridgeError):
        _bridge(authorization_metadata=mismatched_auth)

    with pytest.raises(TheRundownBridgeError):
        _bridge(observation_id="")


def test_evaluator_rejects_bridge_provenance_when_extended_fields_are_removed() -> None:
    evidence = _bridge()
    for field in (
        "provider_record_digest",
        "adapter_source_sha",
        "authorization_metadata",
    ):
        incomplete = {key: value for key, value in evidence.items() if key != field}
        report = evaluate_therundown_qualification(
            {
                "schema_version": incomplete["schema_version"],
                "provider_identity": incomplete["provider_identity"],
                "evidence": [incomplete],
            },
            maximum_odds_age_seconds=300,
        )
        league = next(item for item in report.leagues if item.league == "EPL")
        assert league.status is TheRundownQualificationStatus.PARTIAL_EVIDENCE
        assert not league.qualified_evidence_ids


@pytest.mark.parametrize("league", ["BL1", "EPL", "LL", "SA", "L1"])
def test_bridge_preserves_each_top5_league_identity(league: str) -> None:
    fixture = replace(
        FIXTURE,
        league_code=league,
        fixture_key=f"{league}|Home FC|Away FC|2026-09-18T14:00:00+00:00",
    )
    observation = replace(
        _result().observation,
        league_code=league,
        fixture_key=fixture.fixture_key,
    )
    auth = _authorization()
    auth.update(
        {
            "canonical_league": league,
            "fixture_key": fixture.fixture_key,
            "league_scope": [league],
            "fixture_scope": [fixture.fixture_key],
        }
    )
    evidence = bridge_therundown_observation(
        _result(observation=observation),
        expected_fixture=fixture,
        evidence_id=f"b1-evidence-{league}",
        observation_id=f"observation-{league}",
        provider_league_code=f"therundown-{league}",
        provider_league_identity_verified=True,
        evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
        synthetic_reconstruction=False,
        network_request_count=1,
        maximum_odds_age_seconds=300,
        quota_cost_units=1.0,
        adapter_source_sha="c" * 40,
        authorization_metadata=auth,
    )
    assert evidence["canonical_league"] == league

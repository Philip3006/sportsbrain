"""No-network tests for the APP-B1 TheRundown evidence bridge."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    bridge_therundown_observations,
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
OFFLINE_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "therundown"
    / "fetch_observations_top5.json"
)


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


def _offline_batch() -> tuple[
    list[tuple[Fixture, list[NormalizedOddsObservation], dict[str, object]]],
]:
    records = json.loads(OFFLINE_FIXTURE.read_text())
    batches = []
    for record in records:
        fixture = Fixture(
            fixture_key=record["fixture_key"],
            league_code=record["league_code"],
            home_team=record["home_team"],
            away_team=record["away_team"],
            kickoff=datetime.fromisoformat(record["kickoff_utc"]),
        )
        observations = []
        evidence_ids = {}
        observation_ids = {}
        for bookmaker in record["bookmakers"]:
            identity = bookmaker["identity"]
            observations.append(
                NormalizedOddsObservation(
                    league_code=fixture.league_code,
                    fixture_key=fixture.fixture_key,
                    provider_fixture_id=record["provider_fixture_id"],
                    home_team=fixture.home_team,
                    away_team=fixture.away_team,
                    kickoff_utc=fixture.kickoff,
                    market_type=MARKET_PREMATCH_1X2,
                    home_odds=bookmaker["odds"]["home"],
                    draw_odds=bookmaker["odds"]["draw"],
                    away_odds=bookmaker["odds"]["away"],
                    provider_identity="therundown_experimental",
                    bookmaker_identity=identity,
                    source_timestamp=datetime.fromisoformat(record["source_timestamp"]),
                    captured_at=datetime.fromisoformat(record["captured_at"]),
                    request_identity=record["request_identity"],
                    request_started_at=datetime.fromisoformat(
                        record["request_started_at"]
                    ),
                    request_completed_at=datetime.fromisoformat(
                        record["request_completed_at"]
                    ),
                    latency_ms=5000,
                    provider_priority=0,
                    fallback_depth=0,
                    quota_state_before=QuotaSnapshot(**record["quota_before"]),
                    quota_state_after=QuotaSnapshot(**record["quota_after"]),
                    rate_limit_state=QuotaSnapshot(**record["quota_after"]),
                    source_provenance=(
                        f"{record['source_provenance_prefix']};bookmaker={identity}"
                    ),
                    raw_record_digest=record["raw_record_digest"],
                    adapter_version=record["adapter_version"],
                    candidate_only=True,
                    metadata={
                        "raw_response_digest": record["raw_response_digest"],
                        "competition_identity": fixture.league_code,
                        "league_name": record["provider_league_code"],
                        "offline_fixture": True,
                    },
                    source_timing_provenance=TimingProvenance.SOURCE_TIMESTAMP,
                )
            )
            evidence_ids[identity] = bookmaker["evidence_id"]
            observation_ids[identity] = bookmaker["observation_id"]
        batches.append(
            (
                fixture,
                observations,
                {
                    "evidence_ids": evidence_ids,
                    "observation_ids": observation_ids,
                    "provider_league_code": record["provider_league_code"],
                    "authorization_metadata": {
                        "controlled_shadow_run_id": "offline-controlled-run",
                        "qualification_session_id": "offline-qualification-session",
                        "ceo_authorization_id": "offline-ceo-authorization",
                        "provider_identity": "therundown_experimental",
                        "canonical_league": fixture.league_code,
                        "fixture_key": fixture.fixture_key,
                        "provider_event_id": record["provider_fixture_id"],
                        "provider_request_id": record["request_identity"],
                        "provider_scope": ["therundown_experimental"],
                        "league_scope": [fixture.league_code],
                        "fixture_scope": [fixture.fixture_key],
                        "network_execution": False,
                        "no_bet": True,
                        "publication_enabled": False,
                        "monetary_spend_authorized": False,
                    },
                },
            )
        )
    return batches


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


def test_fetch_observations_offline_tuple_maps_all_top5_books_to_envelope() -> None:
    envelope_evidence = []
    for fixture, observations, bindings in _offline_batch():
        envelope = bridge_therundown_observations(
            observations,
            expected_fixture=fixture,
            evidence_ids=bindings["evidence_ids"],
            observation_ids=bindings["observation_ids"],
            provider_league_code=bindings["provider_league_code"],
            provider_league_identity_verified=True,
            evidence_kind=ObservationEvidenceKind.TEST_FIXTURE,
            synthetic_reconstruction=True,
            network_request_count=0,
            response_status_code=200,
            maximum_odds_age_seconds=300,
            quota_cost_units=0.0,
            adapter_source_sha="f" * 40,
            authorization_metadata=bindings["authorization_metadata"],
        )
        envelope_evidence.extend(envelope["evidence"])

    assert len(envelope_evidence) == 10
    assert {item["canonical_league"] for item in envelope_evidence} == {
        "BL1",
        "EPL",
        "LL",
        "SA",
        "L1",
    }
    assert {item["bookmaker_identity"] for item in envelope_evidence} == {
        "DraftKings",
        "FanDuel",
    }
    assert all(
        item["provider_event_id"].startswith("rd-") for item in envelope_evidence
    )
    assert all(item["raw_record_digest"] for item in envelope_evidence)
    assert all(item["normalized_record_digest"] for item in envelope_evidence)
    assert all(item["adapter_source_sha"] == "f" * 40 for item in envelope_evidence)

    report = evaluate_therundown_qualification(
        {
            "schema_version": envelope_evidence[0]["schema_version"],
            "provider_identity": envelope_evidence[0]["provider_identity"],
            "evidence": envelope_evidence,
        },
        maximum_odds_age_seconds=300,
    )
    assert report.status is TheRundownQualificationStatus.FAILED
    assert all(
        league.status is TheRundownQualificationStatus.FAILED
        for league in report.leagues
    )
    assert all(not league.qualified_evidence_ids for league in report.leagues)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: replace(item, bookmaker_identity=""),
        lambda item: replace(
            item,
            source_timestamp=item.source_timestamp - timedelta(minutes=6),
        ),
        lambda item: replace(item, home_team="Wrong Participant"),
        lambda item: replace(item, request_identity="ambiguous-request"),
        lambda item: replace(
            item,
            quota_state_after=QuotaSnapshot(
                used=999, remaining=1, rate_limit=1, rate_remaining=0
            ),
        ),
        lambda item: replace(
            item,
            rate_limit_state=QuotaSnapshot(rate_limit=1, rate_remaining=2),
        ),
    ],
    ids=[
        "missing-bookmaker",
        "stale",
        "participant",
        "request",
        "quota",
        "rate-limit",
    ],
)
def test_fetch_observations_bridge_rejects_unsafe_batch_provenance(mutation) -> None:
    fixture, observations, bindings = _offline_batch()[0]
    unsafe = [mutation(observations[0]), *observations[1:]]
    with pytest.raises(TheRundownBridgeError):
        bridge_therundown_observations(
            unsafe,
            expected_fixture=fixture,
            evidence_ids=bindings["evidence_ids"],
            observation_ids=bindings["observation_ids"],
            provider_league_code=bindings["provider_league_code"],
            provider_league_identity_verified=True,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            synthetic_reconstruction=False,
            network_request_count=1,
            response_status_code=200,
            maximum_odds_age_seconds=300,
            quota_cost_units=11.0,
            adapter_source_sha="f" * 40,
            authorization_metadata={
                **bindings["authorization_metadata"],
                "network_execution": True,
            },
        )


def test_fetch_observations_bridge_requires_explicit_authorization_and_request_count() -> (
    None
):
    fixture, observations, bindings = _offline_batch()[0]
    with pytest.raises(TheRundownBridgeError):
        bridge_therundown_observations(
            observations,
            expected_fixture=fixture,
            evidence_ids=bindings["evidence_ids"],
            observation_ids=bindings["observation_ids"],
            provider_league_code=bindings["provider_league_code"],
            provider_league_identity_verified=True,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            synthetic_reconstruction=False,
            network_request_count=2,
            response_status_code=200,
            maximum_odds_age_seconds=300,
            quota_cost_units=11.0,
            adapter_source_sha="f" * 40,
            authorization_metadata=bindings["authorization_metadata"],
        )

    incomplete_auth = dict(bindings["authorization_metadata"])
    incomplete_auth.pop("ceo_authorization_id")
    with pytest.raises(TheRundownBridgeError):
        bridge_therundown_observations(
            observations,
            expected_fixture=fixture,
            evidence_ids=bindings["evidence_ids"],
            observation_ids=bindings["observation_ids"],
            provider_league_code=bindings["provider_league_code"],
            provider_league_identity_verified=True,
            evidence_kind=ObservationEvidenceKind.TEST_FIXTURE,
            synthetic_reconstruction=True,
            network_request_count=0,
            response_status_code=200,
            maximum_odds_age_seconds=300,
            quota_cost_units=0.0,
            adapter_source_sha="f" * 40,
            authorization_metadata=incomplete_auth,
        )


def test_fetch_observations_bridge_binds_provider_league_identity() -> None:
    fixture, observations, bindings = _offline_batch()[0]
    with pytest.raises(TheRundownBridgeError):
        bridge_therundown_observations(
            observations,
            expected_fixture=fixture,
            evidence_ids=bindings["evidence_ids"],
            observation_ids=bindings["observation_ids"],
            provider_league_code="WRONG_PROVIDER_LEAGUE",
            provider_league_identity_verified=True,
            evidence_kind=ObservationEvidenceKind.TEST_FIXTURE,
            synthetic_reconstruction=True,
            network_request_count=0,
            response_status_code=200,
            maximum_odds_age_seconds=300,
            quota_cost_units=0.0,
            adapter_source_sha="f" * 40,
            authorization_metadata=bindings["authorization_metadata"],
        )

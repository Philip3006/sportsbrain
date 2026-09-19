"""Offline tests for the APP-B1 qualification-to-receipt seam."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.football.production_contracts import Fixture
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    NormalizedOddsObservation,
    QuotaSnapshot,
    TimingProvenance,
)
from src.football.top5_builder2_qualification_receipt import RECEIPT_SCHEMA_VERSION
from src.football.top5_controlled_shadow_provider_qualification import (
    CAPTURE_ATTESTATION_CONTRACT_VERSION,
    ObservationEvidenceKind,
)
from src.football.top5_therundown_qualification import (
    TheRundownQualificationStatus,
    evaluate_therundown_qualification,
)
from src.football.top5_therundown_qualification_bridge import (
    bridge_therundown_observations,
)
from src.football.top5_therundown_qualification_receipt_preflight import (
    QualificationReceiptPreflightError,
    build_builder2_qualification_receipt_preflight,
)

FIXTURE_DATA = (
    Path(__file__).parents[1]
    / "fixtures"
    / "therundown"
    / "fetch_observations_top5.json"
)


def _inputs(
    *,
    evidence_kind: ObservationEvidenceKind = ObservationEvidenceKind.REAL_OBSERVED,
    synthetic_reconstruction: bool = False,
    network_request_count: int = 1,
) -> tuple[dict[str, object], object, dict[str, dict[str, object]]]:
    records = json.loads(FIXTURE_DATA.read_text())
    evidence: list[dict[str, object]] = []
    for record in records:
        fixture = Fixture(
            fixture_key=record["fixture_key"],
            league_code=record["league_code"],
            home_team=record["home_team"],
            away_team=record["away_team"],
            kickoff=datetime.fromisoformat(record["kickoff_utc"]),
        )
        observations = [
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
                bookmaker_identity=bookmaker["identity"],
                source_timestamp=datetime.fromisoformat(record["source_timestamp"]),
                captured_at=datetime.fromisoformat(record["captured_at"]),
                request_identity=record["request_identity"],
                request_started_at=datetime.fromisoformat(record["request_started_at"]),
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
                    f"{record['source_provenance_prefix']};bookmaker={bookmaker['identity']}"
                ),
                raw_record_digest=record["raw_record_digest"],
                adapter_version=record["adapter_version"],
                candidate_only=True,
                metadata={
                    "raw_response_digest": record["raw_response_digest"],
                    "competition_identity": fixture.league_code,
                    "league_name": record["provider_league_code"],
                },
                source_timing_provenance=TimingProvenance.SOURCE_TIMESTAMP,
            )
            for bookmaker in record["bookmakers"]
        ]
        evidence_ids = {
            item.bookmaker_identity: bookmaker["evidence_id"]
            for item, bookmaker in zip(observations, record["bookmakers"], strict=True)
        }
        observation_ids = {
            item.bookmaker_identity: bookmaker["observation_id"]
            for item, bookmaker in zip(observations, record["bookmakers"], strict=True)
        }
        authorization = {
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
            "network_execution": network_request_count == 1,
            "no_bet": True,
            "publication_enabled": False,
            "monetary_spend_authorized": False,
        }
        batch = bridge_therundown_observations(
            observations,
            expected_fixture=fixture,
            evidence_ids=evidence_ids,
            observation_ids=observation_ids,
            provider_league_code=record["provider_league_code"],
            provider_league_identity_verified=True,
            evidence_kind=evidence_kind,
            synthetic_reconstruction=synthetic_reconstruction,
            network_request_count=network_request_count,
            response_status_code=200,
            maximum_odds_age_seconds=300,
            quota_cost_units=11.0,
            adapter_source_sha="f" * 40,
            authorization_metadata=authorization,
        )
        evidence.extend(batch["evidence"])

    envelope = {
        "schema_version": evidence[0]["schema_version"],
        "provider_identity": evidence[0]["provider_identity"],
        "evidence": evidence,
    }
    report = evaluate_therundown_qualification(
        envelope,
        maximum_odds_age_seconds=300,
    )
    bindings: dict[str, dict[str, object]] = {}
    for item in evidence:
        bindings[item["evidence_id"]] = {
            "observation_digest": "b" * 64,
            "cascade_evidence_digest": "c" * 64,
            "capture_attestation": {
                "schema_version": CAPTURE_ATTESTATION_CONTRACT_VERSION,
                "controlled_shadow_run_id": "offline-controlled-run",
                "ceo_authorization_id": "offline-ceo-authorization",
                "qualification_session_id": "offline-qualification-session",
                "provider_identity": item["provider_identity"],
                "fixture_key": item["fixture_key"],
                "provider_event_id": item["provider_event_id"],
                "provider_request_id": item["provider_request_id"],
                "adapter_version": item["adapter_version"],
                "adapter_source_sha": item["adapter_source_sha"],
                "cascade_evidence_digest": "c" * 64,
                "raw_response_digest": item["raw_record_digest"],
                "normalized_record_digest": item["normalized_record_digest"],
                "captured_at": item["captured_at"],
                "network_execution": True,
                "no_bet": True,
                "publication": False,
                "monetary_spend_authorized": False,
            },
        }
    return envelope, report, bindings


def test_complete_five_league_multi_bookmaker_projection_is_deterministic() -> None:
    envelope, report, bindings = _inputs()

    first = build_builder2_qualification_receipt_preflight(
        report, envelope, shadow_bindings=bindings
    )
    second = build_builder2_qualification_receipt_preflight(
        report, envelope, shadow_bindings=bindings
    )

    assert report.status is TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
    assert len(first) == 10
    assert first == second
    assert {item.source_evidence["evidence"]["canonical_league"] for item in first} == {
        "BL1",
        "EPL",
        "LL",
        "SA",
        "L1",
    }
    assert {item.source_evidence["bookmaker_identity"] for item in first} == {
        "DraftKings",
        "FanDuel",
    }
    for item in first:
        payload = item.as_payload()
        assert payload["schema_version"] == RECEIPT_SCHEMA_VERSION
        assert payload["receipt_eligible"] is True
        assert payload["issuer_present"] is False
        assert "qualification_receipt_id" not in payload["receipt_input"]
        assert "receipt_digest" not in payload["receipt_input"]
        assert payload["authority_changed"] is False
        assert payload["no_bet"] is True
        assert (
            payload["receipt_input"]["provider_identity"] == "therundown_experimental"
        )


def test_qualification_success_alone_and_shadow_success_alone_never_project() -> None:
    envelope, report, bindings = _inputs()

    with pytest.raises(QualificationReceiptPreflightError):
        build_builder2_qualification_receipt_preflight(
            report, envelope, shadow_bindings={}
        )
    missing_binding = dict(bindings)
    missing_binding.pop(next(iter(missing_binding)))
    with pytest.raises(QualificationReceiptPreflightError):
        build_builder2_qualification_receipt_preflight(
            report, envelope, shadow_bindings=missing_binding
        )


def test_test_fixture_cannot_be_upgraded_to_real_observed() -> None:
    envelope, report, bindings = _inputs(
        evidence_kind=ObservationEvidenceKind.TEST_FIXTURE,
        synthetic_reconstruction=True,
        network_request_count=0,
    )

    assert report.status is not TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
    with pytest.raises(QualificationReceiptPreflightError):
        build_builder2_qualification_receipt_preflight(
            report, envelope, shadow_bindings=bindings
        )


@pytest.mark.parametrize(
    "mutation",
    [
        ("missing_league", lambda item: item.update({"canonical_league": "EPL"})),
        (
            "stale_odds",
            lambda item: item.update({"source_timestamp": "2026-09-18T12:00:00+00:00"}),
        ),
        ("missing_bookmaker", lambda item: item.update({"bookmaker_identity": ""})),
        ("incomplete_1x2", lambda item: item["odds"].update({"draw": None})),
        (
            "participant_mismatch",
            lambda item: item.update({"home_team": item["away_team"]}),
        ),
        ("ambiguous_request", lambda item: item.update({"provider_request_id": ""})),
        ("missing_digest", lambda item: item.update({"normalized_record_digest": ""})),
        (
            "quota_inconsistency",
            lambda item: item["quota_state_after"].update({"used": 999}),
        ),
        (
            "unsafe_request_count",
            lambda item: item.update({"network_request_count": 0}),
        ),
    ],
    ids=lambda value: value[0],
)
def test_malformed_qualification_evidence_fails_closed(mutation) -> None:
    envelope, _report, bindings = _inputs()
    name, mutate = mutation
    del name
    mutate(envelope["evidence"][0])
    report = evaluate_therundown_qualification(envelope, maximum_odds_age_seconds=300)

    with pytest.raises(QualificationReceiptPreflightError):
        build_builder2_qualification_receipt_preflight(
            report, envelope, shadow_bindings=bindings
        )


@pytest.mark.parametrize(
    "mutation",
    [
        ("run", lambda att: att.update({"controlled_shadow_run_id": "other-run"})),
        (
            "authorization",
            lambda att: att.update({"ceo_authorization_id": "other-ceo"}),
        ),
        (
            "capture",
            lambda att: att.update({"captured_at": "2026-09-18T13:00:00+00:00"}),
        ),
        ("provider", lambda att: att.update({"provider_identity": "therundown"})),
    ],
    ids=lambda value: value[0],
)
def test_shadow_binding_mismatch_fails_closed(mutation) -> None:
    envelope, report, bindings = _inputs()
    name, mutate = mutation
    del name
    first = next(iter(bindings.values()))
    mutate(first["capture_attestation"])

    with pytest.raises(QualificationReceiptPreflightError):
        build_builder2_qualification_receipt_preflight(
            report, envelope, shadow_bindings=bindings
        )


def test_preflight_does_not_register_provider_or_call_receipt_issuer() -> None:
    envelope, report, bindings = _inputs()
    result = build_builder2_qualification_receipt_preflight(
        report, envelope, shadow_bindings=bindings
    )

    assert all(item.receipt_eligible for item in result)
    assert all(item.as_payload()["issuer_present"] is False for item in result)
    assert all(
        item.receipt_input["provider_identity"] == "therundown_experimental"
        for item in result
    )

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from src.football.top5_b4_provider_neutral_evidence import (
    AUTHORIZATION_PROVENANCE_SCHEMA_VERSION,
    SUPPORTED_EVIDENCE_PROVIDERS,
    TOP5_LEAGUE_ORDER,
    B4ControlledShadowCaptureV1,
    B4ControlledShadowEvidenceV1,
    B4FixtureDiscoveryEvidenceV1,
    B4MarketEvidenceV1,
    B4ProviderOperationEvidenceV1,
    B4ProviderReadinessV1,
    B4ProviderUsageEvidenceV1,
    ProviderNeutralB4EvidenceError,
    Top5B4ProviderNeutralEvidenceDossierV1,
    canonical_evidence_digest,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
SOURCE_SHA = "1" * 40
CONFIG_DIGEST = "2" * 64
PROVIDER = "isports_api"


def _sha(label: str) -> str:
    return sha256(label.encode()).hexdigest()


def _usage(**values: object) -> B4ProviderUsageEvidenceV1:
    return B4ProviderUsageEvidenceV1(**values)


def _operation(
    *,
    operation_id: str,
    kind: str,
    ordinal: int,
    request_identity: str,
    requested_at: datetime,
    completed_at: datetime,
    response_digest: str | None = None,
    provider: str = PROVIDER,
    retry_count: int = 0,
    request_count: int = 1,
) -> B4ProviderOperationEvidenceV1:
    return B4ProviderOperationEvidenceV1(
        provider_identity=provider,
        operation_id=operation_id,
        request_identity=request_identity,
        operation_kind=kind,
        endpoint_path=(
            "/v1/competitions/schedule"
            if kind in {"competition_schedule", "fixture_discovery"}
            else "/v1/odds/bulk"
        ),
        request_ordinal=ordinal,
        request_count=request_count,
        retry_count=retry_count,
        http_status=200,
        requested_at=requested_at,
        completed_at=completed_at,
        response_digest=response_digest or _sha(f"response:{operation_id}"),
        usage=_usage(),
    )


def _evidence(*, provider: str = PROVIDER) -> B4ControlledShadowEvidenceV1:
    schedule = _operation(
        operation_id="schedule-op",
        kind="competition_schedule",
        ordinal=1,
        request_identity="request-schedule",
        requested_at=NOW - timedelta(seconds=150),
        completed_at=NOW - timedelta(seconds=140),
        provider=provider,
    )
    bulk_odds = _operation(
        operation_id="bulk-odds-op",
        kind="bulk_odds",
        ordinal=2,
        request_identity="request-bulk-odds",
        requested_at=NOW - timedelta(seconds=60),
        completed_at=NOW - timedelta(seconds=50),
        response_digest=_sha("bulk-odds-response"),
        provider=provider,
    )
    discovery: list[B4FixtureDiscoveryEvidenceV1] = []
    markets: list[B4MarketEvidenceV1] = []
    captures: list[B4ControlledShadowCaptureV1] = []
    for index, league in enumerate(TOP5_LEAGUE_ORDER):
        home = f"Home {league}"
        away = f"Away {league}"
        kickoff = NOW + timedelta(days=index + 2)
        fixture_key = make_fixture_key(league, home, away, kickoff)
        competition_id = f"competition-{league}"
        match_id = f"matchId-{league}"
        discovery_item = B4FixtureDiscoveryEvidenceV1(
            provider_identity=provider,
            league=league,
            provider_competition_id=competition_id,
            provider_fixture_id=match_id,
            fixture_key=fixture_key,
            home_team=home,
            away_team=away,
            kickoff=kickoff,
            discovered_at=NOW - timedelta(seconds=135),
            operation_id=schedule.operation_id,
            complete=True,
        )
        market = B4MarketEvidenceV1(
            provider_identity=provider,
            league=league,
            provider_fixture_id=match_id,
            request_identity=bulk_odds.request_identity,
            operation_id=bulk_odds.operation_id,
            source_identity=f"bookmaker-{league}",
            source_provenance=f"isports_api:bulk_odds:{league}",
            market_type="PRE_MATCH_1X2_REGULATION",
            home_odds=2.1,
            draw_odds=3.2,
            away_odds=3.7,
            odds_timestamp=NOW - timedelta(seconds=45),
            captured_at=NOW - timedelta(seconds=40),
            raw_response_digest=bulk_odds.response_digest,
            provider_record_digest=_sha(f"provider-record:{league}"),
            normalized_observation_digest=_sha(f"normalized:{league}"),
        )
        capture = B4ControlledShadowCaptureV1(
            provider_identity=provider,
            league=league,
            fixture_key=fixture_key,
            provider_competition_id=competition_id,
            provider_fixture_id=match_id,
            provider_request_id=bulk_odds.request_identity,
            adapter_version="isports-adapter-v1",
            adapter_source_sha="3" * 40,
            discovery_evidence_digest=discovery_item.evidence_digest,
            market_evidence_digest=market.evidence_digest,
            operation_evidence_ids=(schedule.operation_id, bulk_odds.operation_id),
            source_timestamp=market.odds_timestamp,
            captured_at=NOW - timedelta(seconds=30 - index),
            raw_response_digest=market.raw_response_digest,
            provider_record_digest=market.provider_record_digest,
            normalized_observation_digest=market.normalized_observation_digest,
            cascade_evidence_digest=_sha(f"cascade:{league}"),
            capture_attestation_digest=_sha(f"attestation:{league}"),
        )
        discovery.append(discovery_item)
        markets.append(market)
        captures.append(capture)

    auth_body = {
        "schema_version": AUTHORIZATION_PROVENANCE_SCHEMA_VERSION,
        "provider_identity": provider,
        "controlled_shadow_run_id": "run-2026-09-27-a",
        "qualification_session_id": "session-2026-09-27-a",
        "authorization_id": "ceo-auth-2026-09-27-a",
        "league_scope": list(TOP5_LEAGUE_ORDER),
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "betting": False,
        "monetary_spend_authorized": False,
    }
    authorization = {
        **auth_body,
        "authorization_digest": canonical_evidence_digest(auth_body),
    }
    readiness = B4ProviderReadinessV1(
        provider_identity=provider,
        maximum_request_count=3,
        requests_consumed_before_run=0,
        authorized_run_request_count=3,
        run_request_count=2,
        retry_count=0,
        quota_required_by_provider_policy=False,
        usage_observed_at=NOW - timedelta(seconds=160),
        usage=_usage(),
    )
    return B4ControlledShadowEvidenceV1(
        provider_identity=provider,
        controlled_shadow_run_id="run-2026-09-27-a",
        qualification_session_id="session-2026-09-27-a",
        authorization_id="ceo-auth-2026-09-27-a",
        authorization_digest=authorization["authorization_digest"],
        source_main_sha=SOURCE_SHA,
        configuration_digest=CONFIG_DIGEST,
        adapter_version="isports-adapter-v1",
        adapter_source_sha="3" * 40,
        authorization_provenance=authorization,
        operations=(schedule, bulk_odds),
        discovery_evidence=tuple(discovery),
        market_evidence=tuple(markets),
        readiness=readiness,
        captures=tuple(captures),
    )


def test_isports_operation_and_usage_do_not_require_datapoint_accounting():
    evidence = _evidence()
    evidence.validate(now=NOW)
    operation_payload = evidence.operations[1].as_payload()
    assert operation_payload["provider_identity"] == "isports_api"
    assert operation_payload["operation_kind"] == "bulk_odds"
    assert operation_payload["request_count"] == 1
    assert operation_payload["retry_count"] == 0
    assert operation_payload["usage"]["quota_status"] == "not_exposed"
    assert "datapoints" not in operation_payload
    assert "remaining_datapoints" not in operation_payload["usage"]


def test_isports_fixture_discovery_supports_competition_id_and_match_id():
    evidence = _evidence()
    item = evidence.discovery_evidence[2]
    payload = item.as_payload(now=NOW)
    assert payload["provider_competition_id"] == "competition-LL"
    assert payload["provider_fixture_id"] == "matchId-LL"
    assert B4FixtureDiscoveryEvidenceV1.from_payload(payload, now=NOW) == item


def test_missing_provider_fixture_or_competition_identity_fails_closed():
    evidence = _evidence()
    with pytest.raises(ProviderNeutralB4EvidenceError, match="provider_fixture_id"):
        replace(evidence.discovery_evidence[0], provider_fixture_id="").validate(
            now=NOW
        )
    with pytest.raises(ProviderNeutralB4EvidenceError, match="provider_competition_id"):
        replace(evidence.discovery_evidence[0], provider_competition_id=" ").validate(
            now=NOW
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("request_count", 2), ("retry_count", 1)),
)
def test_operation_request_and_retry_bounds_fail_closed(field: str, value: int):
    evidence = _evidence()
    operation = replace(evidence.operations[0], **{field: value})
    with pytest.raises(ProviderNeutralB4EvidenceError):
        operation.validate()


def test_stale_odds_and_stale_discovery_fail_closed():
    evidence = _evidence()
    stale_market = replace(
        evidence.market_evidence[0], odds_timestamp=NOW - timedelta(seconds=301)
    )
    with pytest.raises(ProviderNeutralB4EvidenceError, match="stale"):
        stale_market.validate(now=NOW)
    stale_discovery = replace(
        evidence.discovery_evidence[0], discovered_at=NOW - timedelta(seconds=901)
    )
    with pytest.raises(ProviderNeutralB4EvidenceError, match="stale"):
        stale_discovery.validate(now=NOW)


def test_incomplete_market_missing_league_and_credential_bearing_provenance_fail():
    evidence = _evidence()
    incomplete = replace(evidence.market_evidence[0], draw_odds=None)
    with pytest.raises(ProviderNeutralB4EvidenceError, match="draw_odds"):
        incomplete.validate(now=NOW)
    with pytest.raises(ProviderNeutralB4EvidenceError, match="credentials"):
        replace(
            evidence.market_evidence[0],
            source_provenance="https://provider.invalid/odds?api_key=secret",
        ).validate(now=NOW)
    with pytest.raises(ProviderNeutralB4EvidenceError, match="five leagues"):
        replace(evidence, discovery_evidence=evidence.discovery_evidence[:-1]).validate(
            now=NOW
        )


def test_provider_mismatch_and_unknown_provider_fail_closed():
    evidence = _evidence()
    bad = replace(
        evidence,
        market_evidence=(
            replace(evidence.market_evidence[0], provider_identity="the_odds_api"),
            *evidence.market_evidence[1:],
        ),
    )
    with pytest.raises(ProviderNeutralB4EvidenceError, match="provider"):
        bad.validate(now=NOW)
    with pytest.raises(ProviderNeutralB4EvidenceError, match="not supported"):
        _operation(
            operation_id="unknown",
            kind="bulk_odds",
            ordinal=1,
            request_identity="request-unknown",
            requested_at=NOW,
            completed_at=NOW,
            provider="mystery_provider",
        ).validate()
    assert SUPPORTED_EVIDENCE_PROVIDERS == {"isports_api", "the_odds_api"}


def test_available_quota_must_truthfully_cover_authorized_run():
    evidence = _evidence()
    insufficient = B4ProviderReadinessV1(
        **{
            **evidence.readiness.__dict__,
            "quota_required_by_provider_policy": True,
            "usage": _usage(
                quota_status="available",
                quota_remaining_requests=2,
                quota_limit_requests=20,
            ),
        }
    )
    with pytest.raises(ProviderNeutralB4EvidenceError, match="headroom"):
        insufficient.validate(now=NOW, operation_request_count=2)
    no_quota_but_explicitly_optional = replace(
        evidence.readiness, quota_required_by_provider_policy=False
    )
    no_quota_but_explicitly_optional.validate(now=NOW, operation_request_count=2)


def test_quota_required_by_policy_fails_when_provider_does_not_expose_it():
    evidence = _evidence()
    required = replace(evidence.readiness, quota_required_by_provider_policy=True)
    with pytest.raises(
        ProviderNeutralB4EvidenceError, match="required but unavailable"
    ):
        required.validate(now=NOW, operation_request_count=2)


def test_dossier_digest_roundtrip_and_b1_logical_handoff_are_deterministic():
    evidence = _evidence()
    first = Top5B4ProviderNeutralEvidenceDossierV1.build(evidence, now=NOW)
    second = Top5B4ProviderNeutralEvidenceDossierV1.build(evidence, now=NOW)
    assert first.dossier_digest == second.dossier_digest
    payload = first.as_payload(now=NOW)
    assert (
        Top5B4ProviderNeutralEvidenceDossierV1.from_payload(payload, now=NOW) == first
    )

    handoff = first.b1_evidence_inputs(now=NOW)
    # Same logical inputs as the merged composer; Builder 1's separate
    # consumer PR will update the provider-specific validators.
    from src.football.top5_final_acceptance_composer import _B4_INPUT_KEYS

    assert set(handoff) == set(_B4_INPUT_KEYS)
    assert (
        handoff["b4_quota_proof_package"]["schema_version"]
        == "top5-b4-provider-readiness-v1"
    )
    assert (
        handoff["b4_quota_headroom"]["schema_version"]
        == "top5-b4-operation-headroom-v1"
    )
    assert (
        handoff["provider_native_discovery_provenance"]["schema_version"]
        == "top5-b4-provider-operation-provenance-v1"
    )
    assert handoff["controlled_shadow"]["provider_identity"] == "isports_api"
    assert "remaining_datapoints" not in handoff["b4_quota_proof_package"]


def test_tampered_serialized_operation_or_dossier_is_rejected():
    evidence = _evidence()
    operation_payload = evidence.operations[0].as_payload()
    operation_payload["provider_identity"] = "the_odds_api"
    with pytest.raises(ProviderNeutralB4EvidenceError, match="digest"):
        B4ProviderOperationEvidenceV1.from_payload(operation_payload)

    dossier = Top5B4ProviderNeutralEvidenceDossierV1.build(evidence, now=NOW)
    payload = dossier.as_payload(now=NOW)
    payload["controlled_shadow"]["provider_identity"] = "the_odds_api"
    with pytest.raises(ProviderNeutralB4EvidenceError):
        Top5B4ProviderNeutralEvidenceDossierV1.from_payload(payload, now=NOW)


def test_run_session_and_authorization_bindings_are_exact():
    evidence = _evidence()
    bad_session = replace(evidence, qualification_session_id="other-session")
    with pytest.raises(
        ProviderNeutralB4EvidenceError, match="authorization provenance"
    ):
        bad_session.validate(now=NOW)
    bad_authorization = replace(evidence, authorization_id="other-auth")
    with pytest.raises(
        ProviderNeutralB4EvidenceError, match="authorization provenance"
    ):
        bad_authorization.validate(now=NOW)
    bad_run = replace(evidence, controlled_shadow_run_id="other-run")
    with pytest.raises(
        ProviderNeutralB4EvidenceError, match="authorization provenance"
    ):
        bad_run.validate(now=NOW)


def test_structural_qualification_never_authorizes_signal_time_or_production():
    dossier = Top5B4ProviderNeutralEvidenceDossierV1.build(_evidence(), now=NOW)
    payload = dossier.qualification.as_payload()
    assert payload["structural_provider_qualified"] is True
    assert payload["production_signal_time_approved"] is False
    assert payload["receipt_eligible"] is False
    assert payload["publication"] is False
    assert payload["production_activation"] is False
    assert payload["betting"] is False
    assert payload["provider_identity"] in {"isports_api", "the_odds_api"}
    assert dossier.reconciliation.retry_count == 0
    assert dossier.reconciliation.league_scope == TOP5_LEAGUE_ORDER


def test_existing_the_odds_api_identity_is_valid_without_changing_routing():
    evidence = _evidence(provider="the_odds_api")
    dossier = Top5B4ProviderNeutralEvidenceDossierV1.build(evidence, now=NOW)
    assert dossier.controlled_shadow.provider_identity == "the_odds_api"
    assert (
        dossier.b1_evidence_inputs(now=NOW)["controlled_shadow"]["provider_identity"]
        == "the_odds_api"
    )


def test_provider_neutral_validation_has_no_network_or_production_side_effects(
    monkeypatch,
):
    import socket
    import urllib.request

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("provider/network operation was attempted")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    dossier = Top5B4ProviderNeutralEvidenceDossierV1.build(_evidence(), now=NOW)
    handoff = dossier.b1_evidence_inputs(now=NOW)
    assert handoff["controlled_shadow"]["no_bet"] is True
    assert handoff["controlled_shadow"]["publication"] is False
    assert handoff["controlled_shadow"]["production_activation"] is False
    assert handoff["controlled_shadow"]["betting"] is False
    assert handoff["controlled_shadow"]["ledger_mutated"] is False

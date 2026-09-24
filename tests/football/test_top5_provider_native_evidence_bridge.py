from __future__ import annotations

from dataclasses import replace

import pytest

from src.football.top5_controlled_shadow_authorization_package import (
    FiveLeagueReconciliationV1,
    QualificationReadyArtifactsV1,
    _digest,
)
from src.football.top5_provider_native_evidence_bridge import (
    ProviderNativeDiscoveryProvenanceV1,
    ProviderNativeEvidenceBridgeError,
    assemble_top5_b4_evidence_dossier,
    build_provider_native_discovery_provenance,
    materialize_prebound_network_configuration_from_provider_native,
    project_provider_native_discovery_evidence,
    validate_native_provenance_against_legacy,
)
from src.football.top5_therundown_event_discovery import (
    EventDiscoveryContractError,
    EventDiscoveryExecutionBlocked,
)
from src.football.top5_therundown_network_shadow import NetworkShadowExecutionBlocked
from src.football.top5_therundown_provider_native_discovery import (
    DISCOVERY_LEAGUE_ORDER,
    discover_five_league_events_provider_native,
)
from tests.football.test_top5_therundown_provider_native_discovery import (
    NOW,
    FakeNativeTransport,
    _authorization,
    _proof,
    _response,
)


@pytest.fixture
def native_run(monkeypatch, tmp_path):
    state = tmp_path / "native-discovery-consumption.json"
    monkeypatch.setattr(
        "src.football.top5_therundown_event_discovery.discovery_authorization_consumption_state_path",
        lambda: state,
    )
    return discover_five_league_events_provider_native(
        _authorization(),
        proof=_proof(),
        api_key="injected-test-only",
        transport=FakeNativeTransport(
            [_response(league) for league in DISCOVERY_LEAGUE_ORDER]
        ),
        now=NOW,
        pacer=lambda _: None,
    )


def test_native_roundtrip_projection_and_shadow_materialization(native_run):
    provenance = build_provider_native_discovery_provenance(native_run)
    assert provenance.provider_affiliate_ids == ("19",)
    assert provenance.leagues == DISCOVERY_LEAGUE_ORDER
    assert len(provenance.provider_event_ids) == 5
    assert len(provenance.participant_ids) == 10

    roundtripped = type(native_run).from_payload(native_run.as_payload())
    assert roundtripped.as_payload() == native_run.as_payload()

    legacy = project_provider_native_discovery_evidence(roundtripped)
    assert tuple(item.league for item in legacy) == DISCOVERY_LEAGUE_ORDER
    assert all(item.network_execution is True for item in legacy)
    assert all(item.qualification_eligible is False for item in legacy)
    assert all(
        item.provider_event_evidence_digest != capture.evidence_digest
        for item, capture in zip(legacy, roundtripped.captures, strict=True)
    )
    validate_native_provenance_against_legacy(
        provenance.as_payload(), tuple(item.as_payload() for item in legacy)
    )

    configuration = materialize_prebound_network_configuration_from_provider_native(
        roundtripped
    )
    assert configuration.enabled is False
    assert configuration.provider_affiliate_ids == ("19",)
    assert configuration.maximum_request_count == 5
    assert configuration.maximum_datapoints == 275
    assert configuration.maximum_quota_cost_units == 275
    assert configuration.request_quota_cost_units == 55
    assert configuration.minimum_interval_seconds >= 1.1
    assert configuration.maximum_retries == 0
    assert configuration.no_bet is True
    assert configuration.publication is False
    assert configuration.production_activation is False
    assert configuration.monetary_spend_authorized is False
    assert (
        tuple(item.request_identity for item in configuration.request_scope)
        == provenance.request_identities
    )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload["authorization"].__setitem__(
            "request_shape_digest", "f" * 64
        ),
        lambda payload: payload["captures"][0].__setitem__(
            "provider_event_id", "tampered-event"
        ),
        lambda payload: payload["captures"][0].__setitem__(
            "home_participant_id", "tampered-participant"
        ),
        lambda payload: payload["captures"][0].__setitem__(
            "raw_response_digest", "0" * 64
        ),
        lambda payload: payload.__setitem__(
            "independent_fixture_source_qualification", "SELF_ATTESTED"
        ),
    ),
)
def test_native_roundtrip_mutations_fail_closed(native_run, mutation):
    payload = native_run.as_payload()
    mutation(payload)
    with pytest.raises((EventDiscoveryContractError, EventDiscoveryExecutionBlocked)):
        type(native_run).from_payload(payload)


def test_native_provenance_mutation_and_identity_divergence_fail_closed(native_run):
    provenance = build_provider_native_discovery_provenance(native_run)
    payload = provenance.as_payload()
    payload["provider_event_ids"][0] = "other-event"
    payload["provenance_digest"] = provenance.computed_provenance_digest
    with pytest.raises(ProviderNativeEvidenceBridgeError):
        type(provenance).from_payload(payload)

    configuration = materialize_prebound_network_configuration_from_provider_native(
        native_run
    )
    tampered = replace(configuration, provider_affiliate_ids=("22",))
    tampered = replace(
        tampered, configuration_digest=tampered.computed_configuration_digest
    )
    with pytest.raises(NetworkShadowExecutionBlocked):
        tampered.validate()


def test_b1_acceptance_accepts_bound_native_provenance_without_changing_authority():
    from tests.football.test_top5_final_acceptance import (
        NOW_ACCEPTANCE,
        _bundle,
    )

    bundle = _bundle()
    legacy = bundle["discovery_evidence"]
    first = legacy[0]
    provenance = ProviderNativeDiscoveryProvenanceV1(
        provider="therundown_experimental",
        discovery_target_source="therundown_provider_native",
        independent_fixture_source_qualification="WAIVED",
        provider_affiliate_ids=("19",),
        discovery_authorization_id=first["discovery_authorization_id"],
        discovery_authorization_digest=first["discovery_authorization_digest"],
        request_shape_digest=first["request_shape_digest"],
        native_run_digest="a" * 64,
        leagues=tuple(item["league"] for item in legacy),
        capture_evidence_digests=("d" * 64,) * 5,
        provider_event_ids=tuple(item["provider_event_id"] for item in legacy),
        participant_ids=tuple(
            participant
            for item in legacy
            for participant in (
                item["home_participant_id"],
                item["away_participant_id"],
            )
        ),
        fixture_keys=tuple(item["fixture_key"] for item in legacy),
        request_identities=tuple(item["request_identity"] for item in legacy),
        request_count=5,
        datapoint_total=275,
        billing_modes=("provider_x_datapoints",) * 5,
        billing_datapoints=(55,) * 5,
        raw_response_digests=tuple(item["raw_response_digest"] for item in legacy),
        adapter_version="therundown-v2-experimental:2",
        adapter_source_sha="e" * 40,
        provenance_digest="",
    )
    provenance = replace(
        provenance, provenance_digest=provenance.computed_provenance_digest
    )
    provenance.validate()
    bundle["provider_native_discovery_provenance"] = provenance.as_payload()

    from src.football.top5_final_acceptance import verify_final_acceptance

    result = verify_final_acceptance(bundle, now=NOW_ACCEPTANCE)
    assert result["status"] == "TOP5_FINAL_ACCEPTANCE_VERIFIED"
    assert result["manifest"]["candidate_provider"] == "therundown_experimental"
    assert result["manifest"]["provider_authority"] == "the_odds_api"


def test_typed_b4_dossier_binds_native_projection_and_reconciliation(native_run):
    configuration = materialize_prebound_network_configuration_from_provider_native(
        native_run
    )
    artifacts = QualificationReadyArtifactsV1(
        provider="therundown_experimental",
        controlled_shadow_run_id="shadow-run-native",
        qualification_session_id="qualification-native",
        authorization_id="shadow-auth-native",
        configuration_digest=configuration.configuration_digest,
        capture_attestations=({},) * 5,
        candidate_eligibilities=({},) * 5,
        qualification_inputs=({},) * 5,
        builder2_receipt_inputs=({},) * 5,
    )
    reconciliation = FiveLeagueReconciliationV1(
        provider="therundown_experimental",
        controlled_shadow_run_id=artifacts.controlled_shadow_run_id,
        qualification_session_id=artifacts.qualification_session_id,
        authorization_id=artifacts.authorization_id,
        authorization_digest=native_run.authorization.authorization_digest,
        configuration_digest=configuration.configuration_digest,
        adapter_version=configuration.adapter_version,
        adapter_source_sha=configuration.adapter_source_sha,
        leagues=DISCOVERY_LEAGUE_ORDER,
        fixture_keys=tuple(item.fixture_key for item in configuration.targets),
        provider_event_ids=tuple(
            item.provider_event_id for item in configuration.targets
        ),
        provider_request_ids=tuple(
            item.request_identity for item in configuration.request_scope
        ),
        request_count=5,
        datapoint_count=275,
        quota_cost_units=275,
        artifacts=artifacts,
        reconciliation_digest="",
    )
    reconciliation = replace(
        reconciliation,
        reconciliation_digest=_digest(reconciliation._payload_without_digest()),
    )
    dossier = assemble_top5_b4_evidence_dossier(
        source_main_sha="a" * 40,
        quota_proof=_proof(),
        native_run=native_run,
        configuration=configuration,
        reconciliation=reconciliation,
        shadow_headroom=None,
        now=NOW,
    )
    assert dossier.as_payload()["schema_version"] == "top5-b4-evidence-dossier-v1"
    assert dossier.dossier_digest == dossier.computed_dossier_digest

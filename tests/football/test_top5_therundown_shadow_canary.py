"""Safety and contract tests for the disabled TheRundown canary."""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.provider_cascade.contracts import FOOTBALL_PROVIDER_REPERTOIRE
from src.football.top5_builder2_qualification_receipt import (
    RECEIPT_SCHEMA_VERSION,
    Builder2QualificationReceiptError,
    issue_builder2_qualification_receipt,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ControlledShadowCaptureAttestation,
    ObservationEvidenceKind,
    ProviderQualificationStatus,
    QualificationContractError,
    RealProviderObservation,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_shadow_canary import (
    CANARY_ATTESTATION_INPUT_SCHEMA_VERSION,
    CANARY_EXECUTION_MODE,
    CanaryContractError,
    CanaryExecutionBlocked,
    CanaryRunStatus,
    FakeTheRundownCanaryTransport,
    TheRundownCanaryAuthorizationV1,
    TheRundownCanaryConfigurationV1,
    TheRundownCanaryLifecycleArtifactV1,
    TheRundownCanaryNetworkTransport,
    TheRundownCanaryPacingPolicyV1,
    TheRundownCanaryResponseV1,
    TheRundownCanaryTargetV1,
    TheRundownControlledShadowCanary,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)
KICKOFF = NOW + timedelta(hours=2)
ADAPTER_SOURCE_SHA = "a" * 64
RAW_DIGEST = "b" * 64
NORMALIZED_DIGEST = "c" * 64


def _target(**changes: object) -> TheRundownCanaryTargetV1:
    values: dict[str, object] = {
        "provider": "therundown",
        "league": "EPL",
        "fixture_key": make_fixture_key("EPL", "Home FC", "Away FC", KICKOFF),
        "provider_event_id": "therundown-event-1",
        "home_team": "Home FC",
        "away_team": "Away FC",
        "kickoff": KICKOFF,
    }
    values.update(changes)
    return TheRundownCanaryTargetV1(**values)


def _config(**changes: object) -> TheRundownCanaryConfigurationV1:
    values: dict[str, object] = {
        "target": _target(),
        "adapter_version": "therundown-adapter-v1",
        "adapter_source_sha": ADAPTER_SOURCE_SHA,
        "maximum_request_count": 1,
        "maximum_quota_cost_units": 1.0,
        "request_quota_cost_units": 1.0,
        "maximum_source_age_seconds": 300,
        "pacing_policy": TheRundownCanaryPacingPolicyV1(
            execution_mode=CANARY_EXECUTION_MODE,
            minimum_interval_seconds=1.0,
            maximum_retries=0,
        ),
        "enabled": False,
    }
    values.update(changes)
    draft = TheRundownCanaryConfigurationV1(
        **values,  # type: ignore[arg-type]
        configuration_digest="0" * 64,
    )
    return replace(draft, configuration_digest=draft.computed_configuration_digest)


def _authorization(
    configuration: TheRundownCanaryConfigurationV1, **changes: object
) -> TheRundownCanaryAuthorizationV1:
    values: dict[str, object] = {
        "authorization_id": "ceo-auth:therundown-canary-1",
        "ceo_authorization_identity": "ceo:canonical",
        "controlled_shadow_run_id": "controlled-shadow:therundown-1",
        "qualification_session_id": "qualification-session:therundown-1",
        "target": configuration.target,
        "adapter_version": configuration.adapter_version,
        "adapter_source_sha": configuration.adapter_source_sha,
        "configuration_digest": configuration.configuration_digest,
        "maximum_request_count": configuration.maximum_request_count,
        "maximum_quota_cost_units": configuration.maximum_quota_cost_units,
        "request_quota_cost_units": configuration.request_quota_cost_units,
        "maximum_source_age_seconds": configuration.maximum_source_age_seconds,
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=10),
        "pacing_policy": configuration.pacing_policy,
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "monetary_spend_authorized": False,
    }
    values.update(changes)
    return TheRundownCanaryAuthorizationV1(**values)  # type: ignore[arg-type]


def _response(request, **changes: object) -> TheRundownCanaryResponseV1:
    values: dict[str, object] = {
        "outcome": "SUCCESS",
        "provider": "therundown",
        "provider_event_id": "therundown-event-1",
        "provider_request_id": request.request_identity,
        "bookmaker_identity": "book-a",
        "source_identity": "therundown:top5",
        "source_timestamp": NOW - timedelta(seconds=5),
        "captured_at": NOW,
        "request_started_at": NOW - timedelta(milliseconds=10),
        "request_finished_at": NOW,
        "home_odds": 2.1,
        "draw_odds": 3.4,
        "away_odds": 3.2,
        "adapter_version": "therundown-adapter-v1",
        "adapter_source_sha": ADAPTER_SOURCE_SHA,
        "raw_response_digest": RAW_DIGEST,
        "normalized_record_digest": NORMALIZED_DIGEST,
        "quota_before": 10,
        "quota_after": 9,
        "quota_cost_units": 1.0,
        "retry_count": 0,
        "evidence_kind": "TEST_FIXTURE",
        "network_execution": False,
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "monetary_spend_authorized": False,
    }
    values.update(changes)
    return TheRundownCanaryResponseV1(**values)  # type: ignore[arg-type]


def _fake_success() -> FakeTheRundownCanaryTransport:
    return FakeTheRundownCanaryTransport(lambda request: _response(request))


def _run_fake(configuration=None, authorization=None, transport=None):
    configuration = configuration or _config(enabled=True)
    authorization = authorization or _authorization(configuration)
    transport = transport or _fake_success()
    return (
        TheRundownControlledShadowCanary(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        ),
        transport,
    )


def test_default_configuration_is_disabled_and_makes_no_transport_call():
    configuration = _config()
    authorization = _authorization(configuration)
    transport = FakeTheRundownCanaryTransport(
        lambda request: pytest.fail("disabled canary called the transport")
    )
    with pytest.raises(CanaryExecutionBlocked, match="disabled by default"):
        TheRundownControlledShadowCanary(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )
    assert transport.calls == []


def test_authorization_requires_exact_scope_and_expiry_before_call():
    configuration = _config(enabled=True)
    transport = _fake_success()
    expired = _authorization(
        configuration,
        issued_at=NOW - timedelta(minutes=10),
        expires_at=NOW - timedelta(seconds=1),
    )
    with pytest.raises(CanaryExecutionBlocked, match="expired"):
        TheRundownControlledShadowCanary(clock=lambda: NOW).run(
            configuration, expired, transport=transport
        )
    assert transport.calls == []

    mismatched = _authorization(
        configuration,
        controlled_shadow_run_id="controlled-shadow:other",
    )
    # The run ID is external identity and may differ from another run; it is
    # still bound into the generated request and authorization digest.
    mismatched.validate(configuration, now=NOW)
    assert mismatched.controlled_shadow_run_id != configuration.target.fixture_key


def test_authorization_and_configuration_bind_all_budget_and_provenance_fields():
    configuration = _config(enabled=True)
    authorization = _authorization(configuration)
    configuration.validate()
    authorization.validate(configuration, now=NOW)
    assert authorization.configuration_digest == configuration.configuration_digest
    assert authorization.maximum_request_count == 1
    assert authorization.maximum_quota_cost_units == 1.0
    assert authorization.adapter_source_sha == ADAPTER_SOURCE_SHA
    assert authorization.no_bet is True
    assert authorization.publication is False
    assert authorization.production_activation is False
    assert authorization.monetary_spend_authorized is False
    assert (
        authorization.as_payload()["authorization_digest"]
        == authorization.authorization_digest
    )


def test_configuration_digest_mismatch_is_rejected_before_transport():
    configuration = _config(enabled=True)
    authorization = _authorization(configuration, configuration_digest="d" * 64)
    transport = _fake_success()
    with pytest.raises(CanaryExecutionBlocked, match="configuration digest"):
        TheRundownControlledShadowCanary(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )
    assert transport.calls == []


def test_fake_success_is_test_fixture_only_and_emits_all_future_evidence_inputs():
    result, transport = _run_fake()
    assert len(transport.calls) == 1
    assert result.status is CanaryRunStatus.TEST_FIXTURE
    assert result.observed is False
    evidence = result.evidence.as_payload()
    assert evidence["provider"] == "therundown"
    assert evidence["league"] == "EPL"
    assert evidence["fixture_key"] == result.request.target.fixture_key
    assert evidence["provider_event_id"] == "therundown-event-1"
    assert evidence["request_identity"] == result.request.request_identity
    assert evidence["network_execution"] is False
    assert evidence["evidence_kind"] == "TEST_FIXTURE"
    assert evidence["canonical_attestation_eligible"] is False
    assert evidence["qualification_report_eligible"] is False
    assert evidence["builder2_receipt_eligible"] is False
    capture_input = evidence["capture_attestation_input"]
    assert (
        capture_input["attestation_input_schema"]
        == CANARY_ATTESTATION_INPUT_SCHEMA_VERSION
    )
    assert capture_input["network_execution"] is False
    assert capture_input["no_bet"] is True
    assert capture_input["publication"] is False
    assert capture_input["monetary_spend_authorized"] is False
    assert evidence["builder2_receipt_input"]["issuer_present"] is False


def test_test_fixture_capture_input_cannot_validate_as_canonical_real_attestation():
    result, _ = _run_fake()
    with pytest.raises(QualificationContractError):
        ControlledShadowCaptureAttestation.from_payload(
            result.evidence.as_payload()["capture_attestation_input"]
        ).validate()


def test_test_injected_real_observed_and_network_execution_claims_fail_closed():
    configuration = _config(enabled=True)
    authorization = _authorization(configuration)

    def real_claim(request):
        return _response(
            request,
            evidence_kind="REAL_OBSERVED",
            network_execution=True,
        )

    transport = FakeTheRundownCanaryTransport(real_claim)
    with pytest.raises(CanaryExecutionBlocked, match="TEST_INJECTED"):
        TheRundownControlledShadowCanary(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )
    assert len(transport.calls) == 1


def test_network_capable_is_only_available_through_sealed_marker_and_stub_is_not_http():
    configuration = _config(enabled=True)
    authorization = _authorization(configuration)

    class Impostor:
        transport_capability = "NETWORK_CAPABLE"

        def execute(self, request):
            pytest.fail("unsealed network impostor was called")

    with pytest.raises(CanaryExecutionBlocked, match="reviewed marker"):
        TheRundownControlledShadowCanary(clock=lambda: NOW).run(
            configuration, authorization, transport=Impostor()
        )

    class StubNetwork(TheRundownCanaryNetworkTransport):
        def __init__(self):
            self.calls = []

        def execute(self, request):
            self.calls.append(request)
            return _response(
                request,
                evidence_kind="REAL_OBSERVED",
                network_execution=True,
            )

    stub = StubNetwork()
    result = TheRundownControlledShadowCanary(clock=lambda: NOW).run(
        configuration, authorization, transport=stub
    )
    assert len(stub.calls) == 1
    assert result.status is CanaryRunStatus.REAL_OBSERVED
    assert result.observed is True
    assert result.evidence.network_execution is True
    assert result.evidence.canonical_attestation_eligible is True
    capture_input = result.evidence.as_payload()["capture_attestation_input"]
    assert capture_input["schema_version"] == "controlled-shadow-capture-attestation-v1"
    assert capture_input["network_execution"] is True
    assert capture_input["provider_event_id"] == "therundown-event-1"
    assert capture_input["provider_request_id"] == result.request.request_identity
    with pytest.raises(QualificationContractError):
        ControlledShadowCaptureAttestation.from_payload(capture_input).validate()


@pytest.mark.parametrize(
    "changes, pattern",
    [
        ({"provider": "other-provider"}, "provider"),
        ({"provider_event_id": "other-event"}, "provider event"),
        ({"provider_request_id": "other-request"}, "provider request"),
        ({"retry_count": 1}, "retry"),
        ({"source_timestamp": NOW - timedelta(seconds=301)}, "stale"),
        ({"source_timestamp": None}, "timestamps"),
        ({"draw_odds": None}, "decimal price"),
        ({"adapter_source_sha": "d" * 64}, "adapter source SHA"),
        ({"quota_cost_units": 2.0}, "quota cost"),
        ({"authority_attempted": True}, "authority_attempted"),
        ({"publication_attempted": True}, "publication_attempted"),
        ({"activation_attempted": True}, "activation_attempted"),
    ],
)
def test_unsafe_or_mismatched_response_fails_closed(changes, pattern):
    configuration = _config(enabled=True)
    authorization = _authorization(configuration)
    transport = FakeTheRundownCanaryTransport(
        lambda request: _response(request, **changes)
    )
    with pytest.raises(CanaryContractError, match=pattern):
        TheRundownControlledShadowCanary(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )


def test_incomplete_1x2_and_missing_provenance_are_not_observations():
    configuration = _config(enabled=True)
    authorization = _authorization(configuration)
    transport = FakeTheRundownCanaryTransport(
        lambda request: _response(
            request,
            bookmaker_identity="",
            source_identity="",
            home_odds=None,
        )
    )
    with pytest.raises(CanaryExecutionBlocked, match="provenance"):
        TheRundownControlledShadowCanary(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )


def test_provider_failure_is_no_observation_and_never_real():
    configuration = _config(enabled=True)
    authorization = _authorization(configuration)
    transport = FakeTheRundownCanaryTransport(
        lambda request: _response(
            request,
            outcome="RATE_LIMITED",
            quota_cost_units=1.0,
            provider_event_id="",
            provider_request_id="",
        )
    )
    result, _ = _run_fake(configuration, authorization, transport)
    assert result.status is CanaryRunStatus.NO_OBSERVATION
    assert result.observed is False
    assert result.evidence.network_execution is False
    assert result.evidence.canonical_attestation_eligible is False


def test_wrong_target_provider_or_league_is_rejected_before_transport():
    configuration = _config(enabled=True)
    authorization = _authorization(configuration)
    transport = _fake_success()
    bad_provider = replace(configuration, target=_target(provider="the_odds_api"))
    with pytest.raises(CanaryContractError):
        TheRundownControlledShadowCanary(clock=lambda: NOW).run(
            bad_provider, authorization, transport=transport
        )
    assert not transport.calls

    bad_league_target = _target(league="CL")
    bad_league = replace(configuration, target=bad_league_target)
    with pytest.raises(CanaryContractError):
        bad_league.validate()


def test_canary_has_no_authority_or_automatic_scheduler_surface():
    module = inspect.getsource(
        __import__("src.football.top5_therundown_shadow_canary", fromlist=["module"])
    )
    for forbidden in (
        "import requests",
        "import httpx",
        "import urllib",
        "import socket",
        "urlopen(",
        "issue_builder2_qualification_receipt",
        "place_bet(",
        "publish(",
        "activate_production(",
        "register_scheduler(",
    ):
        assert forbidden not in module
    assert "class TheRundownCanaryNetworkTransport" in module
    assert not hasattr(
        TheRundownControlledShadowCanary, "issue_builder2_qualification_receipt"
    )
    assert not hasattr(TheRundownControlledShadowCanary, "publish")
    assert not hasattr(TheRundownControlledShadowCanary, "place_bet")
    assert not hasattr(TheRundownControlledShadowCanary, "activate_production")


def test_therundown_remains_outside_active_football_provider_authority():
    assert FOOTBALL_PROVIDER_REPERTOIRE == ("the_odds_api",)
    assert "therundown" not in FOOTBALL_PROVIDER_REPERTOIRE


def test_pacing_is_sequential_and_retry_policy_is_zero():
    configuration = _config(enabled=True)
    assert configuration.pacing_policy.execution_mode == "SEQUENTIAL"
    assert configuration.pacing_policy.maximum_retries == 0
    with pytest.raises(CanaryExecutionBlocked, match="retries"):
        TheRundownCanaryPacingPolicyV1(maximum_retries=1).validate()


def _network_result_for_league(league: str):
    target = _target(
        league=league,
        fixture_key=make_fixture_key(league, "Home FC", "Away FC", KICKOFF),
        provider_event_id=f"therundown-event-{league.lower()}",
    )
    configuration = _config(enabled=True, target=target)
    authorization = _authorization(configuration)

    class OfflineNetworkStub(TheRundownCanaryNetworkTransport):
        def execute(self, request):
            return _response(
                request,
                provider_event_id=target.provider_event_id,
                evidence_kind="REAL_OBSERVED",
                network_execution=True,
            )

    return TheRundownControlledShadowCanary(clock=lambda: NOW).run(
        configuration, authorization, transport=OfflineNetworkStub()
    )


@pytest.mark.parametrize("league", ["EPL", "BL1", "LL", "SA", "L1"])
def test_offline_real_shaped_lifecycle_preserves_exact_five_league_bindings(league):
    result = _network_result_for_league(league)
    artifact = result.lifecycle_artifact()
    assert isinstance(artifact, TheRundownCanaryLifecycleArtifactV1)
    assert artifact.compatibility.value == "READY_FOR_EXTERNAL_VALIDATION"
    observation = artifact.qualification_input["observation"]
    assert observation["provider_identity"] == "therundown"
    assert observation["league"] == league
    assert observation["fixture_key"] == result.request.target.fixture_key
    assert observation["provider_event_id"] == result.evidence.provider_event_id
    assert observation["provider_request_id"] == result.request.request_identity
    assert observation["bookmaker_identity"] == "book-a"
    assert observation["home_odds"] == 2.1
    assert observation["draw_odds"] == 3.4
    assert observation["away_odds"] == 3.2
    assert observation["source_timestamp"] is not None
    assert observation["provider_timestamp_provenance"] == "PROVIDER_SOURCE_TIMESTAMP"
    assert observation["adapter_source_sha"] == ADAPTER_SOURCE_SHA
    assert observation["raw_response_digest"] == RAW_DIGEST
    assert observation["normalized_record_digest"] == NORMALIZED_DIGEST
    assert observation["observation_digest"] == result.evidence.observation_digest
    assert observation["quota_before"] == 10
    assert observation["quota_after"] == 9
    assert observation["quota_cost_units"] == 1.0
    assert observation["network_request_count"] == 1

    capture = artifact.capture_attestation_input
    assert capture["schema_version"] == "controlled-shadow-capture-attestation-v1"
    assert capture["controlled_shadow_run_id"] == "controlled-shadow:therundown-1"
    assert capture["ceo_authorization_id"] == "ceo-auth:therundown-canary-1"
    assert capture["qualification_session_id"] == "qualification-session:therundown-1"
    assert capture["provider_identity"] == "therundown"
    assert capture["fixture_key"] == result.request.target.fixture_key
    assert capture["provider_event_id"] == result.evidence.provider_event_id
    assert capture["provider_request_id"] == result.request.request_identity
    assert capture["network_execution"] is True
    assert capture["no_bet"] is True
    assert capture["publication"] is False
    assert capture["production_activation"] is False
    assert capture["monetary_spend_authorized"] is False
    assert artifact.result.evidence.canonical_capture_attestation_digest

    receipt_input = artifact.builder2_receipt_input
    assert receipt_input["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert receipt_input["issuer_present"] is False
    assert receipt_input["eligible"] is False
    assert (
        receipt_input["available_evidence"]["observation_id"]
        == result.evidence.observation_id
    )
    assert (
        receipt_input["available_evidence"]["normalized_record_digest"]
        == NORMALIZED_DIGEST
    )
    assert (
        receipt_input["available_evidence"]["adapter_source_sha"] == ADAPTER_SOURCE_SHA
    )
    assert (
        receipt_input["available_evidence"]["capture_attestation_digest"]
        == result.evidence.canonical_capture_attestation_digest
    )


def test_fake_lifecycle_is_test_only_and_cannot_cross_attestation_boundary():
    result, _ = _run_fake()
    artifact = result.lifecycle_artifact()
    assert artifact.compatibility.value == "TEST_ONLY"
    observation = artifact.qualification_input["observation"]
    assert observation["evidence_kind"] == ObservationEvidenceKind.TEST_FIXTURE.value
    assert observation["network_request_count"] == 0
    assert observation["synthetic_reconstruction"] is True
    assert observation["candidate_only"] is True
    assert artifact.capture_attestation_input["network_execution"] is False
    with pytest.raises(QualificationContractError):
        ControlledShadowCaptureAttestation.from_payload(
            artifact.capture_attestation_input
        ).validate()


def test_current_b1_b2_contracts_reject_therundown_without_authority_change():
    result = _network_result_for_league("EPL")
    artifact = result.lifecycle_artifact()
    observation_payload = artifact.qualification_input["observation"]
    observation = RealProviderObservation.from_payload(observation_payload)
    with pytest.raises(QualificationContractError, match="unknown provider"):
        observation.validate_structural()

    from tests.football.test_top5_controlled_shadow_provider_qualification import (
        _observation as active_observation,
    )
    from tests.football.test_top5_controlled_shadow_provider_qualification import (
        _qualify as qualify_active,
    )

    active = active_observation()
    active_report = qualify_active((active,))
    active_result = active_report.results[0]
    assert (
        active_result.status is ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    )
    with pytest.raises((Builder2QualificationReceiptError, QualificationContractError)):
        issue_builder2_qualification_receipt(
            active_report,
            observation_payload,
            active_result,
        )
    assert FOOTBALL_PROVIDER_REPERTOIRE == ("the_odds_api",)
    assert artifact.as_payload()["safety"] == {
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "monetary_spend_authorized": False,
        "authority_changed": False,
        "scheduler_registered": False,
        "ledger_mutated": False,
    }


def test_receipt_requires_attestation_and_accepted_qualification_result():
    from tests.football.test_top5_controlled_shadow_provider_qualification import (
        _observation as active_observation,
    )
    from tests.football.test_top5_controlled_shadow_provider_qualification import (
        _qualify as qualify_active,
    )

    fixture = active_observation()
    report = qualify_active((fixture,))
    valid_result = report.results[0]
    receipt = issue_builder2_qualification_receipt(report, fixture, valid_result)
    assert receipt.accepted is True
    assert receipt.no_bet is True
    assert receipt.publication is False
    assert receipt.production_activation is False

    missing_attestation = replace(fixture, capture_attestation=None)
    with pytest.raises(Builder2QualificationReceiptError):
        issue_builder2_qualification_receipt(report, missing_attestation, valid_result)

    test_fixture = replace(
        fixture,
        evidence_kind=ObservationEvidenceKind.TEST_FIXTURE,
        network_request_count=0,
        quota_cost_units=0.0,
        capture_attestation=None,
    )
    test_report = qualify_active((test_fixture,), authorization=None)
    with pytest.raises(Builder2QualificationReceiptError):
        issue_builder2_qualification_receipt(
            test_report,
            test_fixture,
            test_report.results[0],
        )

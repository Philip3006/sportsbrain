"""Regression tests for the isolated, stub-only network transport bridge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.football.provider_cascade import (
    CascadeTimingPolicy,
    ConfiguredNetworkProviderTransport,
    ControlledShadowExecutionHarness,
    ControlledShadowRunAuthorizationV1,
    NetworkAuthorizationContract,
    ProviderConfig,
    QuotaSnapshot,
    TheOddsAPIAdapter,
    TransportCapability,
)
from src.football.provider_cascade.adapters import RawProviderResponse
from src.football.provider_cascade.execution_harness import (
    HarnessExecutionBlocked,
    ProviderTransportResponse,
)
from src.football.provider_cascade.preparation import preparation_from_input_payload
from src.football.top5_controlled_shadow_provider_qualification import (
    CEOAuthorization,
    ExpectedCascadeFixture,
    ObservationEvidenceKind,
    ProviderQualificationSession,
    ProviderQualificationStatus,
    QualificationTimingPolicy,
    qualify_provider_observations,
)
from src.football.top5_shadow_provider_redundancy import (
    ProviderReadinessState,
    make_fixture_key,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 16, 15, tzinfo=UTC)
KICKOFF = NOW + timedelta(hours=2)
PROVIDER = "the_odds_api"
SOURCE_SHA = "a" * 64
ADAPTER_VERSION = f"{PROVIDER}:adapter-v1"
TIMING = CascadeTimingPolicy(900, 60)


def _fixture_payload() -> dict[str, object]:
    return {
        "fixture_key": make_fixture_key("EPL", "Home FC", "Away FC", KICKOFF),
        "league_code": "EPL",
        "home_team": "Home FC",
        "away_team": "Away FC",
        "kickoff": KICKOFF.isoformat(),
    }


def _readiness(provider: str, *, remaining: int = 10) -> dict[str, object]:
    return {
        "readiness_state": "LIVE_PATH_READY_FOR_OBSERVATION",
        "identity_state": "RESOLVED",
        "fixture_id_known": True,
        "provider_fixture_id": f"{provider}-event",
        "discovery_required": False,
        "quota": {"used": 0, "remaining": remaining},
        "quota_cost_units_per_request": 1.0,
    }


def _prepare(
    *,
    order: tuple[str, ...] = (PROVIDER,),
    remaining: dict[str, int] | None = None,
    maximum_requests: int | None = None,
    maximum_cost: float | None = None,
):
    remaining = remaining or {provider: 10 for provider in order}
    all_providers = order
    return preparation_from_input_payload(
        {
            "fixture": _fixture_payload(),
            "timing_policy": {
                "maximum_odds_age_seconds": TIMING.maximum_odds_age_seconds,
                "kickoff_tolerance_seconds": TIMING.kickoff_tolerance_seconds,
            },
            "provider_order": list(order),
            "credential_presence": {provider: True for provider in all_providers},
            "provider_readiness": {
                provider: _readiness(provider, remaining=remaining.get(provider, 10))
                for provider in all_providers
            },
            "maximum_total_network_requests": (
                len(order) if maximum_requests is None else maximum_requests
            ),
            "maximum_total_quota_cost_units": (
                float(len(order)) if maximum_cost is None else maximum_cost
            ),
        }
    )


def _auth(preparation, *, run_id: str = "run-1"):
    return ControlledShadowRunAuthorizationV1(
        authorization_id="auth-1",
        controlled_shadow_run_id=run_id,
        authorization_nonce="nonce-1",
        ceo_authorization_identity="ceo:canonical",
        preparation_id=preparation.preparation_id,
        preparation_digest=preparation.preparation_digest,
        qualification_session_id="qualification-session-1",
        fixture_key=preparation.fixture_identity["fixture_key"],
        league=preparation.league,
        home_team=preparation.fixture_identity["home_team"],
        away_team=preparation.fixture_identity["away_team"],
        kickoff=preparation.kickoff,
        configured_provider_order=preparation.configured_provider_order,
        timing_policy_digest=preparation.timing_policy_digest,
        maximum_total_network_requests=preparation.maximum_total_network_requests,
        maximum_total_quota_cost_units=preparation.maximum_total_quota_cost_units,
        per_provider_maximums=preparation.per_provider_maximums,
        adapter_version_scope={
            provider: ADAPTER_VERSION
            if provider == PROVIDER
            else f"{provider}:adapter-v1"
            for provider in preparation.configured_provider_order
        },
        adapter_source_sha_scope={
            provider: SOURCE_SHA for provider in preparation.configured_provider_order
        },
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
        zero_monetary_spend=True,
        no_bet=True,
        publication=False,
        production_activation=False,
    )


def _event(*, source: datetime | None = NOW - timedelta(seconds=5)):
    return {
        "id": f"{PROVIDER}-event",
        "sport_key": "soccer_epl",
        "commence_time": KICKOFF.isoformat(),
        "home_team": "Home FC",
        "away_team": "Away FC",
        "bookmakers": [
            {
                "key": "book-a",
                "last_update": source.isoformat() if source else None,
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": source.isoformat() if source else None,
                        "outcomes": [
                            {"name": "Home FC", "price": 2.0},
                            {"name": "Draw", "price": 3.5},
                            {"name": "Away FC", "price": 4.0},
                        ],
                    }
                ],
            }
        ],
    }


def _raw(payload: object, *, status: int = 200) -> RawProviderResponse:
    return RawProviderResponse(
        status,
        payload,
        {"x-requests-remaining": "9"},
        NOW,
        NOW,
        0,
    )


def _bridge(preparation, authorization, http_transport, *, network_ref=None):
    return ConfiguredNetworkProviderTransport(
        provider=PROVIDER,
        adapter=TheOddsAPIAdapter(transport=http_transport),
        provider_config=ProviderConfig(
            name=PROVIDER,
            bookmakers=("book-a",),
            credentials_required=False,
            credential_available=True,
            adapter_version=ADAPTER_VERSION,
            initial_quota=QuotaSnapshot(remaining=10),
        ),
        network_authorization=NetworkAuthorizationContract(
            controlled_shadow_run_ref=network_ref
            or authorization.controlled_shadow_run_id,
            authorized_providers=(PROVIDER,),
        ),
        adapter_source_sha=SOURCE_SHA,
        provider_fixture_id=f"{PROVIDER}-event",
        timing_policy=TIMING,
        clock=lambda: NOW,
    )


def test_network_bridge_requires_exact_run_authorization_before_http():
    preparation = _prepare()
    authorization = _auth(preparation)
    calls = []
    bridge = _bridge(
        preparation,
        authorization,
        lambda request, timeout: calls.append(request) or _raw([_event()]),
        network_ref="other-run",
    )
    with pytest.raises(HarnessExecutionBlocked, match="exact controlled-shadow run"):
        ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
            preparation, authorization, transport=bridge
        )
    assert calls == []


def test_plain_network_capable_test_double_cannot_cross_harness_gate():
    preparation = _prepare()
    authorization = _auth(preparation)

    class Impostor:
        transport_capability = TransportCapability.NETWORK_CAPABLE

        def execute(self, request):
            raise AssertionError("impostor must not be called")

    with pytest.raises(HarnessExecutionBlocked, match="reviewed network bridge"):
        ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
            preparation, authorization, transport=Impostor()
        )


def test_network_bridge_emits_real_observation_and_canonical_b2_attestation():
    preparation = _prepare()
    authorization = _auth(preparation)
    calls = []

    def http_transport(request, timeout):
        calls.append((request, timeout))
        return _raw([_event()])

    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation,
        authorization,
        transport=_bridge(preparation, authorization, http_transport),
    )

    assert len(calls) == 1
    assert result.observation is not None
    assert result.observation.evidence_kind is ObservationEvidenceKind.REAL_OBSERVED
    assert result.observation.capture_attestation is not None
    canonical = result.observation.capture_attestation
    assert canonical.network_execution is True
    assert canonical.ceo_authorization_id == authorization.authorization_id
    assert canonical.controlled_shadow_run_id == authorization.controlled_shadow_run_id
    assert canonical.qualification_session_id == authorization.qualification_session_id
    assert canonical.provider_event_id == f"{PROVIDER}-event"
    assert canonical.provider_request_id == result.observation.provider_request_id
    assert canonical.cascade_evidence_digest
    assert canonical.raw_response_digest == result.observation.raw_response_digest
    assert (
        canonical.normalized_record_digest
        == result.observation.normalized_record_digest
    )
    assert result.cascade_evidence.prediction_input_allowed is False
    assert result.authorization.no_bet is True
    assert result.authorization.publication is False
    assert result.authorization.production_activation is False

    observation = result.observation
    session = ProviderQualificationSession(
        qualification_session_id=observation.qualification_session_id,
        schema_version="top5-controlled-shadow-provider-qualification-v1",
        created_at=observation.captured_at,
        provider_identity="top5_cascade",
        league_scope=(observation.league,),
        fixture_scope=(observation.fixture_key,),
        configured_provider_order=authorization.configured_provider_order,
        adapter_version=observation.adapter_version,
        adapter_source_sha=observation.adapter_source_sha,
        qualification_state=ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION,
    )
    expected = ExpectedCascadeFixture(
        league=observation.league,
        fixture_key=observation.fixture_key,
        home_team=observation.home_team,
        away_team=observation.away_team,
        kickoff=observation.kickoff,
    )
    report = qualify_provider_observations(
        (observation,),
        session,
        expected,
        QualificationTimingPolicy(900, 60, 0, 10_800),
        {PROVIDER: ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION},
        CEOAuthorization(
            authorization_id=authorization.authorization_id,
            controlled_shadow_run_id=authorization.controlled_shadow_run_id,
            qualification_session_id=authorization.qualification_session_id,
            provider_scope=(PROVIDER,),
            league_scope=(observation.league,),
            fixture_scope=(observation.fixture_key,),
            maximum_network_requests=1,
            monetary_spend_authorized=False,
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=10),
        ),
    )
    assert (
        report.results[0].status
        is ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    )


def test_quota_zero_makes_zero_bridge_http_calls():
    preparation = _prepare(
        order=(PROVIDER,),
        remaining={PROVIDER: 0},
        maximum_requests=1,
        maximum_cost=1.0,
    )
    authorization = _auth(preparation)
    calls = []
    bridge = _bridge(
        preparation,
        authorization,
        lambda request, timeout: calls.append(request) or _raw([_event()]),
    )
    with pytest.raises(HarnessExecutionBlocked, match="preparation is not READY"):
        ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
            preparation, authorization, transport=bridge
        )
    assert calls == []


def test_request_cap_exhaustion_stops_before_next_http_call():
    preparation = _prepare(order=(PROVIDER,), maximum_requests=1, maximum_cost=1.0)
    authorization = _auth(preparation)
    calls = []
    bridge = _bridge(
        preparation,
        authorization,
        lambda request, timeout: calls.append(request) or _raw(None, status=429),
    )
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation, authorization, transport=bridge
    )
    assert len(calls) == 1
    assert result.network_request_count == 1
    assert result.observation is None
    assert any("NO_OBSERVATION" in item for item in result.failure_evidence)


@pytest.mark.parametrize("payload", [None, "not-json", [{"id": "wrong-event"}]])
def test_malformed_provider_response_emits_no_real_observation(payload):
    preparation = _prepare()
    authorization = _auth(preparation)
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation,
        authorization,
        transport=_bridge(
            preparation,
            authorization,
            lambda request, timeout, payload=payload: _raw(payload),
        ),
    )
    assert result.observation is None
    assert all(
        item.evidence_kind is not ObservationEvidenceKind.REAL_OBSERVED
        for item in result.attestations
    )


def test_source_timestamp_missing_emits_no_real_observation():
    preparation = _prepare()
    authorization = _auth(preparation)
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation,
        authorization,
        transport=_bridge(
            preparation,
            authorization,
            lambda request, timeout: _raw([_event(source=None)]),
        ),
    )
    assert result.observation is None
    assert all(
        item.evidence_kind is not ObservationEvidenceKind.REAL_OBSERVED
        for item in result.attestations
    )


def test_replay_is_fail_closed_and_does_not_repeat_http():
    preparation = _prepare()
    authorization = _auth(preparation)
    calls = []
    bridge = _bridge(
        preparation,
        authorization,
        lambda request, timeout: calls.append(request) or _raw([_event()]),
    )
    harness = ControlledShadowExecutionHarness(clock=lambda: NOW)
    first = harness.execute(preparation, authorization, transport=bridge)
    second = harness.execute(preparation, authorization, transport=bridge)
    assert first.status.value == "OBSERVED"
    assert second.status.value == "REPLAYED"
    assert len(calls) == 1


def test_bridge_has_no_receipt_or_prediction_authority_and_no_network_default():
    preparation = _prepare()
    authorization = _auth(preparation)
    bridge = _bridge(
        preparation, authorization, lambda request, timeout: _raw([_event()])
    )
    assert not hasattr(bridge, "issue_builder2_qualification_receipt")
    assert not hasattr(bridge, "create_prediction")
    assert not hasattr(bridge, "publish")
    assert not hasattr(bridge, "place_bet")
    assert not hasattr(bridge, "activate_production")
    assert bridge.transport_capability is TransportCapability.NETWORK_CAPABLE


def test_test_injected_response_cannot_become_real_observed():
    preparation = _prepare()
    authorization = _auth(preparation)

    class TestInjectedWithNetworkLabel:
        transport_capability = TransportCapability.TEST_INJECTED

        def execute(self, request):
            return ProviderTransportResponse(
                outcome="SUCCESS",
                provider_event_id="fake-event",
                provider_request_id=request.request_identity,
                home_odds=2.0,
                draw_odds=3.5,
                away_odds=4.0,
                source_timestamp=NOW - timedelta(seconds=5),
                adapter_version=ADAPTER_VERSION,
                adapter_source_sha=SOURCE_SHA,
                evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            )

    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation, authorization, transport=TestInjectedWithNetworkLabel()
    )
    assert result.observation is None
    assert result.status.value == "NO_OBSERVATION"

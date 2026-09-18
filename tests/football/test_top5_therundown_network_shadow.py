"""Offline tests for the disabled network-capable TheRundown shadow path."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.provider_cascade.contracts import (
    FOOTBALL_PROVIDER_REPERTOIRE,
    MARKET_PREMATCH_1X2,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ControlledShadowCaptureAttestation,
    ObservationEvidenceKind,
    QualificationContractError,
    RealProviderObservation,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_network_shadow import (
    NetworkShadowContractError,
    NetworkShadowExecutionBlocked,
    NetworkShadowRunStatus,
    TheRundownCanonicalPayloadAdapterV1,
    TheRundownHttpNetworkTransportV1,
    TheRundownNetworkAuthorizationV1,
    TheRundownNetworkConfigurationV1,
    TheRundownNetworkParticipantScopeV1,
    TheRundownNetworkRequestScopeV1,
    TheRundownNetworkResponseV1,
    TheRundownNetworkShadowExecutorV1,
    TheRundownReplayTransportV1,
)
from src.football.top5_therundown_shadow_canary import (
    CanaryContractError,
    CanaryOutcome,
    TheRundownCanaryNetworkTransport,
    TheRundownCanaryTargetV1,
    TransportCapability,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)
ADAPTER_SHA = "a" * 64
RAW_DIGEST = "b" * 64
PROVIDER_DIGEST = "c" * 64
NORMALIZED_DIGEST = "d" * 64
CASCADE_DIGEST = "e" * 64


def _targets() -> tuple[TheRundownCanaryTargetV1, ...]:
    return tuple(
        TheRundownCanaryTargetV1(
            provider="therundown",
            league=league,
            fixture_key=make_fixture_key(
                league, f"Home {league}", f"Away {league}", NOW + timedelta(hours=2)
            ),
            provider_event_id=f"event-{league}",
            home_team=f"Home {league}",
            away_team=f"Away {league}",
            kickoff=NOW + timedelta(hours=2),
        )
        for league in ("EPL", "BL1", "LL", "SA", "L1")
    )


def _configuration(**changes: object) -> TheRundownNetworkConfigurationV1:
    targets = changes.pop("targets", _targets())
    participants = tuple(
        TheRundownNetworkParticipantScopeV1(
            fixture_key=target.fixture_key,
            home_participant_id=f"home-id-{target.league}",
            away_participant_id=f"away-id-{target.league}",
        )
        for target in targets
    )
    request_scope = tuple(
        (target.fixture_key, f"request-{target.league}") for target in targets
    )
    values: dict[str, object] = {
        "targets": targets,
        "participant_scope": participants,
        "request_scope": tuple(
            TheRundownNetworkRequestScopeV1(fixture_key=key, request_identity=value)
            for key, value in request_scope
        ),
        "adapter_version": "therundown-adapter-v1",
        "adapter_source_sha": ADAPTER_SHA,
        "maximum_request_count": 5,
        "maximum_datapoints": 5,
        "maximum_quota_cost_units": 5.0,
        "request_quota_cost_units": 1.0,
        "maximum_source_age_seconds": 300,
        "minimum_interval_seconds": 1.0,
        "maximum_retries": 0,
        "enabled": False,
    }
    values.update(changes)
    draft = TheRundownNetworkConfigurationV1(
        **values,  # type: ignore[arg-type]
        configuration_digest="0" * 64,
    )
    return replace(draft, configuration_digest=draft.computed_configuration_digest)


def _authorization(
    configuration: TheRundownNetworkConfigurationV1, **changes: object
) -> TheRundownNetworkAuthorizationV1:
    values: dict[str, object] = {
        "authorization_id": "ceo-auth:therundown-network-1",
        "ceo_authorization_identity": "ceo:canonical",
        "controlled_shadow_run_id": "controlled-shadow:therundown-network-1",
        "qualification_session_id": "qualification-session:therundown-network-1",
        "provider": "therundown",
        "targets": configuration.targets,
        "participant_scope": configuration.participant_scope,
        "request_scope": configuration.request_scope,
        "adapter_version": configuration.adapter_version,
        "adapter_source_sha": configuration.adapter_source_sha,
        "configuration_digest": configuration.configuration_digest,
        "maximum_request_count": configuration.maximum_request_count,
        "maximum_datapoints": configuration.maximum_datapoints,
        "maximum_quota_cost_units": configuration.maximum_quota_cost_units,
        "request_quota_cost_units": configuration.request_quota_cost_units,
        "maximum_source_age_seconds": configuration.maximum_source_age_seconds,
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=10),
        "minimum_interval_seconds": configuration.minimum_interval_seconds,
        "maximum_retries": 0,
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "monetary_spend_authorized": False,
    }
    values.update(changes)
    return TheRundownNetworkAuthorizationV1(**values)  # type: ignore[arg-type]


def _response(request, **changes: object) -> TheRundownNetworkResponseV1:
    participant_home = request.home_participant_id
    participant_away = request.away_participant_id
    values: dict[str, object] = {
        "outcome": CanaryOutcome.SUCCESS,
        "provider": request.target.provider,
        "league": request.target.league,
        "fixture_key": request.target.fixture_key,
        "provider_event_id": request.target.provider_event_id,
        "provider_request_id": request.request_identity,
        "home_team": request.target.home_team,
        "away_team": request.target.away_team,
        "home_participant_id": participant_home,
        "away_participant_id": participant_away,
        "bookmaker_identity": "bookmaker-a",
        "source_identity": "therundown-feed",
        "source_timestamp": NOW - timedelta(seconds=5),
        "captured_at": NOW,
        "request_started_at": NOW - timedelta(seconds=1),
        "request_finished_at": NOW,
        "home_odds": 2.1,
        "draw_odds": 3.4,
        "away_odds": 3.2,
        "adapter_version": "therundown-adapter-v1",
        "adapter_source_sha": ADAPTER_SHA,
        "raw_response_digest": RAW_DIGEST,
        "provider_record_digest": PROVIDER_DIGEST,
        "normalized_record_digest": NORMALIZED_DIGEST,
        "cascade_evidence_digest": CASCADE_DIGEST,
        "quota_before": 100,
        "quota_after": 99,
        "quota_cost_units": 1.0,
        "datapoint_count": 1,
        "rate_limit_remaining": 99,
        "rate_limit_reset_at": NOW + timedelta(hours=1),
        "account_tier": "shadow-test-tier",
        "provider_delay_seconds": 0.0,
        "http_status": 200,
        "retry_count": 0,
        "evidence_kind": ObservationEvidenceKind.TEST_FIXTURE,
        "network_execution": False,
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "monetary_spend_authorized": False,
    }
    values.update(changes)
    return TheRundownNetworkResponseV1(**values)  # type: ignore[arg-type]


def _run(
    *,
    response_factory=None,
    configuration=None,
    authorization=None,
    pacer=None,
    transport=None,
):
    configuration = configuration or _configuration(enabled=True)
    authorization = authorization or _authorization(configuration)
    transport = transport or TheRundownReplayTransportV1(
        response_factory or (lambda request: _response(request)), clock=lambda: NOW
    )
    executor = TheRundownNetworkShadowExecutorV1(
        clock=lambda: NOW,
        pacer=pacer or (lambda seconds: None),
    )
    return executor.run(configuration, authorization, transport=transport), transport


class _UntrustedReplayTransport(TheRundownCanaryNetworkTransport):
    """Test-only seam that lets the executor see untrusted response bindings."""

    test_only = True
    transport_capability = TransportCapability.NETWORK_CAPABLE

    def __init__(self, response_factory):
        self.response_factory = response_factory
        self.calls = []

    def execute(self, request):
        request.validate(now=NOW)
        self.calls.append(request)
        return self.response_factory(request)


class _NetworkStubTransport(_UntrustedReplayTransport):
    """In-memory HTTP-equivalent transport; it never opens a socket."""

    test_only = False


def test_successful_five_league_replay_is_sequential_and_complete():
    pacing: list[float] = []
    result, transport = _run(pacer=pacing.append)

    assert result.status is NetworkShadowRunStatus.COMPLETED_REPLAY
    assert result.all_five_succeeded is True
    assert len(result.captures) == 5
    assert [request.target.league for request in transport.calls] == [
        "EPL",
        "BL1",
        "LL",
        "SA",
        "L1",
    ]
    assert pacing == [1.0] * 4
    assert result.request_count == 5
    assert result.datapoint_count == 5
    assert result.quota_cost_units == 5.0
    for capture in result.captures:
        assert capture.evidence_kind is ObservationEvidenceKind.TEST_FIXTURE
        assert capture.network_execution is False
        assert capture.candidate_only is True
        assert capture.receipt_eligible is False
        payload = capture.as_payload()
        assert payload["participant_scope"]["home_participant_id"].startswith(
            "home-id-"
        )
        assert payload["response"]["rate_limit_remaining"] == 99
        assert payload["response"]["account_tier"] == "shadow-test-tier"
        assert payload["response"]["provider_delay_seconds"] == 0.0


def test_expired_authorization_is_rejected_before_transport():
    configuration = _configuration(enabled=True)
    authorization = _authorization(
        configuration,
        issued_at=NOW - timedelta(minutes=10),
        expires_at=NOW - timedelta(seconds=1),
    )
    transport = TheRundownReplayTransportV1(lambda request: pytest.fail("called"))
    with pytest.raises(NetworkShadowExecutionBlocked, match="expired"):
        TheRundownNetworkShadowExecutorV1(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )
    assert transport.calls == []


@pytest.mark.parametrize("field", ["provider", "league"])
def test_wrong_provider_or_league_scope_fails_closed(field):
    targets = list(_targets())
    if field == "provider":
        targets[0] = replace(targets[0], provider="other-provider")
    else:
        targets[0] = replace(
            targets[0],
            league="CL",
            fixture_key=targets[0].fixture_key,
        )
    with pytest.raises((NetworkShadowContractError, CanaryContractError)):
        _configuration(targets=tuple(targets))


def test_request_budget_violation_makes_zero_calls():
    configuration = _configuration(enabled=True, maximum_request_count=4)
    authorization = _authorization(configuration)
    transport = TheRundownReplayTransportV1(lambda request: pytest.fail("called"))
    with pytest.raises(NetworkShadowExecutionBlocked, match="request budget"):
        TheRundownNetworkShadowExecutorV1(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )
    assert transport.calls == []


def test_datapoint_overrun_stops_before_next_capture():
    configuration = _configuration(enabled=True, maximum_datapoints=5)
    result, transport = _run(
        response_factory=lambda request: _response(request, datapoint_count=2),
        configuration=configuration,
    )
    assert result.status is NetworkShadowRunStatus.PARTIAL
    assert any("DATAPOINT_BUDGET_OVERRUN" in failure for failure in result.failures)
    assert len(transport.calls) == 3
    assert result.datapoint_count == 4


def test_quota_overrun_fails_before_accepting_response():
    configuration = _configuration(enabled=True, request_quota_cost_units=1.0)
    result, transport = _run(
        response_factory=lambda request: _response(request, quota_cost_units=2.0),
        configuration=configuration,
    )
    assert result.status is NetworkShadowRunStatus.PARTIAL
    assert any("quota budget overrun" in failure for failure in result.failures)
    assert len(transport.calls) == 1
    assert result.captures == ()


@pytest.mark.parametrize(
    "changes, pattern",
    [
        ({"source_timestamp": NOW - timedelta(seconds=301)}, "stale odds"),
        ({"home_odds": None}, "home_odds"),
        ({"draw_odds": None}, "draw_odds"),
        ({"away_odds": None}, "away_odds"),
        ({"home_participant_id": "wrong"}, "participant mismatch"),
        ({"provider_event_id": "wrong-event"}, "provider_event_id"),
        ({"provider_request_id": "wrong-request"}, "provider_request_id"),
        ({"bookmaker_identity": ""}, "bookmaker/source"),
        ({"source_identity": ""}, "bookmaker/source"),
        ({"source_timestamp": None}, "source/capture timestamp"),
        ({"rate_limit_remaining": None}, "rate-limit evidence"),
        ({"rate_limit_reset_at": None}, "rate-limit reset"),
        ({"account_tier": ""}, "account_tier"),
        ({"provider_delay_seconds": None}, "provider delay"),
    ],
)
def test_invalid_capture_evidence_fails_closed(changes, pattern):
    transport = None
    if pattern in {"participant mismatch", "provider_event_id", "provider_request_id"}:
        transport = _UntrustedReplayTransport(
            lambda request: _response(request, **changes)
        )
    result, _ = _run(
        response_factory=lambda request: _response(request, **changes),
        transport=transport,
    )
    assert result.status is NetworkShadowRunStatus.PARTIAL
    assert any(pattern in failure for failure in result.failures)


def test_one_league_failure_returns_partial_without_retry_or_fallback():
    def response(request):
        if request.target.league == "LL":
            return _response(request, outcome=CanaryOutcome.RATE_LIMITED)
        return _response(request)

    result, transport = _run(response_factory=response)
    assert result.status is NetworkShadowRunStatus.PARTIAL
    assert any("LL:RATE_LIMITED" in failure for failure in result.failures)
    assert len(transport.calls) == 5
    assert [request.sequence for request in transport.calls] == [0] * 5


def test_partial_five_league_run_transport_error_is_recorded():
    def response(request):
        if request.target.league == "SA":
            raise RuntimeError("stub transport failure")
        return _response(request)

    result, transport = _run(response_factory=response)
    assert result.status is NetworkShadowRunStatus.PARTIAL
    assert any(
        "SA:TRANSPORT_ERROR:RuntimeError" in failure for failure in result.failures
    )
    assert len(transport.calls) == 5
    assert len(result.captures) == 4


def test_generic_http_rate_limit_response_is_no_observation():
    result, _ = _run(
        response_factory=lambda request: _response(
            request,
            outcome=CanaryOutcome.RATE_LIMITED,
            http_status=429,
        )
    )
    assert result.status is NetworkShadowRunStatus.PARTIAL
    assert result.captures[0].observation_input is None
    assert result.receipt_eligible is False


def test_live_transport_is_inert_without_explicit_live_enablement():
    calls = []

    class NoCallHttpClient:
        def execute(self, request):
            calls.append(request)
            pytest.fail("HTTP client was called")

    configuration = _configuration(enabled=True)
    authorization = _authorization(configuration)
    transport = TheRundownHttpNetworkTransportV1(
        endpoint="https://example.invalid/odds",
        api_key="test-secret-not-logged",
        adapter=TheRundownCanonicalPayloadAdapterV1(),
        http_client=NoCallHttpClient(),
    )
    with pytest.raises(NetworkShadowExecutionBlocked, match="live network execution"):
        TheRundownNetworkShadowExecutorV1(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )
    assert calls == []
    assert transport.calls == []


def test_shadow_success_never_implies_receipt_authority_or_active_provider():
    result, _ = _run()
    assert result.all_five_succeeded is True
    assert result.receipt_eligible is False
    assert result.authority_changed is False
    assert result.publication is False
    assert result.production_activation is False
    assert result.monetary_spend_authorized is False
    assert FOOTBALL_PROVIDER_REPERTOIRE == ("the_odds_api",)
    assert all(capture.candidate_only for capture in result.captures)
    assert all(capture.receipt_eligible is False for capture in result.captures)


def test_network_shaped_five_league_output_matches_downstream_input_shapes():
    configuration = _configuration(enabled=True)
    authorization = _authorization(configuration)
    transport = _NetworkStubTransport(
        lambda request: _response(
            request,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            network_execution=True,
        )
    )
    result = TheRundownNetworkShadowExecutorV1(
        clock=lambda: NOW,
        allow_live_network=True,
    ).run(configuration, authorization, transport=transport)

    assert result.status is NetworkShadowRunStatus.COMPLETED_NETWORK
    assert result.all_five_succeeded is True
    assert len(result.captures) == 5
    for capture in result.captures:
        attestation = ControlledShadowCaptureAttestation.from_payload(
            capture.canonical_capture_attestation
        )
        # The canonical shape is complete, but current qualification correctly
        # rejects TheRundown because it is outside the active repertoire.
        with pytest.raises(QualificationContractError, match="candidate"):
            attestation.validate()
        observation = RealProviderObservation.from_payload(capture.observation_input)
        with pytest.raises(QualificationContractError, match="unknown provider"):
            observation.validate_structural()
        receipt_input = capture.builder2_receipt_input
        assert receipt_input["issuer_present"] is False
        assert receipt_input["eligible"] is False
        assert receipt_input["available_evidence"]["configuration_digest"] == (
            configuration.configuration_digest
        )
        payload = capture.as_payload()
        assert payload["safety"] == {
            "no_bet": True,
            "publication": False,
            "production_activation": False,
            "monetary_spend_authorized": False,
            "authority_changed": False,
            "scheduler_registered": False,
            "ledger_mutated": False,
        }


def test_network_request_contract_has_no_retry_and_exact_scope_binding():
    configuration = _configuration(enabled=True)
    authorization = _authorization(configuration)
    request = authorization.request_for(configuration.targets[0], configuration)
    request.validate(now=NOW)
    assert request.target.provider == "therundown"
    assert request.target.league == "EPL"
    assert request.request_identity == "request-EPL"
    assert request.sequence == 0
    assert request.market_type == MARKET_PREMATCH_1X2

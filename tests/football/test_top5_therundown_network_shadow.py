"""Offline tests for the disabled network-capable TheRundown shadow path."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    TheRundownNetworkHttpResponseV1,
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


def _run006_http_fixture():
    body_path = Path(
        "/private/tmp/top5-laliga-nextdate-20260920-006.request-2.body.json"
    )
    headers_path = Path(
        "/private/tmp/top5-laliga-nextdate-20260920-006.request-2.headers.json"
    )
    if not body_path.exists() or not headers_path.exists():
        pytest.skip("canonical Run-006 HTTP fixture is not present")
    body = json.loads(body_path.read_text(encoding="utf-8"))
    headers = json.loads(headers_path.read_text(encoding="utf-8"))
    target_event = body["events"][0]
    kickoff = datetime.fromisoformat("2026-09-20T12:00:00+00:00")
    targets = [
        replace(target, provider="therundown_experimental") for target in _targets()
    ]
    target = TheRundownCanaryTargetV1(
        provider="therundown_experimental",
        league="LL",
        fixture_key=make_fixture_key("LL", "Getafe", "Málaga", kickoff),
        provider_event_id=target_event["event_id"],
        home_team="Getafe",
        away_team="Málaga",
        kickoff=kickoff,
    )
    targets[2] = target
    participants = list(_configuration(targets=tuple(targets)).participant_scope)
    participants[2] = TheRundownNetworkParticipantScopeV1(
        fixture_key=target.fixture_key,
        home_participant_id="3952",
        away_participant_id="133109",
    )
    request_scope = list(_configuration(targets=tuple(targets)).request_scope)
    request_scope[2] = TheRundownNetworkRequestScopeV1(
        fixture_key=target.fixture_key,
        request_identity="request-LL",
    )
    configuration = _configuration(
        targets=tuple(targets),
        participant_scope=tuple(participants),
        request_scope=tuple(request_scope),
        enabled=True,
        maximum_datapoints=275,
        maximum_quota_cost_units=275.0,
        request_quota_cost_units=55.0,
        minimum_interval_seconds=1.1,
    )
    authorization = _authorization(
        configuration,
        provider="therundown_experimental",
        issued_at=datetime(2026, 9, 19, 22, 30, tzinfo=UTC),
        expires_at=datetime(2026, 9, 19, 23, 30, tzinfo=UTC),
    )
    request = authorization.request_for(target, configuration)
    http_response = TheRundownNetworkHttpResponseV1(
        status_code=200,
        payload=body,
        headers=headers,
        started_at=datetime(2026, 9, 19, 22, 40, 35, tzinfo=UTC),
        finished_at=datetime(2026, 9, 19, 22, 40, 36, 431436, tzinfo=UTC),
    )
    return request, http_response


def test_real_run006_http_shape_uses_provider_billed_datapoints_and_headers():
    request, http_response = _run006_http_fixture()
    response = TheRundownCanonicalPayloadAdapterV1(
        adapter_version="therundown-v2-experimental:2",
        adapter_source_sha=ADAPTER_SHA,
    ).decode_response(request, http_response)

    assert response.outcome is CanaryOutcome.SUCCESS
    assert response.evidence_kind is ObservationEvidenceKind.REAL_OBSERVED
    assert response.network_execution is True
    assert response.bookmaker_identity == "betmgm"
    assert (response.home_odds, response.draw_odds, response.away_odds) == (
        2.05,
        3.05,
        4.3,
    )
    assert response.datapoint_count == 55
    assert response.quota_cost_units == 55.0
    assert (response.quota_before, response.quota_after) == (20000, 19945)
    assert response.source_timestamp == datetime(
        2026, 9, 19, 22, 35, 36, 431436, tzinfo=UTC
    )
    assert response.raw_metadata["source_timestamp_method"] == (
        "response_finished_at_minus_x_data_delay_seconds"
    )
    assert response.raw_metadata["billing_unit"] == "provider_billed_x_datapoints"
    assert response.raw_metadata["x_datapoints_used_after"] == 55
    assert response.raw_metadata["x_datapoints_remaining_after"] == 19945
    assert response.raw_metadata["rate_limit_remaining_unavailable"] is True
    assert response.raw_metadata["rate_limit_reset_unavailable"] is True
    assert response.provider_record_digest == (
        "2eabb443c539240dc5db0866d69e500b1b62b8d943ca42d03e4b3aae818eb74b"
    )


def test_real_run006_http_transport_path_is_header_and_payload_complete():
    request, http_response = _run006_http_fixture()

    class OfflineHttpClient:
        def execute(self, http_request):
            assert (
                http_request.headers["X-SportsBrain-Request-Identity"]
                == request.request_identity
            )
            return http_response

    transport = TheRundownHttpNetworkTransportV1(
        endpoint="https://example.invalid/therundown",
        api_key="offline-test-key",
        adapter=TheRundownCanonicalPayloadAdapterV1(adapter_source_sha=ADAPTER_SHA),
        http_client=OfflineHttpClient(),
        clock=lambda: datetime(2026, 9, 19, 22, 40, 36, tzinfo=UTC),
    )
    response = transport.execute(request)
    assert response.outcome is CanaryOutcome.SUCCESS
    assert response.network_execution is True
    assert response.datapoint_count == 55
    assert response.quota_cost_units == 55.0
    assert len(transport.calls) == 1


def test_real_run006_missing_billing_header_fails_closed():
    request, http_response = _run006_http_fixture()
    headers = dict(http_response.headers)
    headers.pop("x-datapoints")
    response = TheRundownCanonicalPayloadAdapterV1(
        adapter_source_sha=ADAPTER_SHA
    ).decode_response(request, replace(http_response, headers=headers))
    assert response.outcome is CanaryOutcome.MALFORMED
    assert response.datapoint_count == 0
    assert response.network_execution is True


def test_real_run006_contradictory_billing_counters_fail_closed():
    request, http_response = _run006_http_fixture()
    headers = dict(http_response.headers)
    headers["x-datapoints-remaining"] = "19944"
    response = TheRundownCanonicalPayloadAdapterV1(
        adapter_source_sha=ADAPTER_SHA
    ).decode_response(request, replace(http_response, headers=headers))
    assert response.outcome is CanaryOutcome.MALFORMED
    assert response.datapoint_count == 0


def test_real_run006_missing_bookmaker_header_fails_closed():
    request, http_response = _run006_http_fixture()
    headers = dict(http_response.headers)
    headers.pop("x-bookmakers")
    response = TheRundownCanonicalPayloadAdapterV1(
        adapter_source_sha=ADAPTER_SHA
    ).decode_response(request, replace(http_response, headers=headers))
    assert response.outcome is CanaryOutcome.MALFORMED
    assert response.datapoint_count == 0


@pytest.mark.parametrize(
    ("maximum_datapoints", "maximum_quota_cost_units", "expected_status"),
    [
        (300, 300.0, NetworkShadowRunStatus.COMPLETED_REPLAY),
        (275, 275.0, NetworkShadowRunStatus.COMPLETED_REPLAY),
        (274, 275.0, NetworkShadowRunStatus.PARTIAL),
    ],
)
def test_provider_billed_five_request_budget_is_under_exact_or_over_bound(
    maximum_datapoints, maximum_quota_cost_units, expected_status
):
    configuration = _configuration(
        enabled=True,
        maximum_datapoints=maximum_datapoints,
        maximum_quota_cost_units=maximum_quota_cost_units,
        request_quota_cost_units=55.0,
    )
    result, transport = _run(
        configuration=configuration,
        response_factory=lambda request: _response(
            request, datapoint_count=55, quota_cost_units=55.0
        ),
    )
    assert result.status is expected_status
    assert len(transport.calls) == 5
    if expected_status is NetworkShadowRunStatus.PARTIAL:
        assert result.datapoint_count == 220
        assert any("DATAPOINT_BUDGET_OVERRUN" in item for item in result.failures)
    else:
        assert result.datapoint_count == 275
        assert result.quota_cost_units == 275.0

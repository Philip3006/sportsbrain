"""Offline tests for the disabled network-capable TheRundown shadow path."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.football.odds.therundown import (
    THERUNDOWN_ADAPTER_VERSION,
    THERUNDOWN_PROVIDER_NAME,
)
from src.football.provider_cascade.contracts import (
    FOOTBALL_PROVIDER_REPERTOIRE,
    MARKET_PREMATCH_1X2,
    QuotaSnapshot,
    digest_record,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ControlledShadowCaptureAttestation,
    ObservationEvidenceKind,
    QualificationContractError,
    RealProviderObservation,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_network_shadow import (
    QUOTA_PROOF_AFFILIATE_IDS,
    THERUNDOWN_DATED_SNAPSHOT_QUOTA_PROOF_DATAPOINTS_PER_REQUEST,
    THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST,
    TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET,
    TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS,
    TOP5_CONTROLLED_SHADOW_QUOTA_BUDGET,
    TOP5_CONTROLLED_SHADOW_REQUEST_COUNT,
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
    TheRundownQuotaHeadroomEvidenceV1,
    TheRundownReplayTransportV1,
    bind_request_rate_limit_provenance,
    PREVIOUS_RESPONSE_PACING_ANCHOR,
    SHADOW_PROOF_PACING_ANCHOR,
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
        "maximum_request_count": TOP5_CONTROLLED_SHADOW_REQUEST_COUNT,
        "maximum_datapoints": TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET,
        "maximum_quota_cost_units": TOP5_CONTROLLED_SHADOW_QUOTA_BUDGET,
        "request_quota_cost_units": float(THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST),
        "maximum_source_age_seconds": 300,
        "minimum_interval_seconds": TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS,
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
        "quota_cost_units": float(THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST),
        "datapoint_count": THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST,
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
        "raw_metadata": {
            "provider_billing": {
                "x-rate-limit": 1,
                "x-datapoints-reset": "2026-09-26T00:00:00Z",
            }
        },
    }
    values.update(changes)
    return TheRundownNetworkResponseV1(**values)  # type: ignore[arg-type]


def _quota_headroom(authorization, *, observed_at=NOW):
    evidence = TheRundownQuotaHeadroomEvidenceV1(
        provider=authorization.provider,
        account_scope="network-test-account",
        observed_remaining_datapoints=275,
        observed_at=observed_at,
        provenance_source="offline-test-fixture",
        provenance_digest="e" * 64,
        authorization_package_digest="f" * 64,
        authorization_id=authorization.authorization_id,
        controlled_shadow_run_id=authorization.controlled_shadow_run_id,
        qualification_session_id=authorization.qualification_session_id,
        ceo_authorization_identity=authorization.ceo_authorization_identity,
        evidence_digest="0" * 64,
    )
    evidence = replace(evidence, evidence_digest=evidence.computed_evidence_digest)
    return replace(
        authorization,
        quota_headroom_evidence_digest=evidence.evidence_digest,
    ), evidence


def _run_live_network_stub(*, observed_at, response_factory=None, fail_closed=False):
    configuration = _configuration(enabled=True)
    authorization, headroom = _quota_headroom(_authorization(configuration))
    headroom = replace(headroom, observed_at=observed_at, evidence_digest="0" * 64)
    headroom = replace(headroom, evidence_digest=headroom.computed_evidence_digest)
    authorization = replace(
        authorization, quota_headroom_evidence_digest=headroom.evidence_digest
    )
    result, transport, pacing, _clock = _run_live_network_fixture(
        configuration,
        authorization,
        headroom,
        response_factory
        or (
            lambda request: _response(
                request,
                evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
                network_execution=True,
                account_tier="free",
                provider_delay_seconds=300.0,
            )
        ),
        fail_closed=fail_closed,
    )
    return result, transport, pacing


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


class _MutableClock:
    def __init__(self, current: datetime | None = None):
        self.current = current if current is not None else NOW

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class _NetworkStubTransport(_UntrustedReplayTransport):
    """In-memory HTTP-equivalent transport; it never opens a socket."""

    test_only = False

    def __init__(self, response_factory, *, clock: _MutableClock | None = None):
        super().__init__(response_factory)
        self.clock = clock

    def execute(self, request):
        response = super().execute(request)
        if (
            self.clock is not None
            and CanaryOutcome(response.outcome) is CanaryOutcome.SUCCESS
        ):
            started = self.clock()
            finished = started + timedelta(milliseconds=240)
            self.clock.current = finished
            response = replace(
                response,
                source_timestamp=started - timedelta(seconds=5),
                captured_at=finished,
                request_started_at=started,
                request_finished_at=finished,
            )
        return response


def _run_live_network_fixture(
    configuration,
    authorization,
    quota_headroom,
    response_factory,
    *,
    fail_closed=False,
    post_capture_validator=None,
    start_at=None,
):
    clock = _MutableClock(start_at)
    transport = _NetworkStubTransport(response_factory, clock=clock)
    pacing = []

    def pace(seconds):
        pacing.append(seconds)
        clock.advance(seconds)

    result = TheRundownNetworkShadowExecutorV1(
        clock=clock,
        pacer=pace,
        allow_live_network=True,
        fail_closed_immediately=fail_closed,
    ).run(
        configuration,
        authorization,
        transport=transport,
        quota_headroom=quota_headroom,
        post_capture_validator=post_capture_validator,
    )
    return result, transport, pacing, clock


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
    assert pacing == [TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS] * 4
    assert result.request_count == 5
    assert result.request_count == TOP5_CONTROLLED_SHADOW_REQUEST_COUNT
    assert result.datapoint_count == TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET
    assert result.quota_cost_units == TOP5_CONTROLLED_SHADOW_QUOTA_BUDGET
    for capture in result.captures:
        assert capture.evidence_kind is ObservationEvidenceKind.TEST_FIXTURE
        assert capture.network_execution is False
        assert capture.candidate_only is True
        assert capture.receipt_eligible is False
        payload = capture.as_payload()
        assert payload["participant_scope"]["home_participant_id"].startswith(
            "home-id-"
        )
        assert "rate_limit_remaining" not in payload["response"]
        assert "rate_limit_reset_at" not in payload["response"]
        assert payload["response"]["account_tier"] == "shadow-test-tier"
        assert payload["response"]["provider_delay_seconds"] == 0.0


def test_live_first_request_waits_only_remaining_proof_interval():
    result, transport, pacing = _run_live_network_stub(
        observed_at=NOW - timedelta(milliseconds=400)
    )

    assert result.status is NetworkShadowRunStatus.COMPLETED_NETWORK
    assert result.request_count == 5
    assert result.datapoint_count == TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET
    assert [request.target.league for request in transport.calls] == [
        "EPL",
        "BL1",
        "LL",
        "SA",
        "L1",
    ]
    assert pacing == pytest.approx(
        [0.7] + [TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS] * 4
    )
    assert TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS == 1.1
    assert result.authority_changed is False
    assert result.publication is False
    assert result.production_activation is False
    assert result.monetary_spend_authorized is False
    assert result.receipt_eligible is False
    assert all(capture.candidate_only for capture in result.captures)
    assert all(capture.response.no_bet for capture in result.captures)
    assert all(capture.response.retry_count == 0 for capture in result.captures)
    assert all(capture.response.account_tier == "free" for capture in result.captures)
    assert all(not capture.response.ledger_mutated for capture in result.captures)
    assert [
        capture.response.request_rate_limit_provenance.observed_pacing_interval_seconds
        for capture in result.captures
    ] == pytest.approx([1.1] * 5)
    assert [
        capture.response.request_rate_limit_provenance.pacing_anchor_kind
        for capture in result.captures
    ] == [SHADOW_PROOF_PACING_ANCHOR] + [PREVIOUS_RESPONSE_PACING_ANCHOR] * 4
    assert all(
        capture.response.request_rate_limit_provenance.requests_per_second_ceiling == 1
        for capture in result.captures
    )


def test_serialized_live_response_restores_enum_types_for_offline_reconciliation():
    result, _transport, _pacing = _run_live_network_stub(
        observed_at=NOW - timedelta(seconds=10)
    )

    response = TheRundownNetworkResponseV1.from_payload(
        result.captures[0].as_payload()["response"]
    )

    assert response.outcome is CanaryOutcome.SUCCESS
    assert response.evidence_kind is ObservationEvidenceKind.REAL_OBSERVED


def test_live_first_request_does_not_wait_after_proof_interval_elapsed():
    result, transport, pacing = _run_live_network_stub(
        observed_at=NOW - timedelta(seconds=2)
    )

    assert result.status is NetworkShadowRunStatus.COMPLETED_NETWORK
    assert result.request_count == 5
    assert [request.target.league for request in transport.calls] == [
        "EPL",
        "BL1",
        "LL",
        "SA",
        "L1",
    ]
    assert pacing == [TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS] * 4


def test_live_rate_limit_fails_closed_without_retry():
    result, transport, pacing = _run_live_network_stub(
        observed_at=NOW - timedelta(seconds=2),
        response_factory=lambda request: _response(
            request,
            outcome=CanaryOutcome.RATE_LIMITED,
            http_status=429,
        ),
        fail_closed=True,
    )

    assert result.status is NetworkShadowRunStatus.PARTIAL
    assert result.failures == ("EPL:provider returned HTTP 429 rate-limit response",)
    assert result.request_count == 1
    assert len(transport.calls) == 1
    assert [request.target.league for request in transport.calls] == ["EPL"]
    assert pacing == []


def _bind_rate_provenance(
    *,
    ceiling=1,
    gap_seconds=1.1,
    minimum_interval=1.1,
    status=200,
    retries=0,
    provider_billing=None,
):
    configuration = _configuration(enabled=True)
    authorization = _authorization(configuration)
    request = authorization.request_for(configuration.targets[0], configuration)
    response = _response(
        request,
        request_started_at=NOW,
        request_finished_at=NOW + timedelta(milliseconds=240),
        captured_at=NOW + timedelta(milliseconds=240),
        http_status=status,
        retry_count=retries,
        raw_metadata={
            "provider_billing": provider_billing
            if provider_billing is not None
            else {
                "x-rate-limit": ceiling,
                "x-datapoints-reset": "2026-09-26T00:00:00Z",
            }
        },
    )
    return bind_request_rate_limit_provenance(
        response,
        authorized_minimum_interval_seconds=minimum_interval,
        pacing_anchor_kind=SHADOW_PROOF_PACING_ANCHOR,
        pacing_anchor_at=NOW - timedelta(seconds=gap_seconds),
        pacing_anchor_digest="f" * 64,
    )


def test_provider_rps_provenance_is_typed_digest_bound_and_has_no_fake_remaining_reset():
    response = _bind_rate_provenance()
    record = response.request_rate_limit_provenance
    assert record is not None
    assert record.provenance_kind == ("PROVIDER_REPORTED_REQUESTS_PER_SECOND_CEILING")
    assert record.source_header == "X-Rate-Limit"
    assert record.requests_per_second_ceiling == 1
    assert record.authorized_minimum_interval_seconds == 1.1
    assert record.provenance_digest == record.computed_provenance_digest
    payload = response.request_rate_limit_provenance.as_payload()
    assert payload["schema_version"].endswith("-v1")
    assert "rate_limit_remaining" not in payload
    assert "rate_limit_reset_at" not in payload
    assert response.raw_metadata["provider_billing"]["x-datapoints-reset"]
    other_ceiling = _bind_rate_provenance(ceiling=2)
    assert (
        other_ceiling.request_rate_limit_provenance.provenance_digest
        != record.provenance_digest
    )


@pytest.mark.parametrize(
    "billing",
    [
        {"x-datapoints-reset": "2026-09-26T00:00:00Z"},
        {"x-rate-limit": 0},
        {"x-rate-limit": -1},
        {"x-rate-limit": "invalid"},
        {"x-rate-limit": True},
    ],
)
def test_invalid_or_missing_provider_rps_header_fails_closed(billing):
    with pytest.raises(NetworkShadowExecutionBlocked, match="X-Rate-Limit"):
        _bind_rate_provenance(provider_billing=billing)


def test_pacing_faster_than_provider_rps_ceiling_fails_closed():
    with pytest.raises(NetworkShadowExecutionBlocked, match="pacing"):
        _bind_rate_provenance(ceiling=2, gap_seconds=0.4, minimum_interval=0.6)


@pytest.mark.parametrize(
    "changes, pattern",
    [
        ({"status": 429}, "HTTP 200"),
        ({"retries": 1}, "forbids retries"),
    ],
)
def test_request_rate_provenance_rejects_429_and_retries(changes, pattern):
    with pytest.raises(NetworkShadowExecutionBlocked, match=pattern):
        _bind_rate_provenance(**changes)


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


def test_stale_datapoint_budget_is_rejected_before_transport():
    configuration = _configuration(enabled=True, maximum_datapoints=5)
    authorization = _authorization(configuration)
    transport = TheRundownReplayTransportV1(lambda request: pytest.fail("called"))
    with pytest.raises(NetworkShadowExecutionBlocked, match="datapoint budget"):
        TheRundownNetworkShadowExecutorV1(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )
    assert transport.calls == []


def test_stale_request_billing_budget_is_rejected_before_transport():
    configuration = _configuration(enabled=True, request_quota_cost_units=1.0)
    authorization = _authorization(configuration)
    transport = TheRundownReplayTransportV1(lambda request: pytest.fail("called"))
    with pytest.raises(NetworkShadowExecutionBlocked, match="billing budget"):
        TheRundownNetworkShadowExecutorV1(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )
    assert transport.calls == []


def test_observed_provider_billing_units_are_the_run_budget_basis():
    assert THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST == 55
    assert TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET == 5 * 55
    assert TOP5_CONTROLLED_SHADOW_QUOTA_BUDGET == 5 * 55


def test_dated_snapshot_proof_billing_cap_is_decoupled_from_discovery_cost():
    assert THERUNDOWN_DATED_SNAPSHOT_QUOTA_PROOF_DATAPOINTS_PER_REQUEST == 56
    assert THERUNDOWN_DATED_SNAPSHOT_QUOTA_PROOF_DATAPOINTS_PER_REQUEST != (
        THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST
    )


def test_exact_five_request_billing_budget_is_accepted():
    result, _ = _run()
    assert result.request_count == 5
    assert result.datapoint_count == 275
    assert result.quota_cost_units == 275.0


def test_over_budget_billed_response_fails_closed():
    result, transport = _run(
        response_factory=lambda request: _response(
            request,
            datapoint_count=56,
            quota_cost_units=56.0,
        )
    )
    assert result.status is NetworkShadowRunStatus.PARTIAL
    assert any("quota budget overrun" in failure for failure in result.failures)
    assert len(transport.calls) == 1


def test_billing_units_must_reconcile_per_response():
    result, _ = _run(
        response_factory=lambda request: _response(
            request,
            datapoint_count=55,
            quota_cost_units=54.0,
        )
    )
    assert result.status is NetworkShadowRunStatus.PARTIAL
    assert any(
        "billing units do not reconcile" in failure for failure in result.failures
    )


def test_pacing_below_one_point_one_seconds_is_rejected():
    configuration = _configuration(enabled=True, minimum_interval_seconds=1.0)
    authorization = _authorization(configuration)
    transport = TheRundownReplayTransportV1(lambda request: pytest.fail("called"))
    with pytest.raises(NetworkShadowExecutionBlocked, match="1.1 seconds"):
        TheRundownNetworkShadowExecutorV1(clock=lambda: NOW).run(
            configuration, authorization, transport=transport
        )
    assert transport.calls == []


def test_provider_billing_headers_bind_to_response_units():
    configuration = _configuration(enabled=True)
    authorization = _authorization(configuration)
    request = authorization.request_for(configuration.targets[0], configuration)
    source = _response(request)
    payload = {name: getattr(source, name) for name in source.__dataclass_fields__}
    payload.pop("quota_before")
    payload.pop("quota_after")
    decoded = TheRundownCanonicalPayloadAdapterV1().decode_response(
        request,
        TheRundownNetworkHttpResponseV1(
            status_code=200,
            payload=payload,
            headers={
                "X-Datapoints": "55",
                "X-Datapoints-Used": "55",
                "X-Datapoints-Remaining": "19945",
                "X-Datapoints-Limit": "20000",
            },
            started_at=NOW - timedelta(seconds=1),
            finished_at=NOW,
        ),
    )
    assert decoded.datapoint_count == 55
    assert decoded.quota_cost_units == 55.0
    assert decoded.quota_before == 20000
    assert decoded.quota_after == 19945
    assert decoded.raw_metadata["provider_billing"] == {
        "x-datapoints": 55,
        "x-datapoints-used": 55,
        "x-datapoints-remaining": 19945,
        "x-datapoints-limit": 20000,
    }


def test_provider_billing_headers_missing_or_contradictory_fail_closed():
    configuration = _configuration(enabled=True)
    authorization = _authorization(configuration)
    request = authorization.request_for(configuration.targets[0], configuration)
    source = _response(request)
    payload = {name: getattr(source, name) for name in source.__dataclass_fields__}
    adapter = TheRundownCanonicalPayloadAdapterV1()
    with pytest.raises(NetworkShadowExecutionBlocked, match="billing header"):
        adapter.decode_response(
            request,
            TheRundownNetworkHttpResponseV1(
                status_code=200,
                payload=payload,
                headers={},
                started_at=NOW - timedelta(seconds=1),
                finished_at=NOW,
            ),
        )
    with pytest.raises(NetworkShadowExecutionBlocked, match="do not reconcile"):
        adapter.decode_response(
            request,
            TheRundownNetworkHttpResponseV1(
                status_code=200,
                payload=payload,
                headers={
                    "X-Datapoints": "55",
                    "X-Datapoints-Used": "55",
                    "X-Datapoints-Remaining": "19944",
                    "X-Datapoints-Limit": "20000",
                },
                started_at=NOW - timedelta(seconds=1),
                finished_at=NOW,
            ),
        )


def test_actual_run006_payload_is_bridged_through_reviewed_adapter():
    body_path = Path(
        "/private/tmp/top5-laliga-nextdate-20260920-006.request-2.body.json"
    )
    if not body_path.exists():
        pytest.skip("local Run-006 raw artifact is not available")
    payload = json.loads(body_path.read_text())
    headers_path = body_path.with_name(
        "top5-laliga-nextdate-20260920-006.request-2.headers.json"
    )
    metadata_path = body_path.with_name(
        "top5-laliga-nextdate-20260920-006.request-2.meta.json"
    )
    if not headers_path.exists() or not metadata_path.exists():
        pytest.skip("local Run-006 response metadata is not available")
    headers = json.loads(headers_path.read_text())
    metadata = json.loads(metadata_path.read_text())
    kickoff = datetime.fromisoformat("2026-09-20T12:00:00+00:00")
    target = TheRundownCanaryTargetV1(
        provider=THERUNDOWN_PROVIDER_NAME,
        league="LL",
        fixture_key=make_fixture_key("LL", "Getafe", "Málaga", kickoff),
        provider_event_id="48e87c231045c73e2318f6b4d5327405",
        home_team="Getafe",
        away_team="Málaga",
        kickoff=kickoff,
    )
    targets = tuple(target if item.league == "LL" else item for item in _targets())
    configuration = _configuration(
        targets=targets,
        request_scope=tuple(
            TheRundownNetworkRequestScopeV1(
                fixture_key=item.fixture_key,
                request_identity=(
                    "therundown-ll:top5-laliga-real-capture-20260920-006:2026-09-20"
                    if item.league == "LL"
                    else f"request-{item.league}"
                ),
            )
            for item in targets
        ),
        adapter_version=THERUNDOWN_ADAPTER_VERSION,
        adapter_source_sha=(
            "67f67ff97cb072bfca03bae688acbf87074359be9af31a36d890483ecd4fe152"
        ),
        maximum_source_age_seconds=900,
        enabled=True,
    )
    authorization = _authorization(configuration, provider=THERUNDOWN_PROVIDER_NAME)
    request = authorization.request_for(target, configuration)
    decoded = TheRundownCanonicalPayloadAdapterV1(
        adapter_source_sha=configuration.adapter_source_sha,
        maximum_source_age_seconds=configuration.maximum_source_age_seconds,
        initial_quota=QuotaSnapshot(used=0, remaining=None),
    ).decode_response(
        request,
        TheRundownNetworkHttpResponseV1(
            status_code=200,
            payload=payload,
            headers=headers,
            started_at=datetime.fromisoformat(metadata["started_at"]),
            finished_at=datetime.fromisoformat(metadata["completed_at"]),
        ),
    )
    assert isinstance(payload.get("events"), list)
    assert "outcome" not in payload
    assert "datapoint_count" not in payload
    assert decoded.outcome is CanaryOutcome.SUCCESS
    assert decoded.provider == THERUNDOWN_PROVIDER_NAME
    assert decoded.datapoint_count == 55
    assert decoded.quota_cost_units == 55.0
    assert "rate_limit_remaining" not in decoded.__dataclass_fields__
    assert "rate_limit_reset_at" not in decoded.__dataclass_fields__
    assert decoded.raw_metadata["provider_billing"]["x-rate-limit"] == 1
    assert decoded.raw_response_digest == (
        "6df5aa61479bbeaa85f46b4a1f66c7c3a40d88736b9b7f08555f9eb0e0f276c4"
    )
    assert [
        digest_record(item) for item in decoded.raw_metadata["normalized_observations"]
    ] == [
        "6e31168f27a5da71a96f369bdb0aca7dcd93b4a103f29685f7f528ea50984355",
        "2b2bb6cc67a204929478c0957ac5af5b8ebcc2002c6e89faee71cc044de582a1",
        "68d0a0da7f2ddf25299b76c08c8d995b57d8cfe571089402215a666fc9f24017",
    ]


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
    assert any(
        "LL:provider returned HTTP 429" in failure for failure in result.failures
    )
    assert [request.target.league for request in transport.calls] == [
        "EPL",
        "BL1",
        "LL",
    ]
    assert [request.sequence for request in transport.calls] == [0] * 3


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
    authorization, quota_headroom = _quota_headroom(_authorization(configuration))
    result, transport, _pacing, _clock = _run_live_network_fixture(
        configuration,
        authorization,
        quota_headroom,
        lambda request: _response(
            request,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            network_execution=True,
        ),
    )

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
    kickoff = datetime(2026, 10, 10, 11, 30, tzinfo=UTC)
    frozen_epl_target = TheRundownCanaryTargetV1(
        provider=THERUNDOWN_PROVIDER_NAME,
        league="EPL",
        fixture_key=make_fixture_key("EPL", "Arsenal FC", "Leeds United", kickoff),
        provider_event_id="92f6ca88e98f7e51ea425f91bd316357",
        home_team="Arsenal FC",
        away_team="Leeds United",
        kickoff=kickoff,
    )
    targets = (frozen_epl_target, *_targets()[1:])
    participant_scope = tuple(
        TheRundownNetworkParticipantScopeV1(
            target.fixture_key,
            "3436" if target.league == "EPL" else f"home-id-{target.league}",
            "3444" if target.league == "EPL" else f"away-id-{target.league}",
        )
        for target in targets
    )
    configuration = _configuration(
        targets=targets,
        participant_scope=participant_scope,
        adapter_version=THERUNDOWN_ADAPTER_VERSION,
        enabled=True,
    )
    authorization = _authorization(configuration, provider=THERUNDOWN_PROVIDER_NAME)
    request = authorization.request_for(frozen_epl_target, configuration)
    request.validate(now=NOW)
    assert request.target.provider == THERUNDOWN_PROVIDER_NAME
    assert request.target.league == "EPL"
    assert request.request_identity == "request-EPL"
    assert request.sequence == 0
    assert request.market_type == MARKET_PREMATCH_1X2
    http_request = TheRundownCanonicalPayloadAdapterV1().build_request(
        request,
        endpoint="https://therundown.io/api/v2",
        api_key="injected-test-only",
    )
    assert http_request.endpoint == (
        "https://therundown.io/api/v2/events/92f6ca88e98f7e51ea425f91bd316357"
    )
    assert http_request.query == {
        "market_ids": "1",
        "affiliate_ids": "19",
        "main_line": "true",
        "hide_closed": "true",
    }
    assert http_request.headers["X-TheRundown-Key"] == "injected-test-only"
    assert "Authorization" not in http_request.headers
    assert not {
        "league",
        "fixture_key",
        "provider_event_id",
        "market",
        "pre_match",
    }.intersection(http_request.query)
    assert configuration.provider_affiliate_ids == QUOTA_PROOF_AFFILIATE_IDS
    assert configuration.as_payload()["provider_affiliate_ids"] == ["19"]
    assert authorization.as_payload()["provider_affiliate_ids"] == ["19"]

    observed_at = datetime(2026, 10, 10, 11, 29, tzinfo=UTC)
    payload = json.loads(
        Path("tests/fixtures/therundown/champions_league_events.json").read_text()
    )
    event = payload["events"][0]
    event.update(
        {
            "event_id": frozen_epl_target.provider_event_id,
            "sport_id": 11,
            "event_date": frozen_epl_target.kickoff.isoformat(),
            "score": {"event_status": "STATUS_SCHEDULED"},
            "teams": [
                {
                    "team_id": 3436,
                    "name": "Arsenal FC",
                    "is_away": False,
                    "is_home": True,
                },
                {
                    "team_id": 3444,
                    "name": "Leeds United",
                    "is_away": True,
                    "is_home": False,
                },
            ],
            "schedule": {
                "event_name": "Leeds United at Arsenal FC",
                "season_type": "Regular Season",
                "season_year": 2026,
                "league_name": "Premier League",
            },
        }
    )
    event["markets"][0]["participants"][0].update(
        {"id": 3436, "name": "Arsenal FC", "is_away": False, "is_home": True}
    )
    event["markets"][0]["participants"][2].update(
        {"id": 3444, "name": "Leeds United", "is_away": True, "is_home": False}
    )
    for participant in event["markets"][0]["participants"]:
        for line in participant["lines"]:
            for price in line["prices"].values():
                price["updated_at"] = (observed_at - timedelta(seconds=5)).isoformat()
    decoded = TheRundownCanonicalPayloadAdapterV1(
        adapter_version=THERUNDOWN_ADAPTER_VERSION,
        adapter_source_sha=ADAPTER_SHA,
        maximum_source_age_seconds=300,
    ).decode_response(
        request,
        TheRundownNetworkHttpResponseV1(
            status_code=200,
            payload=payload,
            headers={
                "X-Datapoints": "55",
                "X-Datapoints-Used": "485",
                "X-Datapoints-Remaining": "19515",
                "X-Datapoints-Limit": "20000",
                "X-Data-Delay-Seconds": "300",
            },
            started_at=observed_at - timedelta(seconds=1),
            finished_at=observed_at,
        ),
    )
    assert decoded.outcome is CanaryOutcome.SUCCESS
    assert decoded.provider_event_id == frozen_epl_target.provider_event_id
    assert decoded.home_participant_id == "3436"
    assert decoded.away_participant_id == "3444"


def test_curated_transport_failure_reason_is_preserved_without_secrets():
    configuration = _configuration(enabled=True)
    authorization = _authorization(configuration)
    transport = _UntrustedReplayTransport(
        lambda _request: (_ for _ in ()).throw(
            NetworkShadowExecutionBlocked("single-event events envelope is malformed")
        )
    )
    result = TheRundownNetworkShadowExecutorV1(
        clock=lambda: NOW,
        fail_closed_immediately=True,
    ).run(configuration, authorization, transport=transport)

    assert result.request_count == 1
    assert result.failures == (
        (
            "EPL:TRANSPORT_ERROR:NetworkShadowExecutionBlocked:"
            "single-event events envelope is malformed"
        ),
    )


def test_network_shadow_rejects_alternate_provider_affiliate_scope():
    configuration = _configuration(provider_affiliate_ids=("3",))
    with pytest.raises(NetworkShadowExecutionBlocked, match="affiliate scope"):
        configuration.validate()

"""Contract tests for the guarded, fake-transport controlled-shadow harness."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from src.football.provider_cascade.execution_harness import (
    AUTHORIZATION_SCHEMA_VERSION,
    CAPTURE_ATTESTATION_SCHEMA_VERSION,
    EXECUTION_BANNERS,
    ControlledShadowExecutionHarness,
    ControlledShadowRunAuthorizationV1,
    ExecutionStatus,
    FakeControlledShadowTransport,
    HarnessExecutionBlocked,
    ProviderTransportResponse,
)
from src.football.provider_cascade.preparation import (
    SUPPORTED_PREPARATION_PROVIDERS,
    preparation_from_input_payload,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 16, 15, tzinfo=UTC)
ORDER = SUPPORTED_PREPARATION_PROVIDERS


def _fixture() -> dict[str, object]:
    return {
        "fixture_key": "EPL:fixture-001",
        "league_code": "EPL",
        "home_team": "Home FC",
        "away_team": "Away FC",
        "kickoff": NOW.isoformat(),
    }


def _state(
    provider: str, *, identity: str = "RESOLVED", remaining: int | None = 10
) -> dict[str, object]:
    known = identity == "RESOLVED"
    return {
        "readiness_state": "LIVE_PATH_READY_FOR_OBSERVATION",
        "identity_state": identity,
        "fixture_id_known": known,
        "provider_fixture_id": f"{provider}-event" if known else None,
        "discovery_required": not known and identity == "DISCOVERY_REQUIRED",
        "quota": {
            "used": 500 if provider == "the_odds_api" and remaining == 0 else 0,
            "remaining": remaining,
        },
        "quota_cost_units_per_request": 1.0,
    }


def _prepare(
    *,
    order: tuple[str, ...] = ORDER,
    remaining: dict[str, int | None] | None = None,
    identities: dict[str, str] | None = None,
    maximum_requests: int | None = None,
    maximum_cost: float | None = None,
):
    remaining = remaining or {provider: 10 for provider in ORDER}
    identities = identities or {provider: "RESOLVED" for provider in ORDER}
    return preparation_from_input_payload(
        {
            "fixture": _fixture(),
            "timing_policy": {
                "maximum_odds_age_seconds": 900,
                "kickoff_tolerance_seconds": 60,
            },
            "provider_order": list(order),
            "credential_presence": {provider: True for provider in ORDER},
            "provider_readiness": {
                provider: _state(
                    provider,
                    identity=identities[provider],
                    remaining=remaining[provider],
                )
                for provider in ORDER
            },
            "maximum_total_network_requests": maximum_requests or len(order),
            "maximum_total_quota_cost_units": maximum_cost or float(len(order)),
        }
    )


def _auth(
    preparation, *, run_id: str = "run-1", authorization_id: str = "auth-1", **changes
):
    versions = {
        provider: f"{provider}:adapter-v1"
        for provider in preparation.configured_provider_order
    }
    source = {provider: "a" * 64 for provider in preparation.configured_provider_order}
    values = {
        "authorization_id": authorization_id,
        "controlled_shadow_run_id": run_id,
        "authorization_nonce": "nonce-1",
        "ceo_authorization_identity": "ceo:canonical",
        "preparation_id": preparation.preparation_id,
        "preparation_digest": preparation.preparation_digest,
        "qualification_session_id": "qualification-session-1",
        "fixture_key": preparation.fixture_identity["fixture_key"],
        "league": preparation.league,
        "home_team": preparation.fixture_identity["home_team"],
        "away_team": preparation.fixture_identity["away_team"],
        "kickoff": preparation.kickoff,
        "configured_provider_order": preparation.configured_provider_order,
        "timing_policy_digest": preparation.timing_policy_digest,
        "maximum_total_network_requests": preparation.maximum_total_network_requests,
        "maximum_total_quota_cost_units": preparation.maximum_total_quota_cost_units,
        "per_provider_maximums": preparation.per_provider_maximums,
        "adapter_version_scope": versions,
        "adapter_source_sha_scope": source,
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=10),
        "zero_monetary_spend": True,
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        **changes,
    }
    return ControlledShadowRunAuthorizationV1(**values)


def _success(
    provider: str, *, event: str | None = None, request: str | None = None, **changes
):
    return ProviderTransportResponse(
        outcome="SUCCESS",
        provider_event_id=event or f"{provider}-event-1",
        provider_request_id=request or f"{provider}-request-1",
        provider_record_id=f"{provider}-record-1",
        home_odds=2.2,
        draw_odds=3.3,
        away_odds=3.1,
        bookmaker_identity=f"{provider}-bookmaker",
        source_identity=provider,
        source_timestamp=NOW - timedelta(seconds=5),
        adapter_version=f"{provider}:adapter-v1",
        adapter_source_sha="a" * 64,
        evidence_kind="MOCK",
        runner_mapping=(
            {"home": "1", "draw": "2", "away": "3"}
            if provider == "betfair_delayed"
            else {}
        ),
        app_session_prerequisites=True if provider == "betfair_delayed" else None,
        **changes,
    )


def test_authorization_is_external_and_exactly_versioned() -> None:
    preparation = _prepare(order=("the_odds_api",))
    authorization = _auth(preparation)
    authorization.validate(preparation, now=NOW)
    assert authorization.schema_version == AUTHORIZATION_SCHEMA_VERSION
    assert not hasattr(ControlledShadowRunAuthorizationV1, "issue")
    assert not hasattr(ControlledShadowRunAuthorizationV1, "create")
    assert (
        authorization.as_payload()["authorization_digest"]
        == authorization.authorization_digest
    )


def test_preparation_mismatch_is_rejected_before_transport() -> None:
    preparation = _prepare(order=("the_odds_api",))
    authorization = _auth(preparation, preparation_digest="b" * 64)
    transport = FakeControlledShadowTransport()
    with pytest.raises(HarnessExecutionBlocked, match="preparation digest"):
        ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
            preparation, authorization, transport=transport
        )
    assert transport.calls == []


def test_expired_authorization_fails_closed_before_transport() -> None:
    preparation = _prepare(order=("the_odds_api",))
    authorization = _auth(
        preparation,
        issued_at=NOW - timedelta(minutes=10),
        expires_at=NOW - timedelta(seconds=1),
    )
    transport = FakeControlledShadowTransport()
    with pytest.raises(HarnessExecutionBlocked, match="expired"):
        ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
            preparation, authorization, transport=transport
        )
    assert not transport.calls


def test_success_is_sequential_candidate_only_and_attested() -> None:
    preparation = _prepare(order=ORDER)
    authorization = _auth(preparation)
    transport = FakeControlledShadowTransport(
        {("the_odds_api", "ODDS"): _success("the_odds_api")}
    )
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation, authorization, transport=transport
    )
    assert result.status is ExecutionStatus.OBSERVED
    assert result.selected_provider == "the_odds_api"
    assert result.attempted_providers == ("the_odds_api",)
    assert result.network_request_count == 1
    assert result.observation is not None
    assert result.observation.evidence_kind == "MOCK"
    assert result.cascade_evidence.prediction_input_allowed is False
    assert result.attestation.schema_version == CAPTURE_ATTESTATION_SCHEMA_VERSION
    assert (
        result.attestation.capture_digest == result.attestation.computed_capture_digest
    )
    result.attestation.validate()
    assert result.attestation.as_payload()["capture_digest"]
    assert result.banners == EXECUTION_BANNERS


def test_failed_first_provider_falls_back_in_configured_order() -> None:
    preparation = _prepare(order=ORDER)
    authorization = _auth(preparation)
    transport = FakeControlledShadowTransport(
        {
            ("the_odds_api", "ODDS"): ProviderTransportResponse(
                outcome="RATE_LIMITED", failure_detail="rate limited"
            ),
            ("odds_api_io", "ODDS"): _success("odds_api_io"),
        }
    )
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation, authorization, transport=transport
    )
    assert result.selected_provider == "odds_api_io"
    assert [request.provider for request in transport.calls] == [
        "the_odds_api",
        "odds_api_io",
    ]
    assert result.network_request_count == 2
    assert result.attestation.attempted_providers == ("the_odds_api", "odds_api_io")


def test_odds_api_500_remaining_zero_makes_zero_calls_then_falls_back() -> None:
    preparation = _prepare(
        order=ORDER,
        remaining={"the_odds_api": 0, **{provider: 10 for provider in ORDER[1:]}},
        maximum_requests=4,
        maximum_cost=4.0,
    )
    authorization = _auth(preparation)
    transport = FakeControlledShadowTransport(
        {("odds_api_io", "ODDS"): _success("odds_api_io")}
    )
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation, authorization, transport=transport
    )
    assert result.selected_provider == "odds_api_io"
    assert [request.provider for request in transport.calls] == ["odds_api_io"]
    assert result.network_request_count == 1
    assert any("remaining=0" in item for item in result.failure_evidence)
    assert any("QUOTA_EXHAUSTED" in item for item in result.failure_evidence)


def test_discovery_and_odds_are_separately_counted() -> None:
    provider = "odds_api_io"
    preparation = _prepare(
        order=(provider,),
        identities={
            provider: "DISCOVERY_REQUIRED",
            **{item: "RESOLVED" for item in ORDER if item != provider},
        },
        maximum_requests=2,
        maximum_cost=2.0,
    )
    authorization = _auth(preparation)
    transport = FakeControlledShadowTransport(
        {
            (provider, "FIXTURE_DISCOVERY"): ProviderTransportResponse(
                outcome="SUCCESS", identity_state="UNIQUE", provider_event_id="event-1"
            ),
            (provider, "ODDS"): _success(provider),
        }
    )
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation, authorization, transport=transport
    )
    assert result.discovery_request_count == 1
    assert result.odds_request_count == 1
    assert result.network_request_count == 2
    assert [request.action.value for request in transport.calls] == [
        "FIXTURE_DISCOVERY",
        "ODDS",
    ]


def test_ambiguous_discovery_stops_before_odds() -> None:
    provider = "api_football"
    preparation = _prepare(
        order=(provider,),
        identities={
            provider: "DISCOVERY_REQUIRED",
            **{item: "RESOLVED" for item in ORDER if item != provider},
        },
        maximum_requests=2,
        maximum_cost=2.0,
    )
    authorization = _auth(preparation)
    transport = FakeControlledShadowTransport(
        {
            (provider, "FIXTURE_DISCOVERY"): ProviderTransportResponse(
                outcome="SUCCESS", identity_state="AMBIGUOUS"
            )
        }
    )
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation, authorization, transport=transport
    )
    assert result.status is ExecutionStatus.NO_OBSERVATION
    assert result.observation is None
    assert [request.action.value for request in transport.calls] == [
        "FIXTURE_DISCOVERY"
    ]
    assert result.odds_request_count == 0


def test_network_capable_transport_is_rejected_without_calling_it() -> None:
    preparation = _prepare(order=("the_odds_api",))
    authorization = _auth(preparation)

    class NetworkTransport:
        transport_capability = "NETWORK_CAPABLE"
        calls = 0

        def execute(self, request):
            self.calls += 1
            raise AssertionError("network transport must never be called")

    transport = NetworkTransport()
    with pytest.raises(HarnessExecutionBlocked, match="injected test transport"):
        ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
            preparation, authorization, transport=transport
        )
    assert transport.calls == 0


def test_completed_replay_does_not_call_fake_transport_twice() -> None:
    preparation = _prepare(order=("the_odds_api",))
    authorization = _auth(preparation)
    transport = FakeControlledShadowTransport(
        {("the_odds_api", "ODDS"): _success("the_odds_api")}
    )
    harness = ControlledShadowExecutionHarness(clock=lambda: NOW)
    first = harness.execute(preparation, authorization, transport=transport)
    second = harness.execute(preparation, authorization, transport=transport)
    assert first.status is ExecutionStatus.OBSERVED
    assert second.status is ExecutionStatus.REPLAYED
    assert transport.network_call_count == 1
    assert second.attestation.capture_digest == first.attestation.capture_digest


def test_conflicting_replay_fails_closed() -> None:
    preparation = _prepare(order=("the_odds_api",))
    first_auth = _auth(preparation)
    conflicting_auth = _auth(preparation, authorization_id="auth-2")
    transport = FakeControlledShadowTransport(
        {("the_odds_api", "ODDS"): _success("the_odds_api")}
    )
    harness = ControlledShadowExecutionHarness(clock=lambda: NOW)
    harness.execute(preparation, first_auth, transport=transport)
    with pytest.raises(HarnessExecutionBlocked, match="conflicting replay"):
        harness.execute(preparation, conflicting_auth, transport=transport)
    assert transport.network_call_count == 1


def test_all_failures_emit_no_observation_and_one_attestation_per_attempt() -> None:
    preparation = _prepare(order=ORDER)
    authorization = _auth(preparation)
    transport = FakeControlledShadowTransport(
        default=ProviderTransportResponse(outcome="PROVIDER_UNAVAILABLE")
    )
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation, authorization, transport=transport
    )
    assert result.status is ExecutionStatus.NO_OBSERVATION
    assert result.selected_provider is None
    assert result.observation is None
    assert len(result.attestations) == len(ORDER)
    assert any(item.startswith("NO_OBSERVATION:") for item in result.failure_evidence)


def test_api_football_pagination_and_body_errors_fail_closed() -> None:
    provider = "api_football"
    preparation = _prepare(order=(provider,), maximum_requests=1, maximum_cost=1.0)
    authorization = _auth(preparation)
    transport = FakeControlledShadowTransport(
        {
            (provider, "ODDS"): _success(
                provider, pagination_total=2, body_error_taxonomy=("errors",)
            )
        }
    )
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation, authorization, transport=transport
    )
    assert result.status is ExecutionStatus.NO_OBSERVATION
    assert any("PAGINATION_RISK" in item for item in result.failure_evidence)


def test_betfair_success_preserves_delayed_semantics() -> None:
    provider = "betfair_delayed"
    preparation = _prepare(order=(provider,), maximum_requests=1, maximum_cost=1.0)
    authorization = _auth(preparation)
    transport = FakeControlledShadowTransport(
        {
            (
                provider,
                "ODDS",
            ): _success(provider, delayed_observation=True, delay_seconds=30)
        }
    )
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation, authorization, transport=transport
    )
    assert result.observation is not None
    assert result.observation.delayed_observation is True
    assert result.normalized_observation is not None
    assert result.normalized_observation.delayed is True


def test_harness_does_not_import_or_issue_builder2_authority() -> None:
    import src.football.provider_cascade.execution_harness as module

    source = inspect.getsource(module)
    assert "Builder2QualificationReceiptV1" not in source
    assert "issue_builder2_qualification_receipt" not in source
    assert "prediction" not in source.lower() or "prediction_input_allowed" in source


def test_fake_acceptance_never_needs_socket(monkeypatch) -> None:
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("socket use is forbidden in harness tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    preparation = _prepare(order=("the_odds_api",))
    authorization = _auth(preparation)
    result = ControlledShadowExecutionHarness(clock=lambda: NOW).execute(
        preparation,
        authorization,
        transport=FakeControlledShadowTransport(
            {("the_odds_api", "ODDS"): _success("the_odds_api")}
        ),
    )
    assert result.observation is not None

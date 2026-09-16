"""Separately reviewed network-capable transport bridge.

This module adapts one of the existing provider adapters to the controlled
shadow harness.  It has no authority-issuing methods and requires both the
caller-supplied run authorization already required by the harness and the
existing network authorization contract.  Tests inject the adapter's HTTP
transport; this module never creates a default HTTP client.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from src.football.production_contracts import Fixture, _utc
from src.football.provider_cascade.adapters import AdapterResult, OddsProviderAdapter
from src.football.provider_cascade.contracts import (
    CascadeTimingPolicy,
    NetworkAuthorizationContract,
    ProviderConfig,
    ProviderIdentityResolution,
    ProviderState,
    TimingProvenance,
    TransportCapability,
)
from src.football.provider_cascade.execution_harness import (
    ControlledShadowRunAuthorizationV1,
    HarnessContractError,
    HarnessExecutionBlocked,
    NetworkCapableProviderTransport,
    ProviderAction,
    ProviderTransportRequest,
    ProviderTransportResponse,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ObservationEvidenceKind,
    ProviderTimestampProvenance,
)
from src.football.top5_provider_cascade_validation import CascadeOutcome

_SOURCE_TIMESTAMP_PROVENANCE = {
    "the_odds_api": ProviderTimestampProvenance.BOOKMAKER_UPDATE_TIMESTAMP,
    "odds_api_io": ProviderTimestampProvenance.PROVIDER_SOURCE_TIMESTAMP,
}


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) < 40 or len(value) > 64:
        raise HarnessContractError(f"{name} must be a hexadecimal digest")
    normalized = value.lower()
    if any(char not in "0123456789abcdef" for char in normalized):
        raise HarnessContractError(f"{name} must be a hexadecimal digest")
    return normalized


def _failure_outcome(state: ProviderState) -> CascadeOutcome:
    return {
        ProviderState.QUOTA_EXHAUSTED: CascadeOutcome.QUOTA_EXHAUSTED,
        ProviderState.RATE_LIMITED: CascadeOutcome.RATE_LIMITED,
        ProviderState.AUTH_FAILED: CascadeOutcome.AUTH_FAILED,
        ProviderState.TEMPORARILY_UNAVAILABLE: CascadeOutcome.PROVIDER_UNAVAILABLE,
        ProviderState.UNSUPPORTED_FIXTURE: CascadeOutcome.UNSUPPORTED_FIXTURE,
        ProviderState.UNSUPPORTED_LEAGUE: CascadeOutcome.UNSUPPORTED_LEAGUE,
        ProviderState.UNSUPPORTED_MARKET: CascadeOutcome.UNSUPPORTED_MARKET,
        ProviderState.STALE: CascadeOutcome.STALE,
        ProviderState.MALFORMED: CascadeOutcome.MALFORMED,
        ProviderState.PARTIAL: CascadeOutcome.PARTIAL,
        ProviderState.QUALITY_REJECTED: CascadeOutcome.QUALITY_REJECTED,
        ProviderState.CONFIG_DISABLED: CascadeOutcome.CONFIG_DISABLED,
        ProviderState.CREDENTIAL_MISSING: CascadeOutcome.CREDENTIAL_MISSING,
        ProviderState.CANDIDATE_ONLY: CascadeOutcome.PROVIDER_UNAVAILABLE,
        ProviderState.HEALTH_UNKNOWN: CascadeOutcome.PROVIDER_UNAVAILABLE,
        ProviderState.DISCOVERY_REQUIRED: CascadeOutcome.UNSUPPORTED_FIXTURE,
        ProviderState.IDENTITY_UNRESOLVED: CascadeOutcome.UNSUPPORTED_FIXTURE,
        ProviderState.IDENTITY_AMBIGUOUS: CascadeOutcome.UNSUPPORTED_FIXTURE,
    }.get(state, CascadeOutcome.MALFORMED)


class ConfiguredNetworkProviderTransport(NetworkCapableProviderTransport):
    """One explicit existing adapter behind the network-capable seam.

    The bridge intentionally supports a pre-resolved, single odds request.
    Discovery is a separate contract and is rejected until an adapter-specific
    discovery evidence path is reviewed.  Current adapters that can supply a
    canonical source timestamp are the only providers accepted here.
    """

    def __init__(
        self,
        *,
        provider: str,
        adapter: OddsProviderAdapter,
        provider_config: ProviderConfig,
        network_authorization: NetworkAuthorizationContract,
        adapter_source_sha: str,
        provider_fixture_id: str,
        timing_policy: CascadeTimingPolicy,
        identity_resolution: ProviderIdentityResolution | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider
        self.adapter = adapter
        self.provider_config = provider_config
        self.network_authorization = network_authorization
        self.adapter_source_sha = _sha(adapter_source_sha, "adapter_source_sha")
        self.provider_fixture_id = provider_fixture_id.strip()
        self.timing_policy = timing_policy
        self.identity_resolution = identity_resolution
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if not self.provider_fixture_id:
            raise HarnessContractError("provider_fixture_id is required")
        self._validate_static_configuration()

    def _validate_static_configuration(self) -> None:
        if not self.provider.strip():
            raise HarnessContractError("network bridge provider is required")
        self.provider_config.validate()
        self.timing_policy.validate()
        self.network_authorization.validate()
        if self.provider_config.name != self.provider:
            raise HarnessContractError("provider config does not match bridge provider")
        if getattr(self.adapter, "name", None) != self.provider:
            raise HarnessContractError(
                "adapter identity does not match bridge provider"
            )
        if (
            getattr(self.adapter, "transport_capability", None)
            is not TransportCapability.NETWORK_CAPABLE
        ):
            raise HarnessExecutionBlocked(
                "a TEST_INJECTED adapter cannot cross the network bridge"
            )
        if self.provider not in _SOURCE_TIMESTAMP_PROVENANCE:
            raise HarnessExecutionBlocked(
                f"{self.provider}: existing adapter does not provide canonical source-time provenance"
            )

    def validate_for_run(
        self,
        preparation: Any,
        authorization: ControlledShadowRunAuthorizationV1,
    ) -> None:
        """Validate caller-supplied exact run and provider scopes only."""

        self._validate_static_configuration()
        if (
            self.network_authorization.controlled_shadow_run_ref
            != authorization.controlled_shadow_run_id
        ):
            raise HarnessExecutionBlocked(
                "network authorization is not bound to the exact controlled-shadow run"
            )
        if not self.network_authorization.permits(self.provider):
            raise HarnessExecutionBlocked(
                f"network authorization does not permit {self.provider}"
            )
        if self.provider not in authorization.configured_provider_order:
            raise HarnessExecutionBlocked(
                "network bridge provider is outside the exact authorization order"
            )
        if (
            authorization.adapter_version_scope[self.provider]
            != self.provider_config.adapter_version
        ):
            raise HarnessExecutionBlocked(
                f"{self.provider}: adapter version is outside the exact authorization scope"
            )
        if (
            authorization.adapter_source_sha_scope[self.provider].lower()
            != self.adapter_source_sha
        ):
            raise HarnessExecutionBlocked(
                f"{self.provider}: adapter source SHA is outside the exact authorization scope"
            )
        manifests = {item.provider: item for item in preparation.providers}
        manifest = manifests.get(self.provider)
        if manifest is None:
            raise HarnessExecutionBlocked(
                f"{self.provider}: provider is absent from preparation"
            )
        if manifest.provider_fixture_id != self.provider_fixture_id:
            raise HarnessExecutionBlocked(
                f"{self.provider}: provider fixture identity does not match preparation"
            )
        if manifest.provider_fixture_id_known is not True:
            raise HarnessExecutionBlocked(
                f"{self.provider}: resolved provider fixture identity is required"
            )
        if manifest.fixture_discovery_required is True:
            raise HarnessExecutionBlocked(
                f"{self.provider}: discovery evidence is required before the odds call"
            )

    def execute(self, request: ProviderTransportRequest) -> ProviderTransportResponse:
        request.validate()
        if request.provider != self.provider:
            return self._failure(
                request,
                CascadeOutcome.MALFORMED,
                "bridge request provider does not match configured adapter",
            )
        if (
            request.controlled_shadow_run_id
            != self.network_authorization.controlled_shadow_run_ref
        ):
            return self._failure(
                request,
                CascadeOutcome.MALFORMED,
                "bridge request is outside the exact network authorization run",
            )
        if ProviderAction(request.action) is not ProviderAction.ODDS:
            return self._failure(
                request,
                CascadeOutcome.UNSUPPORTED_FIXTURE,
                "fixture discovery is not implemented by this bridge",
            )
        try:
            fixture = Fixture(
                request.fixture_key,
                request.league,
                request.home_team,
                request.away_team,
                request.kickoff,
            )
            fixture.validate()
            result = self.adapter.fetch(
                fixture,
                self.provider_config,
                request_identity=request.request_identity,
                requested_at=_utc(self.clock(), "bridge requested_at"),
                provider_priority=0,
                provider_fixture_id=self.provider_fixture_id,
                timing_policy=self.timing_policy,
                identity_resolution=self.identity_resolution,
                authorization=self.network_authorization,
            )
            result.validate()
        except Exception as exc:  # noqa: BLE001 - adapter boundary fails closed
            return self._failure(
                request,
                CascadeOutcome.MALFORMED,
                f"{self.provider}: adapter response malformed ({type(exc).__name__})",
            )
        return self._response_from_adapter_result(request, result)

    def _failure(
        self,
        request: ProviderTransportRequest,
        outcome: CascadeOutcome,
        detail: str,
    ) -> ProviderTransportResponse:
        return ProviderTransportResponse(
            outcome=outcome,
            provider_request_id=request.request_identity,
            failure_detail=detail,
            evidence_kind=ObservationEvidenceKind.MOCK,
        )

    def _response_from_adapter_result(
        self,
        request: ProviderTransportRequest,
        result: AdapterResult,
    ) -> ProviderTransportResponse:
        if result.state is not ProviderState.AVAILABLE or result.observation is None:
            return self._failure(
                request,
                _failure_outcome(result.state),
                f"{self.provider}: {result.reason}",
            )
        observation = result.observation
        if observation.provider_identity != self.provider:
            return self._failure(
                request,
                CascadeOutcome.MALFORMED,
                f"{self.provider}: normalized provider identity mismatch",
            )
        if observation.request_identity != request.request_identity:
            return self._failure(
                request,
                CascadeOutcome.MALFORMED,
                f"{self.provider}: normalized request identity mismatch",
            )
        if observation.source_timestamp is None:
            return self._failure(
                request,
                CascadeOutcome.STALE,
                f"{self.provider}: adapter exposes capture-time-only evidence; canonical source timestamp is missing",
            )
        if (
            observation.source_timing_provenance
            is not TimingProvenance.SOURCE_TIMESTAMP
        ):
            return self._failure(
                request,
                CascadeOutcome.STALE,
                f"{self.provider}: adapter source timestamp provenance is not canonical",
            )
        try:
            raw_digest = _sha(result.raw_response_digest, "raw_response_digest")
            normalized_digest = _sha(
                result.normalized_record_digest, "normalized_record_digest"
            )
        except HarnessContractError as exc:
            return self._failure(request, CascadeOutcome.MALFORMED, str(exc))
        provider_event_id = observation.provider_fixture_id.strip()
        if not provider_event_id:
            return self._failure(
                request,
                CascadeOutcome.MALFORMED,
                f"{self.provider}: provider event identity is missing",
            )
        if (
            not observation.bookmaker_identity.strip()
            or not observation.source_provenance.strip()
        ):
            return self._failure(
                request,
                CascadeOutcome.MALFORMED,
                f"{self.provider}: bookmaker or source provenance is missing",
            )
        provider_timestamp_provenance = _SOURCE_TIMESTAMP_PROVENANCE[self.provider]
        if not isfinite(float(observation.latency_ms)) or observation.latency_ms < 0:
            return self._failure(
                request,
                CascadeOutcome.MALFORMED,
                f"{self.provider}: response latency is invalid",
            )
        return ProviderTransportResponse(
            outcome=CascadeOutcome.SUCCESS,
            provider_event_id=provider_event_id,
            provider_request_id=request.request_identity,
            provider_record_id=provider_event_id,
            identity_state="RESOLVED",
            home_odds=observation.home_odds,
            draw_odds=observation.draw_odds,
            away_odds=observation.away_odds,
            bookmaker_identity=observation.bookmaker_identity,
            source_identity=observation.source_provenance,
            source_timestamp=observation.source_timestamp,
            adapter_version=observation.adapter_version,
            adapter_source_sha=self.adapter_source_sha,
            raw_response_digest=raw_digest,
            normalized_record_digest=normalized_digest,
            source_timing_provenance=observation.source_timing_provenance.value,
            provider_timestamp_provenance=provider_timestamp_provenance,
            delayed_observation=observation.delayed,
            delay_seconds=observation.delay_seconds,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            request_started_at=observation.request_started_at,
            request_finished_at=observation.request_completed_at,
            raw_metadata={
                "adapter_reason": result.reason,
                "source_provenance": observation.source_provenance,
            },
        )


__all__ = ["ConfiguredNetworkProviderTransport"]

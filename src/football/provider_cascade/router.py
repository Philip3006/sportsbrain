"""Deterministic, sequential, fail-closed provider cascade router."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.football.production_contracts import Fixture, ProductionContractError, _utc
from src.football.provider_cascade.adapters import (
    AdapterResult,
    ApiFootballAdapter,
    BetfairDelayedAdapter,
    OddsApiIoAdapter,
    OddsProviderAdapter,
    TheOddsAPIAdapter,
)
from src.football.provider_cascade.budget import RequestBudgetManager
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    TOP5_LEAGUE_CODES,
    CascadeDecisionTrace,
    CascadeResult,
    CascadeTimingPolicy,
    ProviderAttemptTrace,
    ProviderCascadeConfig,
    ProviderIdentityResolution,
    ProviderIdentityResolutionState,
    ProviderState,
    QuotaSnapshot,
    TimingProvenance,
    TransportCapability,
    digest_record,
    stable_request_identity,
)
from src.football.provider_cascade.health import ProviderHealthRegistry

DEFAULT_ADAPTERS: Mapping[str, OddsProviderAdapter] = {
    "the_odds_api": TheOddsAPIAdapter(),
    "odds_api_io": OddsApiIoAdapter(),
    "api_football": ApiFootballAdapter(),
    "betfair_delayed": BetfairDelayedAdapter(),
}

IDENTITY_REQUIRED_PROVIDERS = frozenset(
    {"odds_api_io", "api_football", "betfair_delayed"}
)


class ProviderCascadeRouter:
    """Route one fixture through a configurable provider order.

    The router never retries a provider, never fans out, and never produces a
    model prediction.  A successful candidate remains explicitly marked
    ``candidate_only`` until an independent validation boundary accepts it.
    """

    def __init__(
        self,
        config: ProviderCascadeConfig | None = None,
        *,
        adapters: Mapping[str, OddsProviderAdapter] | None = None,
        budget: RequestBudgetManager | None = None,
        now: datetime | None = None,
        timing_policy: CascadeTimingPolicy | None = None,
    ) -> None:
        self.config = config or ProviderCascadeConfig.default()
        self.config.validate()
        if timing_policy is None:
            raise ProductionContractError(
                "explicit experiment timing policy is required; no production timing default is approved"
            )
        timing_policy.validate()
        self.timing_policy = timing_policy
        self.adapters = dict(DEFAULT_ADAPTERS if adapters is None else adapters)
        missing = sorted(set(self.config.provider_order) - set(self.adapters))
        if missing:
            raise ProductionContractError(
                f"missing provider adapters: {', '.join(missing)}"
            )
        self.budget = budget or RequestBudgetManager(self.config, now=now)
        credentials = {
            name: self.budget.credential_available(provider)
            for name, provider in self.config.providers.items()
        }
        self.health = ProviderHealthRegistry(self.config.providers, credentials)

    def route(
        self,
        fixture: Fixture,
        *,
        now: datetime | None = None,
        provider_fixture_ids: Mapping[str, str] | None = None,
        identity_resolutions: Mapping[str, ProviderIdentityResolution] | None = None,
    ) -> CascadeResult:
        fixture.validate()
        if fixture.league_code not in TOP5_LEAGUE_CODES:
            return self._fail_closed(fixture, (), "unsupported_top5_league")
        current = _utc(now or datetime.now(timezone.utc), "route now")
        self._route_time = current
        ids = provider_fixture_ids or {}
        resolutions = identity_resolutions or {}
        attempts: list[ProviderAttemptTrace] = []
        total_latency = 0
        authorization = self.config.network_authorization()
        for attempt_index, provider_name in enumerate(self.config.provider_order):
            config = self.config.providers[provider_name]
            request_identity = stable_request_identity(provider_name, fixture, current)
            resolution = self._resolve_identity(
                fixture,
                provider_name,
                current=current,
                provider_fixture_id=ids.get(provider_name),
                supplied=resolutions.get(provider_name),
                kickoff_tolerance_seconds=self.timing_policy.kickoff_tolerance_seconds,
            )
            if (
                fixture.league_code not in config.league_allowlist
                and config.league_allowlist
            ):
                attempts.append(
                    self._trace(
                        attempt_index,
                        provider_name,
                        ProviderState.UNSUPPORTED_LEAGUE,
                        "league_not_allowlisted",
                        request_identity,
                        fixture=fixture,
                        configured_provider_order=self.config.provider_order,
                        credentials_present=self.budget.credential_available(config),
                    )
                )
                continue
            if MARKET_PREMATCH_1X2 not in config.market_allowlist:
                attempts.append(
                    self._trace(
                        attempt_index,
                        provider_name,
                        ProviderState.UNSUPPORTED_MARKET,
                        "market_not_allowlisted",
                        request_identity,
                        fixture=fixture,
                        configured_provider_order=self.config.provider_order,
                        credentials_present=self.budget.credential_available(config),
                    )
                )
                continue
            if not config.enabled:
                attempts.append(
                    self._trace(
                        attempt_index,
                        provider_name,
                        ProviderState.CONFIG_DISABLED,
                        "config_disabled",
                        request_identity,
                        fixture=fixture,
                        configured_provider_order=self.config.provider_order,
                        credentials_present=self.budget.credential_available(config),
                    )
                )
                continue
            if config.candidate_only and not self.config.allow_candidate_only:
                attempts.append(
                    self._trace(
                        attempt_index,
                        provider_name,
                        ProviderState.CANDIDATE_ONLY,
                        "candidate_only_not_eligible",
                        request_identity,
                        fixture=fixture,
                        configured_provider_order=self.config.provider_order,
                        credentials_present=self.budget.credential_available(config),
                    )
                )
                continue
            if (
                resolution is not None
                and ProviderIdentityResolutionState(resolution.state)
                is not ProviderIdentityResolutionState.RESOLVED
            ):
                state = {
                    ProviderIdentityResolutionState.DISCOVERY_REQUIRED: ProviderState.DISCOVERY_REQUIRED,
                    ProviderIdentityResolutionState.UNRESOLVED: ProviderState.IDENTITY_UNRESOLVED,
                    ProviderIdentityResolutionState.AMBIGUOUS: ProviderState.IDENTITY_AMBIGUOUS,
                }[ProviderIdentityResolutionState(resolution.state)]
                attempts.append(
                    self._trace(
                        attempt_index,
                        provider_name,
                        state,
                        f"identity_resolution:{ProviderIdentityResolutionState(resolution.state).value}",
                        request_identity,
                        fixture=fixture,
                        configured_provider_order=self.config.provider_order,
                        credentials_present=self.budget.credential_available(config),
                        resolution=resolution,
                    )
                )
                continue
            self.health.preflight(provider_name, current)
            preflight = self.budget.preflight(provider_name, now=current)
            if not preflight.allowed:
                self.health.result(
                    provider_name,
                    state=preflight.state,
                    at=current,
                    quota=preflight.quota_before,
                    rate_limit=preflight.quota_before,
                )
                attempts.append(
                    self._trace(
                        attempt_index,
                        provider_name,
                        preflight.state,
                        f"preflight_rejected:{preflight.reason}",
                        request_identity,
                        fixture=fixture,
                        configured_provider_order=self.config.provider_order,
                        preflight_allowed=False,
                        preflight_reason=preflight.reason,
                        budget_decision="REJECTED",
                        quota_before=preflight.quota_before,
                        credentials_present=self.budget.credential_available(config),
                        resolution=resolution,
                    )
                )
                continue
            adapter = self.adapters[provider_name]
            capability = getattr(
                adapter, "transport_capability", TransportCapability.TEST_INJECTED
            )
            if (
                TransportCapability(capability) is TransportCapability.NETWORK_CAPABLE
                and (authorization is None or not authorization.permits(provider_name))
            ):
                self.health.result(
                    provider_name,
                    state=ProviderState.HEALTH_UNKNOWN,
                    at=current,
                    quota=preflight.quota_before,
                    rate_limit=preflight.quota_before,
                )
                attempts.append(
                    self._trace(
                        attempt_index,
                        provider_name,
                        ProviderState.HEALTH_UNKNOWN,
                        "live_calls_not_authorized",
                        request_identity,
                        fixture=fixture,
                        configured_provider_order=self.config.provider_order,
                        preflight_allowed=True,
                        preflight_reason=preflight.reason,
                        budget_decision="ALLOWED",
                        quota_before=preflight.quota_before,
                        credentials_present=self.budget.credential_available(config),
                        resolution=resolution,
                    )
                )
                continue
            try:
                result = adapter.fetch(
                    fixture,
                    config,
                    request_identity=request_identity,
                    requested_at=current,
                    provider_priority=attempt_index,
                    provider_fixture_id=(
                        resolution.provider_id
                        if resolution is not None
                        else ids.get(provider_name)
                    ),
                    timing_policy=self.timing_policy,
                    identity_resolution=resolution,
                    authorization=authorization,
                )
                result.validate()
            except Exception:  # noqa: BLE001 - adapter failures must fail closed.
                result = AdapterResult(
                    ProviderState.MALFORMED,
                    "adapter_exception",
                    # An adapter exception does not prove that its transport
                    # was not reached; count the invocation conservatively.
                    network_called=True,
                )
            effective_state = result.state
            effective_reason = result.reason
            if result.state is ProviderState.AVAILABLE:
                if result.observation is None:
                    effective_state = ProviderState.MALFORMED
                    effective_reason = "missing_normalized_observation"
                elif result.observation.provider_identity != provider_name:
                    effective_state = ProviderState.QUALITY_REJECTED
                    effective_reason = "provider_identity_mismatch"
                elif not self._fresh_enough(result.observation, current):
                    effective_state = ProviderState.STALE
                    effective_reason = "stale_observation"
            if result.network_called:
                self.budget.record_attempt(
                    provider_name,
                    request_count=1,
                    quota_cost_units=float(preflight.request_cost),
                    at=current,
                )
                self.health.attempt(provider_name, current)
            quota_after = self._effective_quota(provider_name, result)
            if result.network_called:
                self.budget.record_result(
                    provider_name,
                    state=effective_state,
                    at=current,
                    quota_after=quota_after,
                )
            self.health.result(
                provider_name,
                state=effective_state,
                at=current,
                quota=quota_after,
                rate_limit=result.rate_limit_state,
            )
            total_latency += result.latency_ms
            if (
                effective_state is ProviderState.AVAILABLE
                and result.observation is not None
            ):
                observation = replace(
                    result.observation,
                    fallback_depth=attempt_index,
                    quota_state_before=preflight.quota_before,
                    quota_state_after=quota_after,
                    rate_limit_state=result.rate_limit_state,
                )
                attempts.append(
                    self._trace(
                        attempt_index,
                        provider_name,
                        ProviderState.AVAILABLE,
                        "success_candidate_only"
                        if observation.candidate_only
                        else "success",
                        request_identity,
                        result.status_code,
                        result.latency_ms,
                        result.network_called,
                        fixture=fixture,
                        configured_provider_order=self.config.provider_order,
                        fallback_depth=attempt_index,
                        preflight_allowed=True,
                        preflight_reason=preflight.reason,
                        budget_decision="ALLOWED",
                        quota_before=preflight.quota_before,
                        quota_after=quota_after,
                        network_request_count=int(result.network_called),
                        quota_cost_units=(
                            float(preflight.request_cost)
                            if result.network_called
                            else 0.0
                        ),
                        credentials_present=self.budget.credential_available(config),
                        observation=observation,
                        resolution=resolution,
                        request_started_at=current,
                        request_completed_at=current
                        + timedelta(milliseconds=result.latency_ms),
                    )
                )
                trace = CascadeDecisionTrace(
                    fixture_key=fixture.fixture_key,
                    attempts=tuple(attempts),
                    selected_provider=provider_name,
                    fallback_depth=attempt_index,
                    total_latency_ms=total_latency,
                    fail_closed=False,
                    configured_provider_order=self.config.provider_order,
                )
                routed = CascadeResult(observation, trace)
                routed.validate()
                return routed
            attempts.append(
                self._trace(
                    attempt_index,
                    provider_name,
                    effective_state,
                    effective_reason,
                    request_identity,
                    result.status_code,
                    result.latency_ms,
                    result.network_called,
                    fixture=fixture,
                    configured_provider_order=self.config.provider_order,
                    fallback_depth=attempt_index,
                    preflight_allowed=True,
                    preflight_reason=preflight.reason,
                    budget_decision="ALLOWED",
                    quota_before=preflight.quota_before,
                    quota_after=quota_after,
                    network_request_count=int(result.network_called),
                    quota_cost_units=(
                        float(preflight.request_cost) if result.network_called else 0.0
                    ),
                    credentials_present=self.budget.credential_available(config),
                    resolution=resolution,
                    request_started_at=current,
                    request_completed_at=current
                    + timedelta(milliseconds=result.latency_ms),
                )
            )
        return self._fail_closed(
            fixture, tuple(attempts), "all_providers_exhausted", total_latency
        )

    def _fresh_enough(self, observation, now: datetime) -> bool:
        if observation.source_timestamp is None:
            return observation.source_timing_provenance is TimingProvenance.CAPTURE_TIME_ONLY
        age = (now - observation.source_timestamp).total_seconds()
        return 0 <= age <= self.timing_policy.maximum_odds_age_seconds

    def _effective_quota(self, provider: str, result: AdapterResult):
        before = self.budget.quota_before(provider)
        after = result.quota_after
        if all(
            value is None
            for value in (
                after.used,
                after.remaining,
                after.reset_at,
                after.rate_limit,
                after.rate_remaining,
                after.rate_reset_at,
            )
        ):
            return before
        return after

    @staticmethod
    def _resolve_identity(
        fixture: Fixture,
        provider: str,
        *,
        current: datetime,
        provider_fixture_id: str | None,
        supplied: ProviderIdentityResolution | None,
        kickoff_tolerance_seconds: int,
    ) -> ProviderIdentityResolution | None:
        if supplied is not None:
            try:
                supplied.validate(
                    fixture=fixture,
                    kickoff_tolerance_seconds=kickoff_tolerance_seconds,
                )
                if supplied.provider != provider:
                    raise ProductionContractError(
                        "identity resolution provider does not match configured provider"
                    )
                if (
                    provider in IDENTITY_REQUIRED_PROVIDERS
                    and ProviderIdentityResolutionState(supplied.state)
                    is not ProviderIdentityResolutionState.RESOLVED
                ):
                    return supplied
                if (
                    provider_fixture_id is not None
                    and supplied.provider_id != provider_fixture_id
                ):
                    raise ProductionContractError(
                        "provider fixture id conflicts with identity resolution"
                    )
                return supplied
            except (AttributeError, TypeError, ValueError, ProductionContractError):
                return ProviderIdentityResolution(
                    provider=provider,
                    state=ProviderIdentityResolutionState.AMBIGUOUS,
                    canonical_fixture_key=fixture.fixture_key,
                    provider_id=provider_fixture_id,
                    home_team=fixture.home_team,
                    away_team=fixture.away_team,
                    kickoff=fixture.kickoff,
                    league_competition_evidence="",
                    resolution_provenance="invalid_supplied_resolution",
                    resolution_timestamp=current,
                    resolver_version="router-resolution-v1",
                    digest="",
                )
        if provider not in IDENTITY_REQUIRED_PROVIDERS:
            return None
        if not provider_fixture_id:
            return ProviderIdentityResolution(
                provider=provider,
                state=ProviderIdentityResolutionState.DISCOVERY_REQUIRED,
                canonical_fixture_key=fixture.fixture_key,
                provider_id=None,
                home_team=fixture.home_team,
                away_team=fixture.away_team,
                kickoff=fixture.kickoff,
                league_competition_evidence="",
                resolution_provenance="provider_id_not_supplied",
                resolution_timestamp=current,
                resolver_version="router-resolution-v1",
                digest="",
            )
        if provider == "betfair_delayed":
            return ProviderIdentityResolution(
                provider=provider,
                state=ProviderIdentityResolutionState.DISCOVERY_REQUIRED,
                canonical_fixture_key=fixture.fixture_key,
                provider_id=provider_fixture_id,
                home_team=fixture.home_team,
                away_team=fixture.away_team,
                kickoff=fixture.kickoff,
                league_competition_evidence="market catalogue runner mapping required",
                resolution_provenance="runner_mapping_not_supplied",
                resolution_timestamp=current,
                resolver_version="router-resolution-v1",
                digest="",
            )
        return ProviderIdentityResolution.resolved(
            provider=provider,
            fixture=fixture,
            provider_id=provider_fixture_id,
            league_competition_evidence=f"caller supplied {provider} discovery identity",
            resolution_provenance="caller_supplied_external_discovery",
            resolution_timestamp=current,
            resolver_version="router-resolution-v1",
            kickoff_tolerance_seconds=kickoff_tolerance_seconds,
        )

    def _trace(
        self,
        attempt_index: int,
        provider: str,
        state: ProviderState,
        reason: str,
        request_identity: str,
        status_code: int | None = None,
        latency_ms: int = 0,
        network_called: bool = False,
        fixture: Fixture | None = None,
        configured_provider_order: tuple[str, ...] = (),
        fallback_depth: int = 0,
        preflight_allowed: bool = False,
        preflight_reason: str = "",
        budget_decision: str = "REJECTED",
        quota_before=None,
        quota_after=None,
        network_request_count: int | None = None,
        quota_cost_units: float = 0.0,
        credentials_present: bool | None = None,
        observation=None,
        resolution: ProviderIdentityResolution | None = None,
        request_started_at: datetime | None = None,
        request_completed_at: datetime | None = None,
    ) -> ProviderAttemptTrace:
        if network_request_count is None:
            network_request_count = int(network_called)
        quota_before = quota_before or QuotaSnapshot()
        quota_after = quota_after or quota_before
        request_started_at = request_started_at or getattr(
            self, "_route_time", None
        )
        request_completed_at = request_completed_at or request_started_at
        return ProviderAttemptTrace(
            attempt_index=attempt_index,
            provider=provider,
            state=state,
            result="SUCCESS" if state is ProviderState.AVAILABLE else "REJECTED",
            reason=reason,
            network_called=network_called,
            request_identity=request_identity,
            status_code=status_code,
            latency_ms=latency_ms,
            configured_provider_order=configured_provider_order,
            fallback_depth=fallback_depth,
            preflight_allowed=preflight_allowed,
            preflight_reason=preflight_reason or reason,
            budget_decision=budget_decision,
            quota_before=quota_before,
            quota_after=quota_after,
            network_request_count=network_request_count,
            quota_cost_units=quota_cost_units,
            request_cost_classification="QUOTA_CONSUMING_REQUEST",
            credentials_present=credentials_present,
            provider_readiness_state=(
                "LIVE_PATH_READY_FOR_OBSERVATION"
                if state is ProviderState.AVAILABLE
                else "CONTRACT_SUPPORTED"
            ),
            provider_record_id=(
                observation.provider_fixture_id
                if observation is not None
                else resolution.provider_id
                if resolution is not None and resolution.provider_id is not None
                else f"{provider}:attempt:{attempt_index}"
            ),
            raw_record_digest=(
                observation.raw_record_digest
                if observation is not None
                else digest_record({"provider": provider, "reason": reason})
            ),
            fixture_key=fixture.fixture_key if fixture is not None else "",
            league_code=fixture.league_code if fixture is not None else "",
            home_team=fixture.home_team if fixture is not None else "",
            away_team=fixture.away_team if fixture is not None else "",
            kickoff=fixture.kickoff if fixture is not None else None,
            market_type=(observation.market_type if observation is not None else MARKET_PREMATCH_1X2),
            home_odds=observation.home_odds if observation is not None else None,
            draw_odds=observation.draw_odds if observation is not None else None,
            away_odds=observation.away_odds if observation is not None else None,
            bookmaker_identity=(
                observation.bookmaker_identity if observation is not None else None
            ),
            source_identity=(
                observation.provider_identity if observation is not None else None
            ),
            source_timestamp=(
                observation.source_timestamp if observation is not None else None
            ),
            source_timing_provenance=(
                observation.source_timing_provenance.value
                if observation is not None
                else TimingProvenance.SOURCE_TIMESTAMP.value
            ),
            identity_resolution_state=(
                ProviderIdentityResolutionState(resolution.state).value
                if resolution is not None
                else ""
            ),
            identity_resolution_digest=(
                resolution.digest if resolution is not None else ""
            ),
            adapter_version=(
                observation.adapter_version
                if observation is not None
                else "cascade-trace-v1"
            ),
            request_started_at=request_started_at,
            request_completed_at=request_completed_at,
        )

    @staticmethod
    def _fail_closed(
        fixture: Fixture,
        attempts: tuple[ProviderAttemptTrace, ...],
        reason: str,
        total_latency_ms: int = 0,
    ) -> CascadeResult:
        if reason == "unsupported_top5_league":
            identity = stable_request_identity("cascade", fixture, fixture.kickoff)
            attempts = (
                ProviderAttemptTrace(
                    attempt_index=0,
                    provider="cascade",
                    state=ProviderState.UNSUPPORTED_LEAGUE,
                    result="REJECTED",
                    reason=reason,
                    network_called=False,
                    request_identity=identity,
                ),
            )
        trace = CascadeDecisionTrace(
            fixture_key=fixture.fixture_key,
            attempts=attempts,
            selected_provider=None,
            fallback_depth=None,
            total_latency_ms=total_latency_ms,
            fail_closed=True,
        )
        result = CascadeResult(None, trace)
        result.validate()
        return result

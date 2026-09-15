"""Deterministic, sequential, fail-closed provider cascade router."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone

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
    ProviderAttemptTrace,
    ProviderCascadeConfig,
    ProviderState,
    stable_request_identity,
)
from src.football.provider_cascade.health import ProviderHealthRegistry

DEFAULT_ADAPTERS: Mapping[str, OddsProviderAdapter] = {
    "the_odds_api": TheOddsAPIAdapter(),
    "odds_api_io": OddsApiIoAdapter(),
    "api_football": ApiFootballAdapter(),
    "betfair_delayed": BetfairDelayedAdapter(),
}


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
        max_odds_age_seconds: int = 900,
    ) -> None:
        self.config = config or ProviderCascadeConfig.default()
        self.config.validate()
        if max_odds_age_seconds <= 0:
            raise ProductionContractError("max_odds_age_seconds must be positive")
        self.max_odds_age_seconds = max_odds_age_seconds
        self._uses_builtin_adapters = adapters is None
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
    ) -> CascadeResult:
        fixture.validate()
        if fixture.league_code not in TOP5_LEAGUE_CODES:
            return self._fail_closed(fixture, (), "unsupported_top5_league")
        current = _utc(now or datetime.now(timezone.utc), "route now")
        ids = provider_fixture_ids or {}
        attempts: list[ProviderAttemptTrace] = []
        total_latency = 0
        for attempt_index, provider_name in enumerate(self.config.provider_order):
            config = self.config.providers[provider_name]
            request_identity = stable_request_identity(provider_name, fixture, current)
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
                    )
                )
                continue
            if self._uses_builtin_adapters and not self.config.live_calls_authorized:
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
                    )
                )
                continue
            adapter = self.adapters[provider_name]
            try:
                result = adapter.fetch(
                    fixture,
                    config,
                    request_identity=request_identity,
                    requested_at=current,
                    provider_priority=attempt_index,
                    provider_fixture_id=ids.get(provider_name),
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
                    provider_name, request_cost=preflight.request_cost, at=current
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
                    )
                )
                trace = CascadeDecisionTrace(
                    fixture_key=fixture.fixture_key,
                    attempts=tuple(attempts),
                    selected_provider=provider_name,
                    fallback_depth=attempt_index,
                    total_latency_ms=total_latency,
                    fail_closed=False,
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
                )
            )
        return self._fail_closed(
            fixture, tuple(attempts), "all_providers_exhausted", total_latency
        )

    def _fresh_enough(self, observation, now: datetime) -> bool:
        age = (now - observation.source_timestamp).total_seconds()
        return 0 <= age <= self.max_odds_age_seconds

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
    def _trace(
        attempt_index: int,
        provider: str,
        state: ProviderState,
        reason: str,
        request_identity: str,
        status_code: int | None = None,
        latency_ms: int = 0,
        network_called: bool = False,
    ) -> ProviderAttemptTrace:
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

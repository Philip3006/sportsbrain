"""Local deterministic request and quota guard for provider routing."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite

from src.football.production_contracts import ProductionContractError, _utc
from src.football.provider_cascade.contracts import (
    ProviderCascadeConfig,
    ProviderConfig,
    ProviderState,
    QuotaSnapshot,
)
from src.football.provider_cascade.readiness import (
    QuotaStateStore,
    quota_reset_revalidation_eligible,
)


@dataclass
class ProviderBudgetCounters:
    requests_attempted: int = 0
    requests_successful: int = 0
    rejected_before_network: int = 0
    quota_consumed: float = 0.0
    last_preflight_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_class: ProviderState | None = None
    quota: QuotaSnapshot = field(default_factory=QuotaSnapshot)

    def as_payload(self) -> dict[str, object]:
        return {
            "requests_attempted": self.requests_attempted,
            "requests_successful": self.requests_successful,
            "rejected_before_network": self.rejected_before_network,
            "quota_consumed": self.quota_consumed,
            "last_preflight_at": self.last_preflight_at.isoformat()
            if self.last_preflight_at
            else None,
            "last_attempt_at": self.last_attempt_at.isoformat()
            if self.last_attempt_at
            else None,
            "last_success_at": self.last_success_at.isoformat()
            if self.last_success_at
            else None,
            "last_failure_at": self.last_failure_at.isoformat()
            if self.last_failure_at
            else None,
            "last_failure_class": self.last_failure_class.value
            if self.last_failure_class
            else None,
            "quota": self.quota.as_payload(),
        }


@dataclass(frozen=True)
class PreflightDecision:
    provider: str
    allowed: bool
    state: ProviderState
    reason: str
    network_called: bool = False
    request_cost: int = 1
    quota_before: QuotaSnapshot = field(default_factory=QuotaSnapshot)

    def validate(self) -> None:
        if (
            not self.provider.strip()
            or not self.reason.strip()
            or self.request_cost <= 0
        ):
            raise ProductionContractError("preflight decision is missing safe fields")
        if self.network_called:
            raise ProductionContractError("preflight cannot call the network")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": self.provider,
            "allowed": self.allowed,
            "state": self.state.value,
            "reason": self.reason,
            "network_called": False,
            "request_cost": self.request_cost,
            "quota_before": self.quota_before.as_payload(),
        }


class RequestBudgetManager:
    """In-memory budget authority for one deterministic cascade run.

    This object is intentionally not a billing or subscription authority.  It
    never writes provider state, buys quota, or performs account changes.
    """

    def __init__(
        self,
        config: ProviderCascadeConfig,
        *,
        quota_overrides: Mapping[str, QuotaSnapshot] | None = None,
        credential_overrides: Mapping[str, bool] | None = None,
        quota_state_store: QuotaStateStore | None = None,
        now: datetime | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self._now = _utc(now or datetime.now(timezone.utc), "budget now")
        overrides = quota_overrides or {}
        credential_overrides = credential_overrides or {}
        self._quotas: dict[str, QuotaSnapshot] = {}
        self._counters: dict[str, ProviderBudgetCounters] = {}
        self._credential_overrides = dict(credential_overrides)
        self._quota_state_store = quota_state_store
        persisted = quota_state_store.load() if quota_state_store is not None else {}
        self._revalidation_eligible: dict[str, bool] = {}
        for name, provider in config.providers.items():
            provider.validate()
            persisted_state = persisted.get(name)
            configured_quota = (
                persisted_state.quota
                if name not in overrides and persisted_state is not None
                else provider.initial_quota
            )
            self._quotas[name] = overrides.get(name, configured_quota)
            self._revalidation_eligible[name] = (
                name not in overrides
                and persisted_state is not None
                and persisted_state.observed_at is not None
                and quota_reset_revalidation_eligible(self._quotas[name], now=self._now)
            )
            self._counters[name] = ProviderBudgetCounters(quota=self._quotas[name])
        self._global_requests = 0

    @property
    def global_requests_attempted(self) -> int:
        return self._global_requests

    def quota_before(self, provider: str) -> QuotaSnapshot:
        return self._quotas.get(provider, QuotaSnapshot())

    def counters(self, provider: str) -> ProviderBudgetCounters:
        try:
            return self._counters[provider]
        except KeyError as exc:
            raise ProductionContractError(
                f"unknown provider budget: {provider}"
            ) from exc

    def credential_available(self, provider: ProviderConfig) -> bool:
        if provider.name in self._credential_overrides:
            return self._credential_overrides[provider.name]
        if provider.credential_available is not None:
            return provider.credential_available
        if not provider.credentials_required:
            return True
        return all(
            bool(os.getenv(name, "").strip()) for name in provider.credential_env
        )

    def preflight(
        self,
        provider_name: str,
        *,
        now: datetime | None = None,
        request_cost: int | None = None,
    ) -> PreflightDecision:
        at = _utc(now or self._now, "preflight now")
        provider = self.config.providers.get(provider_name)
        if provider is None:
            decision = PreflightDecision(
                provider_name, False, ProviderState.HEALTH_UNKNOWN, "unknown_provider"
            )
            decision.validate()
            return decision
        cost = provider.request_cost if request_cost is None else request_cost
        if cost <= 0:
            raise ProductionContractError("request cost must be positive")
        counter = self._counters[provider_name]
        counter.last_preflight_at = at
        quota = self._quotas[provider_name]
        if not provider.enabled:
            return self._reject(
                provider, ProviderState.CONFIG_DISABLED, "config_disabled", cost, quota
            )
        if not self.credential_available(provider):
            return self._reject(
                provider,
                ProviderState.CREDENTIAL_MISSING,
                "credential_missing",
                cost,
                quota,
            )
        # Request caps count network calls; quota cost is evaluated separately.
        if self._global_requests + 1 > self.config.global_request_budget:
            return self._reject(
                provider,
                ProviderState.QUOTA_EXHAUSTED,
                "global_request_budget_exhausted",
                cost,
                quota,
            )
        if self._global_requests + 1 > self.config.per_run_cap:
            return self._reject(
                provider,
                ProviderState.QUOTA_EXHAUSTED,
                "per_run_cap_exhausted",
                cost,
                quota,
            )
        if counter.requests_attempted + 1 > provider.request_budget:
            return self._reject(
                provider,
                ProviderState.QUOTA_EXHAUSTED,
                "provider_request_budget_exhausted",
                cost,
                quota,
            )
        reset_revalidation = self._revalidation_eligible.get(provider_name, False)
        if (
            quota.remaining is not None
            and quota.remaining < cost + provider.quota_reserve
            and not reset_revalidation
        ):
            return self._reject(
                provider,
                ProviderState.QUOTA_EXHAUSTED,
                "quota_exhausted_or_reserve",
                cost,
                quota,
            )
        if quota.rate_remaining is not None and quota.rate_remaining < cost:
            return self._reject(
                provider,
                ProviderState.RATE_LIMITED,
                "rate_limit_exhausted",
                cost,
                quota,
            )
        decision = PreflightDecision(
            provider_name,
            True,
            ProviderState.AVAILABLE,
            "quota_reset_revalidation_allowed"
            if reset_revalidation
            else "preflight_allowed",
            False,
            cost,
            quota,
        )
        decision.validate()
        return decision

    def _reject(
        self,
        provider: ProviderConfig,
        state: ProviderState,
        reason: str,
        cost: int,
        quota: QuotaSnapshot,
    ) -> PreflightDecision:
        self._counters[provider.name].rejected_before_network += 1
        decision = PreflightDecision(
            provider.name, False, state, reason, False, cost, quota
        )
        decision.validate()
        return decision

    def record_attempt(
        self,
        provider_name: str,
        *,
        request_count: int = 1,
        quota_cost_units: float = 1.0,
        request_cost: int | None = None,
        at: datetime | None = None,
    ) -> None:
        timestamp = _utc(at or self._now, "attempt at")
        if request_cost is not None:
            # Compatibility alias: this value was historically overloaded as
            # both request count and quota cost.  New callers must provide
            # request_count and quota_cost_units independently.
            quota_cost_units = float(request_cost)
        try:
            quota_cost = float(quota_cost_units)
        except (TypeError, ValueError) as exc:
            raise ProductionContractError(
                "request count and quota cost must be numeric"
            ) from exc
        if (
            not isinstance(request_count, int)
            or isinstance(request_count, bool)
            or request_count <= 0
            or not isfinite(quota_cost)
            or quota_cost <= 0
        ):
            raise ProductionContractError(
                "request count and quota cost must be positive"
            )
        counter = self.counters(provider_name)
        self._revalidation_eligible[provider_name] = False
        counter.requests_attempted += request_count
        self._global_requests += request_count
        counter.quota_consumed += quota_cost
        counter.last_attempt_at = timestamp

    def record_result(
        self,
        provider_name: str,
        *,
        state: ProviderState,
        at: datetime | None = None,
        quota_after: QuotaSnapshot | None = None,
        persist_quota_evidence: bool = True,
    ) -> None:
        timestamp = _utc(at or self._now, "result at")
        counter = self.counters(provider_name)
        if quota_after is not None:
            self._quotas[provider_name] = quota_after
            counter.quota = quota_after
        if state is ProviderState.AVAILABLE:
            counter.requests_successful += 1
            counter.last_success_at = timestamp
        else:
            counter.last_failure_at = timestamp
            counter.last_failure_class = state
        if self._quota_state_store is not None and persist_quota_evidence:
            self._persist_quota_state(
                provider_name,
                quota_after or self._quotas[provider_name],
                state=state,
                observed_at=timestamp,
            )

    def _persist_quota_state(
        self,
        provider_name: str,
        quota: QuotaSnapshot,
        *,
        state: ProviderState,
        observed_at: datetime,
    ) -> None:
        """Persist only redacted quota evidence after a network result."""

        assert self._quota_state_store is not None
        self._quota_state_store.update(
            provider_name,
            quota=quota,
            observed_at=observed_at,
            state=state.value,
            source="top5_adapter_response_headers",
        )

    def update_quota_from_headers(
        self,
        provider_name: str,
        headers: Mapping[str, object],
        *,
        completed_at: datetime | None = None,
    ) -> QuotaSnapshot:
        before = self.quota_before(provider_name)
        lowered = {
            str(key).lower(): str(value).strip() for key, value in headers.items()
        }

        def integer(*names: str, fallback: int | None) -> int | None:
            for name in names:
                raw = lowered.get(name.lower())
                if raw is not None:
                    try:
                        value = int(raw)
                    except ValueError:
                        return fallback
                    return value if value >= 0 else fallback
            return fallback

        after = QuotaSnapshot(
            used=integer(
                "x-requests-used", "x-ratelimit-requests-used", fallback=before.used
            ),
            remaining=integer(
                "x-requests-remaining",
                "x-ratelimit-requests-remaining",
                "x-ratelimit-remaining",
                fallback=before.remaining,
            ),
            reset_at=before.reset_at,
            rate_limit=integer(
                "x-ratelimit-limit", "x-rate-limit-limit", fallback=before.rate_limit
            ),
            rate_remaining=integer(
                "x-ratelimit-remaining",
                "x-rate-limit-remaining",
                fallback=before.rate_remaining,
            ),
            rate_reset_at=before.rate_reset_at,
        )
        self._quotas[provider_name] = after
        self._counters[provider_name].quota = after
        if self._quota_state_store is not None:
            self._persist_quota_state(
                provider_name,
                after,
                state=ProviderState.AVAILABLE,
                observed_at=_utc(
                    completed_at or self._now, "quota evidence completed_at"
                ),
            )
        return after

    def as_payload(self) -> dict[str, object]:
        return {
            "global_requests_attempted": self._global_requests,
            "global_request_budget": self.config.global_request_budget,
            "per_run_cap": self.config.per_run_cap,
            "providers": {
                name: counter.as_payload()
                for name, counter in sorted(self._counters.items())
            },
        }

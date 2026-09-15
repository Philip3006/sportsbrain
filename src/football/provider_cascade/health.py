"""Secret-free runtime provider health for the Top-5 cascade."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from src.football.production_contracts import ProductionContractError, _utc
from src.football.provider_cascade.contracts import (
    ProviderConfig,
    ProviderState,
    QuotaSnapshot,
)


@dataclass
class ProviderHealth:
    provider: str
    configured: bool
    credentials_present: bool
    candidate_only: bool
    validated: bool
    last_preflight_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_class: ProviderState | None = None
    rolling_availability: list[bool] = field(default_factory=list)
    quota_state: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    rate_limit_state: QuotaSnapshot = field(default_factory=QuotaSnapshot)

    def as_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "configured": self.configured,
            "credentials_present": self.credentials_present,
            "candidate_only": self.candidate_only,
            "validated": self.validated,
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
            "rolling_availability": list(self.rolling_availability),
            "quota_state": self.quota_state.as_payload(),
            "rate_limit_state": self.rate_limit_state.as_payload(),
        }


class ProviderHealthRegistry:
    def __init__(
        self, configs: Mapping[str, ProviderConfig], credentials: Mapping[str, bool]
    ) -> None:
        self._health: dict[str, ProviderHealth] = {
            name: ProviderHealth(
                provider=name,
                configured=config.enabled,
                credentials_present=bool(credentials.get(name, False)),
                candidate_only=config.candidate_only or not config.quality_eligible,
                validated=config.quality_eligible,
                quota_state=config.initial_quota,
            )
            for name, config in configs.items()
        }

    def get(self, provider: str) -> ProviderHealth:
        try:
            return self._health[provider]
        except KeyError as exc:
            raise ProductionContractError(
                f"unknown provider health: {provider}"
            ) from exc

    def preflight(self, provider: str, at: datetime) -> None:
        record = self.get(provider)
        record.last_preflight_at = _utc(at, "health preflight at")

    def attempt(self, provider: str, at: datetime) -> None:
        record = self.get(provider)
        record.last_attempt_at = _utc(at, "health attempt at")

    def result(
        self,
        provider: str,
        *,
        state: ProviderState,
        at: datetime,
        quota: QuotaSnapshot,
        rate_limit: QuotaSnapshot,
    ) -> None:
        record = self.get(provider)
        timestamp = _utc(at, "health result at")
        record.quota_state = quota
        record.rate_limit_state = rate_limit
        available = state is ProviderState.AVAILABLE
        record.rolling_availability.append(available)
        del record.rolling_availability[:-20]
        if available:
            record.last_success_at = timestamp
        else:
            record.last_failure_at = timestamp
            record.last_failure_class = state

    def as_payload(self) -> dict[str, object]:
        return {name: self._health[name].as_payload() for name in sorted(self._health)}

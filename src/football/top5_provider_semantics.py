"""Deterministic provider request and cost semantics for shadow simulations.

These types describe possible request behavior without importing a provider
client.  Cost units are an injected accounting scale, not a claim about a
vendor's billing rules.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite

from src.football.production_contracts import (
    ProductionContractError,
    ProviderMapping,
    _utc,
)


class BulkRequestOutcome(str, Enum):
    SUCCESS = "bulk_success"
    UNSUPPORTED_MARKET = "bulk_unsupported_market"
    PARTIAL_EVENT_FALLBACK = "partial_event_fallback"
    PROVIDER_TIMEOUT = "provider_timeout"
    EMPTY_PAYLOAD = "empty_payload"


@dataclass(frozen=True)
class ProviderRequestContext:
    """The request identity needed to coalesce compatible evaluations."""

    league_code: str
    provider_name: str
    sport_key: str
    markets: tuple[str, ...]
    regions: tuple[str, ...]

    @classmethod
    def from_mapping(cls, league_code: str, mapping: ProviderMapping) -> ProviderRequestContext:
        mapping.validate()
        return cls(
            league_code=league_code,
            provider_name=mapping.provider_name,
            sport_key=mapping.sport_key,
            markets=tuple(sorted(set(mapping.markets))),
            regions=tuple(sorted(set(mapping.regions))),
        )

    def validate(self) -> None:
        if any(not value.strip() for value in (
            self.league_code,
            self.provider_name,
            self.sport_key,
        )):
            raise ProductionContractError("provider request context requires identity")
        if not self.markets or not self.regions:
            raise ProductionContractError("provider request context requires markets and regions")
        if any(not value.strip() for value in (*self.markets, *self.regions)):
            raise ProductionContractError("provider request context has blank market or region")


@dataclass(frozen=True)
class LogicalFixtureEvaluation:
    """One fixture evaluated at one candidate signal-time attempt."""

    fixture_key: str
    league_code: str
    attempted_at: datetime
    attempt_number: int
    request_bucket: str
    provider: ProviderRequestContext

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempted_at", _utc(self.attempted_at, "attempted_at"))

    def validate(self) -> None:
        if not self.fixture_key.strip() or not self.league_code.strip() or not self.request_bucket.strip():
            raise ProductionContractError("logical evaluation requires fixture and request identity")
        if self.attempt_number < 0:
            raise ProductionContractError("logical evaluation attempt number must be non-negative")
        self.provider.validate()
        if self.provider.league_code != self.league_code:
            raise ProductionContractError("logical evaluation provider belongs to another league")


@dataclass(frozen=True)
class BulkProviderRequest:
    """One bulk sport-key request capable of serving multiple fixtures."""

    league_code: str
    provider_name: str
    sport_key: str
    requested_at: datetime
    request_bucket: str
    markets: tuple[str, ...]
    regions: tuple[str, ...]
    fixture_keys: tuple[str, ...]
    outcome: BulkRequestOutcome = BulkRequestOutcome.SUCCESS

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_at", _utc(self.requested_at, "requested_at"))

    def validate(self) -> None:
        if any(not value.strip() for value in (
            self.league_code,
            self.provider_name,
            self.sport_key,
            self.request_bucket,
        )):
            raise ProductionContractError("bulk provider request requires identity")
        if not self.fixture_keys or len(set(self.fixture_keys)) != len(self.fixture_keys):
            raise ProductionContractError("bulk provider request requires unique fixtures")
        if not self.markets or not self.regions:
            raise ProductionContractError("bulk provider request requires markets and regions")
        if any(not value.strip() for value in (*self.markets, *self.regions, *self.fixture_keys)):
            raise ProductionContractError("bulk provider request has blank fields")

    @property
    def request_key(self) -> str:
        self.validate()
        return "|".join(
            (
                self.league_code,
                self.provider_name,
                self.sport_key,
                self.request_bucket,
                ",".join(self.markets),
                ",".join(self.regions),
            )
        )


@dataclass(frozen=True)
class FallbackEventRequest:
    """One event-level request after a bulk path fails or is inapplicable."""

    league_code: str
    provider_name: str
    sport_key: str
    fixture_key: str
    requested_at: datetime
    reason: BulkRequestOutcome

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_at", _utc(self.requested_at, "requested_at"))

    def validate(self) -> None:
        if any(not value.strip() for value in (
            self.league_code,
            self.provider_name,
            self.sport_key,
            self.fixture_key,
        )):
            raise ProductionContractError("fallback event request requires identity")
        if self.reason is BulkRequestOutcome.SUCCESS:
            raise ProductionContractError("successful bulk path cannot create event fallback")


@dataclass(frozen=True)
class FallbackScenario:
    """A static bulk outcome and optional exact subset for event fallback."""

    name: str
    outcome: BulkRequestOutcome = BulkRequestOutcome.SUCCESS
    fallback_fixture_keys: tuple[str, ...] = ()

    def validate(self, available_fixture_keys: Iterable[str] = ()) -> None:
        if not self.name.strip():
            raise ProductionContractError("fallback scenario requires a name")
        available = set(available_fixture_keys)
        requested = set(self.fallback_fixture_keys)
        if len(requested) != len(self.fallback_fixture_keys):
            raise ProductionContractError("fallback scenario contains duplicate fixtures")
        if not requested.issubset(available):
            raise ProductionContractError("fallback scenario contains an unknown fixture")
        if self.outcome is BulkRequestOutcome.SUCCESS and requested:
            raise ProductionContractError("successful bulk scenario cannot contain fallback fixtures")
        if self.outcome is BulkRequestOutcome.PARTIAL_EVENT_FALLBACK and not requested:
            raise ProductionContractError("partial fallback scenario requires exact fixture keys")

    def fallback_keys(self, available_fixture_keys: Iterable[str]) -> tuple[str, ...]:
        available = tuple(sorted(set(available_fixture_keys)))
        if self.outcome is BulkRequestOutcome.SUCCESS:
            return ()
        if self.outcome is BulkRequestOutcome.PARTIAL_EVENT_FALLBACK:
            return tuple(sorted(set(self.fallback_fixture_keys).intersection(available)))
        return tuple(sorted(self.fallback_fixture_keys or available))


@dataclass(frozen=True)
class ProviderCostModel:
    """Configurable provider cost units for each request category."""

    bulk_request_cost_units: float = 1.0
    fallback_event_request_cost_units: float = 1.0
    result_request_cost_units: float = 1.0
    revalidation_request_cost_units: float = 1.0
    closing_capture_request_cost_units: float = 1.0

    def validate(self) -> None:
        values = (
            self.bulk_request_cost_units,
            self.fallback_event_request_cost_units,
            self.result_request_cost_units,
            self.revalidation_request_cost_units,
            self.closing_capture_request_cost_units,
        )
        if any(not isfinite(float(value)) or value < 0 for value in values):
            raise ProductionContractError("provider cost units must be finite and non-negative")

    def cost_units(
        self,
        *,
        bulk_requests: float = 0,
        fallback_event_requests: float = 0,
        result_requests: float = 0,
        revalidation_requests: float = 0,
        closing_capture_requests: float = 0,
    ) -> float:
        self.validate()
        return sum((
            bulk_requests * self.bulk_request_cost_units,
            fallback_event_requests * self.fallback_event_request_cost_units,
            result_requests * self.result_request_cost_units,
            revalidation_requests * self.revalidation_request_cost_units,
            closing_capture_requests * self.closing_capture_request_cost_units,
        ))

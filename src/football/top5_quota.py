"""Deterministic quota and provider-cost estimates with no provider calls."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import ceil, isfinite
from types import MappingProxyType

from src.football.production_contracts import (
    ProductionContractError,
    SignalTimeContract,
)
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_provider_semantics import ProviderCostModel


@dataclass(frozen=True)
class QuotaAssumptions:
    """A named architecture's explicit request and cost assumptions.

    ``signal_attempts_per_fixture`` counts logical evaluations, not HTTP
    requests.  With ``bulk_reuse=True`` those evaluations are coalesced into
    league-level sport-key requests by signal pass/retry cycle.
    """

    signal_attempts_per_fixture: int = 1
    revalidation_requests_per_fixture: int = 0
    closing_capture_requests_per_fixture: int = 0
    fixture_bulk_requests_per_league: int = 1
    odds_bulk_batch_size: int | None = None
    fallback_requests_per_fixture: int = 0
    result_requests_per_league: int = 1
    bulk_reuse: bool = True
    signal_window_passes: int = 1
    fallback_probability_by_league: Mapping[str, float] = field(default_factory=dict)
    fallback_event_requests_by_league: Mapping[str, int] = field(default_factory=dict)
    markets: tuple[str, ...] = ("h2h", "totals", "spreads")
    regions: tuple[str, ...] = ("eu",)
    cost_model: ProviderCostModel = field(default_factory=ProviderCostModel)
    signal_time_contract: SignalTimeContract | None = None

    def validate(self) -> None:
        if self.signal_attempts_per_fixture <= 0:
            raise ProductionContractError("signal attempts per fixture must be positive")
        if any(value < 0 for value in (
            self.revalidation_requests_per_fixture,
            self.closing_capture_requests_per_fixture,
            self.fixture_bulk_requests_per_league,
            self.fallback_requests_per_fixture,
            self.result_requests_per_league,
        )):
            raise ProductionContractError("quota request counts must be non-negative")
        if self.odds_bulk_batch_size is not None and self.odds_bulk_batch_size <= 0:
            raise ProductionContractError("bulk batch size must be positive when supplied")
        if self.signal_window_passes <= 0:
            raise ProductionContractError("signal window passes must be positive")
        if not self.markets or not self.regions:
            raise ProductionContractError("quota estimate requires markets and regions")
        if any(not value.strip() for value in (*self.markets, *self.regions)):
            raise ProductionContractError("quota estimate has blank market or region")
        unknown = (
            set(self.fallback_probability_by_league)
            | set(self.fallback_event_requests_by_league)
        ) - set(TOP5_LEAGUE_ADAPTERS)
        if unknown:
            raise ProductionContractError(f"quota fallback assumptions contain unknown leagues: {sorted(unknown)}")
        if any(
            not isfinite(float(probability)) or not 0 <= probability <= 1
            for probability in self.fallback_probability_by_league.values()
        ):
            raise ProductionContractError("fallback probabilities must be finite values in [0, 1]")
        if any(count < 0 for count in self.fallback_event_requests_by_league.values()):
            raise ProductionContractError("fallback event request counts must be non-negative")
        if self.signal_time_contract is not None:
            self.signal_time_contract.validate()
        self.cost_model.validate()


@dataclass(frozen=True)
class QuotaHorizon:
    name: str
    fixtures_by_league: Mapping[str, int]

    def validate(self) -> None:
        if not self.name.strip():
            raise ProductionContractError("quota horizon requires a name")
        unknown = set(self.fixtures_by_league) - set(TOP5_LEAGUE_ADAPTERS)
        if unknown:
            raise ProductionContractError(f"quota horizon contains unknown leagues: {sorted(unknown)}")
        if any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in self.fixtures_by_league.values()
        ):
            raise ProductionContractError("fixture counts must be non-negative integers")


@dataclass(frozen=True)
class LeagueQuotaEstimate:
    league_code: str
    fixtures: int
    logical_signal_evaluations: int
    bulk_odds_requests: int
    fallback_event_requests: float
    result_requests: int
    revalidation_requests: int
    closing_capture_requests: int
    estimated_provider_cost_units: float

    @property
    def raw_http_request_count(self) -> float:
        return sum((
            self.bulk_odds_requests,
            self.fallback_event_requests,
            self.result_requests,
            self.revalidation_requests,
            self.closing_capture_requests,
        ))

    # Compatibility aliases from the first readiness pass.
    @property
    def fixture_count(self) -> int:
        return self.fixtures

    @property
    def signal_requests(self) -> int:
        return self.logical_signal_evaluations

    @property
    def fixture_requests(self) -> int:
        return self.bulk_odds_requests

    @property
    def fallback_requests(self) -> float:
        return self.fallback_event_requests

    @property
    def total_requests(self) -> float:
        return self.raw_http_request_count


@dataclass(frozen=True)
class QuotaEstimate:
    horizon: str
    markets: tuple[str, ...]
    regions: tuple[str, ...]
    leagues: tuple[LeagueQuotaEstimate, ...]
    signal_time_contract: SignalTimeContract | None = None

    @property
    def total_fixtures(self) -> int:
        return sum(league.fixtures for league in self.leagues)

    @property
    def logical_signal_evaluations(self) -> int:
        return sum(league.logical_signal_evaluations for league in self.leagues)

    @property
    def bulk_odds_requests(self) -> int:
        return sum(league.bulk_odds_requests for league in self.leagues)

    @property
    def fallback_event_requests(self) -> float:
        return sum(league.fallback_event_requests for league in self.leagues)

    @property
    def result_requests(self) -> int:
        return sum(league.result_requests for league in self.leagues)

    @property
    def revalidation_requests(self) -> int:
        return sum(league.revalidation_requests for league in self.leagues)

    @property
    def closing_capture_requests(self) -> int:
        return sum(league.closing_capture_requests for league in self.leagues)

    @property
    def raw_http_request_count(self) -> float:
        return sum(league.raw_http_request_count for league in self.leagues)

    @property
    def estimated_provider_cost_units(self) -> float:
        return sum(league.estimated_provider_cost_units for league in self.leagues)

    # Compatibility alias; this now means raw HTTP requests, not fixtures.
    @property
    def total_requests(self) -> float:
        return self.raw_http_request_count

    def as_payload(self) -> dict[str, object]:
        return {
            "horizon": self.horizon,
            "markets": list(self.markets),
            "regions": list(self.regions),
            "signal_time_contract": _contract_payload(self.signal_time_contract),
            "total_fixtures": self.total_fixtures,
            "logical_signal_evaluations": self.logical_signal_evaluations,
            "bulk_odds_requests": self.bulk_odds_requests,
            "fallback_event_requests": self.fallback_event_requests,
            "result_requests": self.result_requests,
            "revalidation_requests": self.revalidation_requests,
            "closing_capture_requests": self.closing_capture_requests,
            "raw_http_request_count": self.raw_http_request_count,
            "estimated_provider_cost_units": self.estimated_provider_cost_units,
            "leagues": [
                {
                    "league": league.league_code,
                    "fixtures": league.fixtures,
                    "logical_signal_evaluations": league.logical_signal_evaluations,
                    "bulk_odds_requests": league.bulk_odds_requests,
                    "fallback_event_requests": league.fallback_event_requests,
                    "result_requests": league.result_requests,
                    "revalidation_requests": league.revalidation_requests,
                    "closing_capture_requests": league.closing_capture_requests,
                    "raw_http_request_count": league.raw_http_request_count,
                    "estimated_provider_cost_units": league.estimated_provider_cost_units,
                }
                for league in self.leagues
            ],
        }


def estimate_quota(horizon: QuotaHorizon, assumptions: QuotaAssumptions) -> QuotaEstimate:
    """Estimate raw requests and injected cost units for one horizon."""

    horizon.validate()
    assumptions.validate()
    estimates: list[LeagueQuotaEstimate] = []
    for league_code in sorted(TOP5_LEAGUE_ADAPTERS):
        fixtures = int(horizon.fixtures_by_league.get(league_code, 0))
        logical = fixtures * assumptions.signal_attempts_per_fixture
        if assumptions.bulk_reuse:
            signal_cycles = assumptions.signal_window_passes + max(
                0,
                assumptions.signal_attempts_per_fixture - 1,
            )
            bulk_requests = _bulk_request_count(fixtures, assumptions) * signal_cycles
        else:
            bulk_requests = logical * assumptions.fixture_bulk_requests_per_league
        exact_fallback = assumptions.fallback_event_requests_by_league.get(league_code)
        if exact_fallback is None:
            probability = assumptions.fallback_probability_by_league.get(league_code, 0.0)
            fallback_requests = (
                logical * probability
                + logical * assumptions.fallback_requests_per_fixture
            )
        else:
            fallback_requests = float(exact_fallback)
        result_requests = assumptions.result_requests_per_league if fixtures else 0
        revalidation = fixtures * assumptions.revalidation_requests_per_fixture
        closing_capture = fixtures * assumptions.closing_capture_requests_per_fixture
        cost_units = assumptions.cost_model.cost_units(
            bulk_requests=bulk_requests,
            fallback_event_requests=fallback_requests,
            result_requests=result_requests,
            revalidation_requests=revalidation,
            closing_capture_requests=closing_capture,
        )
        estimates.append(
            LeagueQuotaEstimate(
                league_code=league_code,
                fixtures=fixtures,
                logical_signal_evaluations=logical,
                bulk_odds_requests=bulk_requests,
                fallback_event_requests=fallback_requests,
                result_requests=result_requests,
                revalidation_requests=revalidation,
                closing_capture_requests=closing_capture,
                estimated_provider_cost_units=cost_units,
            )
        )
    return QuotaEstimate(
        horizon=horizon.name,
        markets=tuple(assumptions.markets),
        regions=tuple(assumptions.regions),
        leagues=tuple(estimates),
        signal_time_contract=assumptions.signal_time_contract,
    )


def compare_quota_architectures(
    horizons: Sequence[QuotaHorizon],
    architectures: Mapping[str, QuotaAssumptions],
) -> Mapping[str, tuple[QuotaEstimate, ...]]:
    """Compare deterministic scenarios without selecting cadence or quota."""

    if not architectures or any(not name.strip() for name in architectures):
        raise ProductionContractError("at least one named quota architecture is required")
    return MappingProxyType(
        {
            name: tuple(estimate_quota(horizon, assumptions) for horizon in horizons)
            for name, assumptions in architectures.items()
        }
    )


def _bulk_request_count(fixtures: int, assumptions: QuotaAssumptions) -> int:
    if fixtures == 0:
        return 0
    batches = (
        ceil(fixtures / assumptions.odds_bulk_batch_size)
        if assumptions.odds_bulk_batch_size is not None
        else 1
    )
    return batches * assumptions.fixture_bulk_requests_per_league


def _contract_payload(contract: SignalTimeContract | None) -> dict[str, int] | None:
    if contract is None:
        return None
    return {
        "minimum_minutes_before_kickoff": contract.minimum_minutes_before_kickoff,
        "maximum_minutes_before_kickoff": contract.maximum_minutes_before_kickoff,
        "maximum_odds_age_seconds": contract.maximum_odds_age_seconds,
    }

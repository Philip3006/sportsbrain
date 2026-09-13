"""Deterministic quota estimates with no provider calls."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil
from types import MappingProxyType

from src.football.production_contracts import ProductionContractError
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS


@dataclass(frozen=True)
class QuotaAssumptions:
    """Cost assumptions, deliberately supplied by the caller."""

    signal_attempts_per_fixture: int = 1
    revalidation_requests_per_fixture: int = 0
    closing_capture_requests_per_fixture: int = 0
    fixture_bulk_requests_per_league: int = 1
    odds_bulk_batch_size: int = 100
    fallback_requests_per_fixture: int = 0
    markets: tuple[str, ...] = ("h2h", "totals", "spreads")
    regions: tuple[str, ...] = ("eu",)

    def validate(self) -> None:
        if self.signal_attempts_per_fixture <= 0:
            raise ProductionContractError("signal attempts per fixture must be positive")
        if any(value < 0 for value in (
            self.revalidation_requests_per_fixture,
            self.closing_capture_requests_per_fixture,
            self.fallback_requests_per_fixture,
        )):
            raise ProductionContractError("quota request counts must be non-negative")
        if self.fixture_bulk_requests_per_league < 0 or self.odds_bulk_batch_size <= 0:
            raise ProductionContractError("bulk quota assumptions are invalid")
        if not self.markets or not self.regions:
            raise ProductionContractError("quota estimate requires markets and regions")


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
        if any(count < 0 for count in self.fixtures_by_league.values()):
            raise ProductionContractError("fixture counts must be non-negative")


@dataclass(frozen=True)
class LeagueQuotaEstimate:
    league_code: str
    fixture_count: int
    signal_requests: int
    revalidation_requests: int
    closing_capture_requests: int
    fixture_requests: int
    fallback_requests: int

    @property
    def total_requests(self) -> int:
        return sum((
            self.signal_requests,
            self.revalidation_requests,
            self.closing_capture_requests,
            self.fixture_requests,
            self.fallback_requests,
        ))


@dataclass(frozen=True)
class QuotaEstimate:
    horizon: str
    markets: tuple[str, ...]
    regions: tuple[str, ...]
    leagues: tuple[LeagueQuotaEstimate, ...]

    @property
    def total_requests(self) -> int:
        return sum(league.total_requests for league in self.leagues)

    @property
    def total_fixtures(self) -> int:
        return sum(league.fixture_count for league in self.leagues)

    def as_payload(self) -> dict[str, object]:
        return {
            "horizon": self.horizon,
            "markets": list(self.markets),
            "regions": list(self.regions),
            "total_fixtures": self.total_fixtures,
            "total_requests": self.total_requests,
            "leagues": [
                {
                    "league": league.league_code,
                    "fixture_count": league.fixture_count,
                    "signal_requests": league.signal_requests,
                    "revalidation_requests": league.revalidation_requests,
                    "closing_capture_requests": league.closing_capture_requests,
                    "fixture_requests": league.fixture_requests,
                    "fallback_requests": league.fallback_requests,
                    "total_requests": league.total_requests,
                }
                for league in self.leagues
            ],
        }


def estimate_quota(horizon: QuotaHorizon, assumptions: QuotaAssumptions) -> QuotaEstimate:
    """Estimate request units from counts and explicit architecture assumptions."""

    horizon.validate()
    assumptions.validate()
    estimates: list[LeagueQuotaEstimate] = []
    for league_code in sorted(TOP5_LEAGUE_ADAPTERS):
        fixture_count = int(horizon.fixtures_by_league.get(league_code, 0))
        fixture_requests = (
            ceil(fixture_count / assumptions.odds_bulk_batch_size)
            * assumptions.fixture_bulk_requests_per_league
            if fixture_count
            else 0
        )
        estimates.append(
            LeagueQuotaEstimate(
                league_code=league_code,
                fixture_count=fixture_count,
                signal_requests=fixture_count * assumptions.signal_attempts_per_fixture,
                revalidation_requests=fixture_count * assumptions.revalidation_requests_per_fixture,
                closing_capture_requests=fixture_count * assumptions.closing_capture_requests_per_fixture,
                fixture_requests=fixture_requests,
                fallback_requests=fixture_count * assumptions.fallback_requests_per_fixture,
            )
        )
    return QuotaEstimate(
        horizon=horizon.name,
        markets=tuple(assumptions.markets),
        regions=tuple(assumptions.regions),
        leagues=tuple(estimates),
    )


def compare_quota_architectures(
    horizons: Sequence[QuotaHorizon],
    architectures: Mapping[str, QuotaAssumptions],
) -> Mapping[str, tuple[QuotaEstimate, ...]]:
    """Compare deterministic scenarios without selecting a cadence or buying quota."""

    if not architectures or any(not name.strip() for name in architectures):
        raise ProductionContractError("at least one named quota architecture is required")
    return MappingProxyType(
        {
            name: tuple(estimate_quota(horizon, assumptions) for horizon in horizons)
            for name, assumptions in architectures.items()
        }
    )

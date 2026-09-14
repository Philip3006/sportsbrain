"""Candidate comparison for the Top-5 event-relative signal-time contract.

The matrix exposes operational trade-offs without choosing a production
cadence.  All values are planning proxies unless a caller supplies observed
shadow measurements.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType

from src.football.production_contracts import (
    ProductionContractError,
    SignalTimeContract,
)
from src.football.top5_provider_semantics import ProviderCostModel
from src.football.top5_quota import QuotaAssumptions, QuotaHorizon, estimate_quota


@dataclass(frozen=True)
class SignalTimeCandidateConfig:
    """One explicitly named signal-time proposal, never an approval."""

    name: str
    signal_time_contract: SignalTimeContract
    cadence_seconds: int
    retry_count: int = 0
    signal_time_passes: int = 1
    bulk_reuse: bool = True
    provider_timeout_seconds: int = 10
    inference_latency_seconds: int = 0
    fallback_rate_by_league: Mapping[str, float] = MappingProxyType({})

    @property
    def contract(self) -> SignalTimeContract:
        return self.signal_time_contract

    def validate(self) -> None:
        if not self.name.strip():
            raise ProductionContractError("signal-time candidate requires a name")
        self.signal_time_contract.validate()
        if self.cadence_seconds <= 0 or self.retry_count < 0 or self.signal_time_passes <= 0:
            raise ProductionContractError("signal-time cadence, retries, and passes are invalid")
        if self.provider_timeout_seconds <= 0 or self.inference_latency_seconds < 0:
            raise ProductionContractError("signal-time latency values are invalid")
        if any(
            not isfinite(float(rate)) or not 0 <= rate <= 1
            for rate in self.fallback_rate_by_league.values()
        ):
            raise ProductionContractError("signal-time fallback rates must be in [0, 1]")


@dataclass(frozen=True)
class SignalTimeDecisionEstimate:
    """Comparable planning output for one candidate."""

    candidate_name: str
    fixture_count: int
    expected_fixture_coverage: float
    request_count: float
    fallback_frequency: float
    stale_rejection_risk: float
    latency_budget_seconds: int
    cost_units: float
    operational_complexity: int
    logical_evaluations: int
    bulk_odds_requests: int
    fallback_event_requests: float
    raw_http_requests: float
    selected: bool = False

    @property
    def expected_coverage(self) -> float:
        return self.expected_fixture_coverage

    @property
    def estimated_provider_cost_units(self) -> float:
        return self.cost_units

    def validate(self) -> None:
        if not self.candidate_name.strip() or self.fixture_count < 0:
            raise ProductionContractError("signal-time estimate identity/count is invalid")
        bounded = (
            self.expected_fixture_coverage,
            self.fallback_frequency,
            self.stale_rejection_risk,
        )
        if any(not isfinite(float(value)) or not 0 <= value <= 1 for value in bounded):
            raise ProductionContractError("signal-time estimate ratios must be in [0, 1]")
        if any(value < 0 for value in (
            self.request_count,
            self.latency_budget_seconds,
            self.cost_units,
            self.operational_complexity,
            self.logical_evaluations,
            self.bulk_odds_requests,
            self.fallback_event_requests,
            self.raw_http_requests,
        )):
            raise ProductionContractError("signal-time estimate values must be non-negative")
        if self.selected:
            raise ProductionContractError("candidate comparison cannot select a production winner")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "candidate": self.candidate_name,
            "fixtures": self.fixture_count,
            "expected_fixture_coverage": self.expected_fixture_coverage,
            "request_count": self.request_count,
            "logical_evaluations": self.logical_evaluations,
            "bulk_odds_requests": self.bulk_odds_requests,
            "fallback_event_requests": self.fallback_event_requests,
            "fallback_frequency": self.fallback_frequency,
            "stale_rejection_risk": self.stale_rejection_risk,
            "latency_budget_seconds": self.latency_budget_seconds,
            "cost_units": self.cost_units,
            "raw_http_requests": self.raw_http_requests,
            "operational_complexity": self.operational_complexity,
            "selected": False,
        }


@dataclass(frozen=True)
class SignalTimeDecisionMatrix:
    """Immutable candidate estimates with no automatic recommendation."""

    estimates: tuple[SignalTimeDecisionEstimate, ...]
    recommendation: str | None = None

    def validate(self) -> None:
        if not self.estimates:
            raise ProductionContractError("signal-time matrix requires candidates")
        names = [estimate.candidate_name for estimate in self.estimates]
        if len(set(names)) != len(names):
            raise ProductionContractError("signal-time candidate names must be unique")
        if self.recommendation is not None:
            raise ProductionContractError("signal-time production winner requires explicit CEO decision")
        for estimate in self.estimates:
            estimate.validate()

    @property
    def selected_candidate(self) -> None:
        return None

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "recommendation": None,
            "candidates": [estimate.as_payload() for estimate in self.estimates],
        }


def compare_signal_time_candidates(
    candidates: Sequence[SignalTimeCandidateConfig],
    fixtures_by_league: Mapping[str, int] | QuotaHorizon,
    *,
    markets: tuple[str, ...] = ("h2h", "totals", "spreads"),
    regions: tuple[str, ...] = ("eu",),
    cost_model: ProviderCostModel | None = None,
    result_requests_per_league: int = 1,
    revalidation_requests_per_fixture: int = 0,
    closing_capture_requests_per_fixture: int = 0,
) -> SignalTimeDecisionMatrix:
    """Compare candidates without selecting cadence or activation scope."""

    if not candidates:
        raise ProductionContractError("at least one signal-time candidate is required")
    names = [candidate.name for candidate in candidates]
    if len(set(names)) != len(names):
        raise ProductionContractError("signal-time candidate names must be unique")
    if isinstance(fixtures_by_league, QuotaHorizon):
        fixtures = fixtures_by_league.fixtures_by_league
    else:
        fixtures = fixtures_by_league
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in fixtures.values()
    ):
        raise ProductionContractError("signal-time fixture counts must be non-negative integers")
    total_fixtures = sum(fixtures.values())
    estimates: list[SignalTimeDecisionEstimate] = []
    for candidate in candidates:
        candidate.validate()
        assumptions = QuotaAssumptions(
            signal_attempts_per_fixture=candidate.retry_count + 1,
            signal_window_passes=candidate.signal_time_passes,
            bulk_reuse=candidate.bulk_reuse,
            fallback_rate_by_league=candidate.fallback_rate_by_league,
            markets=markets,
            regions=regions,
            cost_model=cost_model or ProviderCostModel(),
            result_requests_per_league=result_requests_per_league,
            revalidation_requests_per_fixture=revalidation_requests_per_fixture,
            closing_capture_requests_per_fixture=closing_capture_requests_per_fixture,
            cadence_seconds=candidate.cadence_seconds,
        )
        quota = estimate_quota(QuotaHorizon("candidate", fixtures), assumptions)
        logical = quota.logical_signal_evaluations
        fallback = quota.fallback_event_requests
        fallback_frequency = fallback / logical if logical else 0.0
        window_seconds = (
            candidate.contract.maximum_minutes_before_kickoff
            - candidate.contract.minimum_minutes_before_kickoff
        ) * 60
        coverage_proxy = min(1.0, window_seconds / (window_seconds + candidate.cadence_seconds)) if window_seconds else 0.0
        stale_risk = min(
            1.0,
            candidate.cadence_seconds / candidate.contract.maximum_odds_age_seconds,
        )
        complexity = (
            1
            + candidate.retry_count
            + candidate.signal_time_passes
            + (0 if candidate.bulk_reuse else max(1, logical))
            + (1 if fallback_frequency else 0)
        )
        estimate = SignalTimeDecisionEstimate(
            candidate_name=candidate.name,
            fixture_count=total_fixtures,
            expected_fixture_coverage=coverage_proxy,
            request_count=quota.raw_http_request_count,
            fallback_frequency=fallback_frequency,
            stale_rejection_risk=stale_risk,
            latency_budget_seconds=candidate.provider_timeout_seconds + candidate.inference_latency_seconds,
            cost_units=quota.estimated_provider_cost_units,
            operational_complexity=complexity,
            logical_evaluations=logical,
            bulk_odds_requests=quota.bulk_odds_requests,
            fallback_event_requests=fallback,
            raw_http_requests=quota.raw_http_request_count,
        )
        estimate.validate()
        estimates.append(estimate)
    matrix = SignalTimeDecisionMatrix(tuple(estimates))
    matrix.validate()
    return matrix


build_signal_time_matrix = compare_signal_time_candidates

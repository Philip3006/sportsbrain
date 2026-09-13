"""Shadow-performance measurement and fail-closed gate definitions.

This module consumes future shadow observations in memory.  Closing snapshots
are benchmark inputs only and are structurally kept away from prediction
inputs.  No profitability threshold is defined here.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite, log

from src.football.production_contracts import (
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
)


@dataclass(frozen=True)
class ShadowObservation:
    """One immutable observation from a no-bet shadow run."""

    fixture_key: str
    league_code: str
    signal_time_success: bool
    inference_success: bool
    prediction_valid: bool
    candidate_available: bool
    fixture_discovered: bool = True
    signal_snapshot: MarketSnapshot | None = None
    closing_snapshot: MarketSnapshot | None = None
    probabilities: Mapping[str, float] = field(default_factory=dict)
    actual_outcome: str | None = None
    provider_latency_ms: int = 0
    inference_latency_ms: int = 0
    retry_count: int = 0
    fallback_used: bool = False
    stale_rejected: bool = False
    duplicate_suppressed: bool = False
    provider_failed: bool = False
    provenance_complete: bool = True
    no_bet: bool = True
    cross_league_collision: bool = False

    def validate(self) -> None:
        if not self.fixture_key.strip() or not self.league_code.strip():
            raise ProductionContractError("shadow observation requires fixture and league identity")
        if self.provider_latency_ms < 0 or self.inference_latency_ms < 0 or self.retry_count < 0:
            raise ProductionContractError("shadow observation timings and retries must be non-negative")
        if self.signal_snapshot is not None:
            self.signal_snapshot.validate()
            if self.signal_snapshot.fixture_key != self.fixture_key:
                raise ProductionContractError("signal snapshot belongs to another fixture")
            if self.signal_snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
                raise ProductionContractError("closing values cannot enter signal-time prediction input")
        if self.closing_snapshot is not None:
            self.closing_snapshot.validate()
            if self.closing_snapshot.fixture_key != self.fixture_key:
                raise ProductionContractError("closing snapshot belongs to another fixture")
            if self.closing_snapshot.kind is not MarketSnapshotKind.CLOSING:
                raise ProductionContractError("closing comparison requires a closing snapshot")
        if self.inference_success and not self.signal_time_success:
            raise ProductionContractError("inference cannot succeed without signal-time success")
        if self.inference_success and not self.probabilities:
            raise ProductionContractError("successful inference requires probabilities")
        for name, value in self.probabilities.items():
            if not name.strip() or not isfinite(float(value)) or not 0 <= float(value) <= 1:
                raise ProductionContractError("shadow probabilities must be finite values in [0, 1]")
        if self.actual_outcome is not None and not self.actual_outcome.strip():
            raise ProductionContractError("actual outcome cannot be blank")


@dataclass(frozen=True)
class ShadowPerformanceReport:
    """Aggregated shadow metrics, including benchmark-only closing diagnostics."""

    observation_count: int
    fixture_coverage: float
    inference_coverage: float
    valid_prediction_rate: float
    signal_time_success_rate: float
    odds_freshness_rate: float
    inference_latency_ms: float
    provider_latency_ms: float
    retry_count: int
    fallback_count: int
    stale_rejection_count: int
    duplicate_suppression_count: int
    candidate_availability_rate: float
    provider_failure_rate: float
    invalid_prediction_rate: float
    inference_error_rate: float
    brier_score: float | None
    log_loss: float | None
    calibration_error: float | None
    market_comparison_count: int
    closing_comparison_count: int
    clv_style_diagnostic: float | None
    provenance_complete: bool
    no_bet_enforced: bool
    closing_excluded: bool
    cross_league_isolated: bool

    @property
    def stale_rate(self) -> float:
        return 1.0 - self.odds_freshness_rate

    @property
    def duplicate_rate(self) -> float:
        return self.duplicate_suppression_count / self.observation_count if self.observation_count else 0.0

    @property
    def fallback_rate(self) -> float:
        return self.fallback_count / self.observation_count if self.observation_count else 0.0

    def validate(self) -> None:
        if self.observation_count < 0:
            raise ProductionContractError("shadow observation count must be non-negative")
        rates = (
            self.fixture_coverage,
            self.inference_coverage,
            self.valid_prediction_rate,
            self.signal_time_success_rate,
            self.odds_freshness_rate,
            self.candidate_availability_rate,
            self.provider_failure_rate,
            self.invalid_prediction_rate,
            self.inference_error_rate,
        )
        if any(not isfinite(float(value)) or not 0 <= value <= 1 for value in rates):
            raise ProductionContractError("shadow performance rates must be in [0, 1]")
        counts = (
            self.retry_count,
            self.fallback_count,
            self.stale_rejection_count,
            self.duplicate_suppression_count,
            self.market_comparison_count,
            self.closing_comparison_count,
        )
        if any(count < 0 for count in counts):
            raise ProductionContractError("shadow performance counts must be non-negative")
        if self.clv_style_diagnostic is not None and not isfinite(float(self.clv_style_diagnostic)):
            raise ProductionContractError("CLV-style diagnostic must be finite")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "observations": self.observation_count,
            "fixture_coverage": self.fixture_coverage,
            "inference_coverage": self.inference_coverage,
            "valid_prediction_rate": self.valid_prediction_rate,
            "signal_time_success_rate": self.signal_time_success_rate,
            "odds_freshness_rate": self.odds_freshness_rate,
            "inference_latency_ms": self.inference_latency_ms,
            "provider_latency_ms": self.provider_latency_ms,
            "retry_count": self.retry_count,
            "fallback_count": self.fallback_count,
            "stale_rejection_count": self.stale_rejection_count,
            "stale_rate": self.stale_rate,
            "duplicate_suppression_count": self.duplicate_suppression_count,
            "duplicate_rate": self.duplicate_rate,
            "candidate_availability_rate": self.candidate_availability_rate,
            "provider_failure_rate": self.provider_failure_rate,
            "invalid_prediction_rate": self.invalid_prediction_rate,
            "inference_error_rate": self.inference_error_rate,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
            "calibration_error": self.calibration_error,
            "market_comparison_count": self.market_comparison_count,
            "closing_comparison_count": self.closing_comparison_count,
            "clv_style_diagnostic": self.clv_style_diagnostic,
            "provenance_complete": self.provenance_complete,
            "no_bet_enforced": self.no_bet_enforced,
            "closing_excluded": self.closing_excluded,
            "cross_league_isolated": self.cross_league_isolated,
        }


def measure_shadow_performance(observations: Sequence[ShadowObservation]) -> ShadowPerformanceReport:
    """Compute shadow metrics without using closing odds for prediction."""

    items = tuple(observations)
    for item in items:
        item.validate()
    count = len(items)
    signal_successes = sum(item.signal_time_success for item in items)
    inference_successes = sum(item.inference_success for item in items)
    valid_predictions = sum(item.prediction_valid for item in items)
    fresh = sum(item.signal_snapshot is not None and not item.stale_rejected for item in items)
    brier_values: list[float] = []
    log_loss_values: list[float] = []
    calibration_pairs: list[tuple[float, bool]] = []
    for item in items:
        if item.inference_success and item.prediction_valid and item.actual_outcome is not None:
            probability = float(item.probabilities.get(item.actual_outcome, 0.0))
            brier_values.append(sum(
                (float(value) - (1.0 if name == item.actual_outcome else 0.0)) ** 2
                for name, value in item.probabilities.items()
            ) / max(len(item.probabilities), 1))
            log_loss_values.append(-log(max(probability, 1e-15)))
            for name, value in item.probabilities.items():
                calibration_pairs.append((float(value), name == item.actual_outcome))
    calibration_error = None
    if calibration_pairs:
        calibration_error = sum(abs(probability - outcome) for probability, outcome in calibration_pairs) / len(calibration_pairs)
    comparisons: list[float] = []
    for item in items:
        if item.signal_snapshot is None or item.closing_snapshot is None:
            continue
        shared_markets = set(item.signal_snapshot.odds).intersection(item.closing_snapshot.odds)
        comparisons.extend(
            float(item.closing_snapshot.odds[market]) - float(item.signal_snapshot.odds[market])
            for market in shared_markets
        )
    report = ShadowPerformanceReport(
        observation_count=count,
        fixture_coverage=sum(item.fixture_discovered for item in items) / count if count else 0.0,
        inference_coverage=inference_successes / count if count else 0.0,
        valid_prediction_rate=valid_predictions / count if count else 0.0,
        signal_time_success_rate=signal_successes / count if count else 0.0,
        odds_freshness_rate=fresh / count if count else 0.0,
        inference_latency_ms=sum(item.inference_latency_ms for item in items) / count if count else 0.0,
        provider_latency_ms=sum(item.provider_latency_ms for item in items) / count if count else 0.0,
        retry_count=sum(item.retry_count for item in items),
        fallback_count=sum(item.fallback_used for item in items),
        stale_rejection_count=sum(item.stale_rejected for item in items),
        duplicate_suppression_count=sum(item.duplicate_suppressed for item in items),
        candidate_availability_rate=sum(item.candidate_available for item in items) / count if count else 0.0,
        provider_failure_rate=sum(item.provider_failed for item in items) / count if count else 0.0,
        invalid_prediction_rate=sum(not item.prediction_valid for item in items) / count if count else 0.0,
        inference_error_rate=sum(item.signal_time_success and not item.inference_success for item in items) / count if count else 0.0,
        brier_score=sum(brier_values) / len(brier_values) if brier_values else None,
        log_loss=sum(log_loss_values) / len(log_loss_values) if log_loss_values else None,
        calibration_error=calibration_error,
        market_comparison_count=len(comparisons),
        closing_comparison_count=sum(
            bool(item.signal_snapshot and item.closing_snapshot) for item in items
        ),
        clv_style_diagnostic=sum(comparisons) / len(comparisons) if comparisons else None,
        provenance_complete=all(item.provenance_complete for item in items),
        no_bet_enforced=all(item.no_bet for item in items),
        closing_excluded=all(
            item.signal_snapshot is None or item.signal_snapshot.kind is MarketSnapshotKind.SIGNAL_TIME
            for item in items
        ),
        cross_league_isolated=all(not item.cross_league_collision for item in items),
    )
    report.validate()
    return report


@dataclass(frozen=True)
class ShadowPerformanceGateConfig:
    """Explicit operational thresholds; no profitability threshold is present."""

    minimum_fixture_coverage: float = 0.0
    minimum_inference_coverage: float = 0.0
    maximum_stale_rate: float = 1.0
    maximum_provider_failure_rate: float = 1.0
    maximum_duplicate_rate: float = 1.0
    maximum_invalid_prediction_rate: float = 1.0
    maximum_inference_error_rate: float = 1.0
    require_provenance: bool = True
    require_no_bet: bool = True
    require_closing_exclusion: bool = True
    require_cross_league_isolation: bool = True

    def validate(self) -> None:
        rates = (
            self.minimum_fixture_coverage,
            self.minimum_inference_coverage,
            self.maximum_stale_rate,
            self.maximum_provider_failure_rate,
            self.maximum_duplicate_rate,
            self.maximum_invalid_prediction_rate,
            self.maximum_inference_error_rate,
        )
        if any(not isfinite(float(value)) or not 0 <= value <= 1 for value in rates):
            raise ProductionContractError("shadow gate thresholds must be in [0, 1]")


@dataclass(frozen=True)
class ShadowGateCheck:
    name: str
    passed: bool
    observed: float | bool
    threshold: float | bool | None


@dataclass(frozen=True)
class ShadowPerformanceGateResult:
    passed: bool
    checks: tuple[ShadowGateCheck, ...]
    safe_to_activate: bool = False

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)

    def validate(self) -> None:
        if self.safe_to_activate:
            raise ProductionContractError("shadow performance gates cannot authorize activation")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "passed": self.passed,
            "safe_to_activate": False,
            "failures": list(self.failures),
            "checks": [
                {
                    "name": check.name,
                    "passed": check.passed,
                    "observed": check.observed,
                    "threshold": check.threshold,
                }
                for check in self.checks
            ],
        }


def evaluate_shadow_performance_gate(
    report: ShadowPerformanceReport,
    config: ShadowPerformanceGateConfig | None = None,
) -> ShadowPerformanceGateResult:
    """Evaluate explicit shadow gates; a pass is evidence, never activation."""

    report.validate()
    config = config or ShadowPerformanceGateConfig()
    config.validate()
    checks = (
        ShadowGateCheck("minimum_fixture_coverage", report.fixture_coverage >= config.minimum_fixture_coverage, report.fixture_coverage, config.minimum_fixture_coverage),
        ShadowGateCheck("minimum_inference_coverage", report.inference_coverage >= config.minimum_inference_coverage, report.inference_coverage, config.minimum_inference_coverage),
        ShadowGateCheck("maximum_stale_rate", report.stale_rate <= config.maximum_stale_rate, report.stale_rate, config.maximum_stale_rate),
        ShadowGateCheck("maximum_provider_failure_rate", report.provider_failure_rate <= config.maximum_provider_failure_rate, report.provider_failure_rate, config.maximum_provider_failure_rate),
        ShadowGateCheck("maximum_duplicate_rate", report.duplicate_rate <= config.maximum_duplicate_rate, report.duplicate_rate, config.maximum_duplicate_rate),
        ShadowGateCheck("maximum_invalid_prediction_rate", report.invalid_prediction_rate <= config.maximum_invalid_prediction_rate, report.invalid_prediction_rate, config.maximum_invalid_prediction_rate),
        ShadowGateCheck("maximum_inference_error_rate", report.inference_error_rate <= config.maximum_inference_error_rate, report.inference_error_rate, config.maximum_inference_error_rate),
        ShadowGateCheck("provenance_complete", report.provenance_complete, report.provenance_complete, True),
        ShadowGateCheck("no_bet_enforced", report.no_bet_enforced, report.no_bet_enforced, True),
        ShadowGateCheck("closing_exclusion", report.closing_excluded, report.closing_excluded, True),
        ShadowGateCheck("cross_league_isolation", report.cross_league_isolated, report.cross_league_isolated, True),
    )
    selected = tuple(check for check in checks if (
        check.name == "provenance_complete" and config.require_provenance
    ) or (
        check.name == "no_bet_enforced" and config.require_no_bet
    ) or (
        check.name == "closing_exclusion" and config.require_closing_exclusion
    ) or (
        check.name == "cross_league_isolation" and config.require_cross_league_isolation
    ) or check.name not in {
        "provenance_complete", "no_bet_enforced", "closing_exclusion", "cross_league_isolation"
    })
    result = ShadowPerformanceGateResult(all(check.passed for check in selected), selected)
    result.validate()
    return result


evaluate_shadow_gate = evaluate_shadow_performance_gate

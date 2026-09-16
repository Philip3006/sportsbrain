"""Safe comparison metrics for independently collected provider observations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from src.football.production_contracts import Fixture, ProductionContractError, _utc
from src.football.provider_cascade.adapters import AdapterResult
from src.football.provider_cascade.contracts import (
    ObservationCompleteness,
    ProviderState,
)


@dataclass(frozen=True)
class ProviderComparisonMetric:
    """One provider sample; never used as routing or authority policy."""

    provider: str
    state: ProviderState
    network_called: bool
    coverage: bool
    latency_ms: int
    odds_age_seconds: int | None
    bookmaker_available: bool
    odds_complete: bool
    fixture_match: bool
    rejection_rate: float
    provider_error: str | None
    quota_consumed: int | None

    def as_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "state": self.state.value,
            "network_called": self.network_called,
            "coverage": self.coverage,
            "latency_ms": self.latency_ms,
            "odds_age_seconds": self.odds_age_seconds,
            "bookmaker_available": self.bookmaker_available,
            "odds_complete": self.odds_complete,
            "fixture_match": self.fixture_match,
            "rejection_rate": self.rejection_rate,
            "provider_error": self.provider_error,
            "quota_consumed": self.quota_consumed,
        }


@dataclass(frozen=True)
class ProviderComparisonReport:
    """Comparison evidence from separately supplied provider results."""

    fixture_key: str
    captured_at: datetime
    metrics: tuple[ProviderComparisonMetric, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "captured_at", _utc(self.captured_at, "captured_at"))
        if not self.fixture_key.strip():
            raise ProductionContractError("comparison requires a fixture key")

    def as_payload(self) -> dict[str, object]:
        return {
            "fixture_key": self.fixture_key,
            "captured_at": self.captured_at.isoformat(),
            "metrics": [metric.as_payload() for metric in self.metrics],
            "routing_authority": "none",
        }


def compare_provider_results(
    fixture: Fixture,
    results: Mapping[str, AdapterResult],
    *,
    now: datetime | None = None,
) -> ProviderComparisonReport:
    """Build non-authoritative metrics from independently collected results.

    This function does not call providers, fan out, choose a provider, or
    override the cascade.  It is intended for controlled comparison fixtures.
    """

    fixture.validate()
    captured_at = _utc(now or datetime.now(timezone.utc), "comparison now")
    metrics: list[ProviderComparisonMetric] = []
    for provider, result in sorted(results.items()):
        if not provider.strip():
            raise ProductionContractError("comparison provider name is blank")
        result.validate()
        observation = result.observation
        fixture_match = False
        bookmaker_available = False
        odds_complete = False
        odds_age_seconds: int | None = None
        quota_consumed: int | None = None
        if observation is not None:
            observation.validate(require_fresh=False)
            fixture_match = (
                observation.provider_identity == provider
                and observation.fixture_key == fixture.fixture_key
                and observation.league_code == fixture.league_code
                and observation.home_team == fixture.home_team
                and observation.away_team == fixture.away_team
                and observation.kickoff_utc == fixture.kickoff
            )
            bookmaker_available = bool(observation.bookmaker_identity.strip())
            odds_complete = observation.completeness is ObservationCompleteness.COMPLETE
            if observation.source_timestamp is not None:
                age = (captured_at - observation.source_timestamp).total_seconds()
                if age >= 0:
                    odds_age_seconds = round(age)
            before = observation.quota_state_before.remaining
            after = observation.quota_state_after.remaining
            if before is not None and after is not None:
                quota_consumed = max(0, before - after)
        coverage = (
            result.state is ProviderState.AVAILABLE
            and observation is not None
            and fixture_match
            and bookmaker_available
            and odds_complete
            and (
                odds_age_seconds is not None
                or observation.source_timing_provenance.value == "CAPTURE_TIME_ONLY"
            )
        )
        metric_state = (
            result.state
            if result.state is not ProviderState.AVAILABLE or coverage
            else ProviderState.QUALITY_REJECTED
        )
        metrics.append(
            ProviderComparisonMetric(
                provider=provider,
                state=metric_state,
                network_called=result.network_called,
                coverage=coverage,
                latency_ms=result.latency_ms,
                odds_age_seconds=odds_age_seconds,
                bookmaker_available=bookmaker_available,
                odds_complete=odds_complete,
                fixture_match=fixture_match,
                rejection_rate=0.0 if coverage else 1.0,
                provider_error=None if coverage else result.reason,
                quota_consumed=quota_consumed,
            )
        )
    return ProviderComparisonReport(fixture.fixture_key, captured_at, tuple(metrics))

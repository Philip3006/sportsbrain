"""In-memory operational observability for future Top-5 production."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from src.football.production_contracts import (
    ActivationMode,
    ProductionContractError,
    _utc,
)


@dataclass(frozen=True)
class Top5OperationalHealth:
    """A disabled-by-default health snapshot with no writer side effect."""

    league_code: str
    provider_health: str
    fixture_coverage: float
    odds_freshness: float
    signal_time_coverage: float
    inference_health: str
    publisher_health: str
    result_source_health: str
    fallback_rate: float
    quota_cost_usage: float
    stale_rate: float
    retry_rate: float
    duplicate_suppression_count: int
    last_successful_cycle: datetime | None
    activation_state: str = "disabled"
    activation_mode: ActivationMode = ActivationMode.DISABLED
    no_bet: bool = True
    registered: bool = False
    health_identity: str = ""
    fixture_count: int = 0
    provider_request_count: int = 0

    def __post_init__(self) -> None:
        if not self.health_identity:
            object.__setattr__(self, "health_identity", f"top5_shadow:{self.league_code}")
        if self.last_successful_cycle is not None:
            object.__setattr__(self, "last_successful_cycle", _utc(self.last_successful_cycle, "last_successful_cycle"))

    def validate(self) -> None:
        if not self.league_code.strip() or not self.health_identity.strip():
            raise ProductionContractError("operational health requires league identity")
        rates = (
            self.fixture_coverage,
            self.odds_freshness,
            self.signal_time_coverage,
            self.fallback_rate,
            self.stale_rate,
            self.retry_rate,
        )
        if any(not isfinite(float(value)) or not 0 <= value <= 1 for value in rates):
            raise ProductionContractError("operational health rates must be in [0, 1]")
        if not isfinite(float(self.quota_cost_usage)) or self.quota_cost_usage < 0:
            raise ProductionContractError("operational health quota/cost usage must be non-negative")
        if self.fixture_count < 0 or self.provider_request_count < 0 or self.duplicate_suppression_count < 0:
            raise ProductionContractError("operational health counts must be non-negative")
        if ActivationMode(self.activation_mode) is not ActivationMode.DISABLED:
            raise ProductionContractError("Top-5 readiness health must remain disabled")
        if self.activation_state != "disabled" or self.registered or not self.no_bet:
            raise ProductionContractError("Top-5 operational health safety state is invalid")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "health_identity": self.health_identity,
            "league": self.league_code,
            "provider_health": self.provider_health,
            "fixture_count": self.fixture_count,
            "provider_request_count": self.provider_request_count,
            "fixture_coverage": self.fixture_coverage,
            "odds_freshness": self.odds_freshness,
            "signal_time_coverage": self.signal_time_coverage,
            "inference_health": self.inference_health,
            "publisher_health": self.publisher_health,
            "result_source_health": self.result_source_health,
            "fallback_rate": self.fallback_rate,
            "quota_cost_usage": self.quota_cost_usage,
            "stale_rate": self.stale_rate,
            "retry_rate": self.retry_rate,
            "duplicate_suppression": self.duplicate_suppression_count,
            "last_successful_cycle": self.last_successful_cycle.isoformat() if self.last_successful_cycle else None,
            "activation_state": "disabled",
            "activation_mode": ActivationMode.DISABLED.value,
            "no_bet": True,
            "registered": False,
        }


Top5ProductionHealth = Top5OperationalHealth
Top5ReadinessHealth = Top5OperationalHealth

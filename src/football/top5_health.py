"""Disabled, in-memory observability payloads for Top-5 shadow readiness."""
from __future__ import annotations

from dataclasses import dataclass

from src.football.production_contracts import ActivationMode, ProductionContractError


@dataclass(frozen=True)
class Top5ShadowHealth:
    league_code: str
    fixture_count: int
    eligible_count: int
    prediction_count: int
    skipped_count: int
    stale_count: int
    provider_failure_count: int
    retry_count: int
    duplicate_suppression_count: int
    model_adapter_identity: str
    contract_id: str
    no_bet: bool = True
    activation_mode: ActivationMode = ActivationMode.DISABLED
    logical_fixture_evaluations: int = 0
    bulk_provider_request_count: int = 0
    fallback_request_count: int = 0
    health_identity: str = ""

    def __post_init__(self) -> None:
        if not self.health_identity:
            object.__setattr__(self, "health_identity", f"top5_shadow:{self.league_code}")

    def validate(self) -> None:
        if any(count < 0 for count in (
            self.fixture_count,
            self.eligible_count,
            self.prediction_count,
            self.skipped_count,
            self.stale_count,
            self.provider_failure_count,
            self.retry_count,
            self.duplicate_suppression_count,
            self.logical_fixture_evaluations,
            self.bulk_provider_request_count,
            self.fallback_request_count,
        )):
            raise ProductionContractError("Top-5 health counts must be non-negative")
        if any(
            not value.strip()
            for value in (
                self.league_code,
                self.health_identity,
                self.model_adapter_identity,
                self.contract_id,
            )
        ):
            raise ProductionContractError("Top-5 health requires league, model, and contract identity")
        if not self.no_bet:
            raise ProductionContractError("Top-5 health must remain no-bet")
        if ActivationMode(self.activation_mode) is not ActivationMode.DISABLED:
            raise ProductionContractError("readiness health must remain disabled")
        if self.eligible_count > self.fixture_count or self.prediction_count > self.eligible_count:
            raise ProductionContractError("Top-5 health counts are inconsistent")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "league": self.league_code,
            "fixture_count": self.fixture_count,
            "eligible": self.eligible_count,
            "predictions": self.prediction_count,
            "skipped": self.skipped_count,
            "stale": self.stale_count,
            "provider_failures": self.provider_failure_count,
            "retries": self.retry_count,
            "duplicate_suppression": self.duplicate_suppression_count,
            "model_adapter_identity": self.model_adapter_identity,
            "health_identity": self.health_identity,
            "contract_id": self.contract_id,
            "logical_fixture_evaluations": self.logical_fixture_evaluations,
            "bulk_provider_requests": self.bulk_provider_request_count,
            "fallback_requests": self.fallback_request_count,
            "no_bet": True,
            "activation_mode": ActivationMode.DISABLED.value,
            "registered": False,
        }

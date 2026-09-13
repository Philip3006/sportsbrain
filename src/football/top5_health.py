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
        )):
            raise ProductionContractError("Top-5 health counts must be non-negative")
        if not self.league_code.strip() or not self.model_adapter_identity.strip() or not self.contract_id.strip():
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
            "contract_id": self.contract_id,
            "no_bet": True,
            "activation_mode": ActivationMode.DISABLED.value,
            "registered": False,
        }

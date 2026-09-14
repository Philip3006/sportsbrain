"""Backward-compatible, unpublished Top-5 PWA data contract."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite

from src.football.production_contracts import (
    ActivationMode,
    MarketSnapshotKind,
    ProductionContractError,
    _utc,
)


@dataclass(frozen=True)
class Top5PwaData:
    """The eventual PWA record, validated without changing frontend code."""

    league: str
    fixture: str
    kickoff: datetime
    probabilities: Mapping[str, float]
    model_identity: str
    confidence_metadata: Mapping[str, object] = field(default_factory=dict)
    market_snapshot_age_seconds: int = 0
    signal_timestamp: datetime | None = None
    status: str = "shadow"
    provenance: Mapping[str, str] = field(default_factory=dict)
    health_state: str = "disabled"
    activation_mode: ActivationMode = ActivationMode.DISABLED
    no_bet: bool = True
    publication_enabled: bool = False
    snapshot_kind: MarketSnapshotKind = MarketSnapshotKind.SIGNAL_TIME

    def __post_init__(self) -> None:
        object.__setattr__(self, "kickoff", _utc(self.kickoff, "kickoff"))
        if self.signal_timestamp is not None:
            object.__setattr__(self, "signal_timestamp", _utc(self.signal_timestamp, "signal_timestamp"))

    def validate(self) -> None:
        required = {
            "league": self.league,
            "fixture": self.fixture,
            "model_identity": self.model_identity,
            "status": self.status,
            "health_state": self.health_state,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ProductionContractError(f"PWA Top-5 contract lacks fields: {', '.join(missing)}")
        if self.market_snapshot_age_seconds < 0:
            raise ProductionContractError("PWA market snapshot age must be non-negative")
        if not self.probabilities:
            raise ProductionContractError("PWA Top-5 contract requires probabilities")
        if any(
            not name.strip() or not isfinite(float(value)) or not 0 <= float(value) <= 1
            for name, value in self.probabilities.items()
        ):
            raise ProductionContractError("PWA Top-5 probabilities are invalid")
        if self.snapshot_kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ProductionContractError("PWA prediction data cannot contain closing odds")
        if ActivationMode(self.activation_mode) is not ActivationMode.DISABLED:
            raise ProductionContractError("PWA Top-5 readiness payload must remain disabled")
        if not self.no_bet or self.publication_enabled:
            raise ProductionContractError("PWA Top-5 readiness payload must remain no-bet and unpublished")
        required_provenance = {"source_sha", "research_sha", "model_artifact_hash"}
        if not required_provenance.issubset(self.provenance):
            raise ProductionContractError("PWA provenance is incomplete")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "league": self.league,
            "fixture": self.fixture,
            "kickoff": self.kickoff.isoformat(),
            "probabilities": dict(self.probabilities),
            "model_identity": self.model_identity,
            "confidence_metadata": dict(self.confidence_metadata),
            "market_snapshot_age_seconds": self.market_snapshot_age_seconds,
            "signal_timestamp": self.signal_timestamp.isoformat() if self.signal_timestamp else None,
            "status": self.status,
            "provenance": dict(self.provenance),
            "health_state": self.health_state,
            "activation_mode": ActivationMode.DISABLED.value,
            "no_bet": True,
            "publication_enabled": False,
        }


PwaTop5Signal = Top5PwaData
Top5PwaSignal = Top5PwaData


def validate_top5_pwa_payload(payload: Top5PwaData) -> None:
    payload.validate()

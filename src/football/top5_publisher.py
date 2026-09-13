"""Disabled Top-5 publisher boundary for future controlled activation."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import MappingProxyType

from src.football.production_contracts import (
    ActivationMode,
    ArtifactOwner,
    ProductionContractError,
    _utc,
    validate_artifact_ownership,
)


@dataclass(frozen=True)
class Top5PublisherPayload:
    """A future publisher payload that can only be staged in shadow mode."""

    artifact_path: str
    league_code: str
    fixture_key: str
    candidate_id: str
    model_identity: str
    signal_id: str
    signal_generated_at: datetime
    source_sha: str
    research_sha: str
    model_artifact_hash: str
    probabilities: Mapping[str, float]
    snapshot_age_seconds: int
    activation_mode: ActivationMode = ActivationMode.SHADOW
    no_bet: bool = True
    publication_enabled: bool = False
    activation_gate_passed: bool = False
    provenance: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_generated_at", _utc(self.signal_generated_at, "signal_generated_at"))

    def validate(self) -> None:
        values = {
            "artifact_path": self.artifact_path,
            "league_code": self.league_code,
            "fixture_key": self.fixture_key,
            "candidate_id": self.candidate_id,
            "model_identity": self.model_identity,
            "signal_id": self.signal_id,
            "source_sha": self.source_sha,
            "research_sha": self.research_sha,
            "model_artifact_hash": self.model_artifact_hash,
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise ProductionContractError(f"Top-5 publisher provenance is incomplete: {', '.join(missing)}")
        validate_artifact_ownership(self.artifact_path, ArtifactOwner.STAGED_PUBLIC)
        if self.snapshot_age_seconds < 0:
            raise ProductionContractError("publisher snapshot age must be non-negative")
        if not self.probabilities:
            raise ProductionContractError("publisher payload requires probabilities")
        if any(
            not name.strip() or not isfinite(float(value)) or not 0 <= float(value) <= 1
            for name, value in self.probabilities.items()
        ):
            raise ProductionContractError("publisher probabilities are invalid")
        if ActivationMode(self.activation_mode) is not ActivationMode.SHADOW:
            raise ProductionContractError("Top-5 publisher remains shadow-only")
        if not self.no_bet or self.publication_enabled or self.activation_gate_passed:
            raise ProductionContractError("Top-5 publisher must remain no-bet and unpublished")
        required_provenance = {"source_sha", "research_sha", "model_artifact_hash"}
        if not required_provenance.issubset(self.provenance):
            raise ProductionContractError("publisher provenance map is incomplete")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "artifact_path": self.artifact_path,
            "league": self.league_code,
            "fixture": self.fixture_key,
            "candidate": self.candidate_id,
            "model": self.model_identity,
            "signal_id": self.signal_id,
            "signal_generated_at": self.signal_generated_at.isoformat(),
            "source_sha": self.source_sha,
            "research_sha": self.research_sha,
            "model_artifact_hash": self.model_artifact_hash,
            "probabilities": dict(self.probabilities),
            "snapshot_age_seconds": self.snapshot_age_seconds,
            "activation_mode": ActivationMode.SHADOW.value,
            "no_bet": True,
            "publication_enabled": False,
            "activation_gate_passed": False,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class StagedTop5Artifact:
    payload: Top5PublisherPayload
    overwritten_paths: tuple[str, ...] = ()
    published: bool = False

    def validate(self) -> None:
        self.payload.validate()
        if self.overwritten_paths or self.published:
            raise ProductionContractError("staged Top-5 artifact cannot overwrite or publish")


class Top5PublisherContract:
    """A no-write interface that validates a future payload and stages in memory."""

    def stage_shadow(self, payload: Top5PublisherPayload) -> StagedTop5Artifact:
        payload.validate()
        staged = StagedTop5Artifact(payload)
        staged.validate()
        return staged

    def publish(self, _payload: Top5PublisherPayload) -> None:
        raise ProductionContractError("Top-5 publication is disabled")


def validate_top5_publisher_payload(payload: Top5PublisherPayload) -> None:
    payload.validate()


Top5Publisher = Top5PublisherContract

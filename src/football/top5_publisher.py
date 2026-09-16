"""Top-5 shadow and controlled-publication boundaries.

The legacy ``Top5PublisherPayload`` remains shadow-only.  The controlled
publication seam below is deliberately an in-memory boundary: it validates a
separately authorized public artifact and atomically swaps an injected store,
but it never writes the checkout, Cloudflare, a scheduler, or the ledger.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from types import MappingProxyType

from src.football.production_contracts import (
    ActivationMode,
    ArtifactOwner,
    ProductionContractError,
    _utc,
    validate_artifact_ownership,
)
from src.notifications.public_serializer import serialize_public_product


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _hash(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) not in (40, 64) or any(
        char not in "0123456789abcdefABCDEF" for char in value
    ):
        raise ProductionContractError(f"{name} must be a hexadecimal digest")


def _required_text(values: Mapping[str, object], prefix: str) -> None:
    missing = [
        name
        for name, value in values.items()
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise ProductionContractError(f"{prefix} is incomplete: {', '.join(missing)}")


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


@dataclass(frozen=True)
class Top5PublicationAuthorization:
    """Caller-supplied publication approval, separate from activation approval."""

    publication_authorization_id: str
    activation_id: str
    league_code: str
    candidate_id: str
    model_identity: str
    source_sha: str
    research_sha: str
    model_artifact_hash: str
    signal_time_experiment_id: str
    publication_token: str
    issued_at: datetime
    expires_at: datetime
    publication_authorized: bool = True
    no_bet: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", _utc(self.issued_at, "issued_at"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))

    def validate(self, *, now: datetime | None = None) -> None:
        _required_text(
            {
                "publication_authorization_id": self.publication_authorization_id,
                "activation_id": self.activation_id,
                "league_code": self.league_code,
                "candidate_id": self.candidate_id,
                "model_identity": self.model_identity,
                "source_sha": self.source_sha,
                "research_sha": self.research_sha,
                "model_artifact_hash": self.model_artifact_hash,
                "signal_time_experiment_id": self.signal_time_experiment_id,
                "publication_token": self.publication_token,
            },
            "publication authorization",
        )
        for name, value in (
            ("source_sha", self.source_sha),
            ("research_sha", self.research_sha),
            ("model_artifact_hash", self.model_artifact_hash),
        ):
            _hash(value, name)
        if not self.publication_authorized:
            raise ProductionContractError("publication authorization is not approved")
        if not self.no_bet:
            raise ProductionContractError("publication authorization cannot enable betting")
        if self.expires_at <= self.issued_at:
            raise ProductionContractError("publication authorization expiry is invalid")
        if now is not None and not self.issued_at <= _utc(now, "now") <= self.expires_at:
            raise ProductionContractError("publication authorization is expired or not yet valid")

    def binds(self, payload: ControlledTop5PublicationPayload) -> None:
        self.validate()
        for name in (
            "activation_id",
            "league_code",
            "candidate_id",
            "model_identity",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "signal_time_experiment_id",
        ):
            if getattr(self, name) != getattr(payload, name):
                raise ProductionContractError(f"publication binding mismatch: {name}")


@dataclass(frozen=True)
class ControlledTop5PublicationPayload:
    """A public football artifact bound to an already active controlled run."""

    artifact_path: str
    activation_id: str
    league_code: str
    candidate_id: str
    model_identity: str
    signal_time_experiment_id: str
    source_sha: str
    research_sha: str
    model_artifact_hash: str
    provider_authority: str
    result_authority: str
    evidence_digest: str
    generated_at: datetime
    football_records: tuple[Mapping[str, object], ...]
    health: Mapping[str, object]
    activation_mode: ActivationMode = ActivationMode.CONTROLLED
    no_bet: bool = True
    publication_enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", _utc(self.generated_at, "generated_at"))
        object.__setattr__(self, "football_records", tuple(self.football_records))

    def validate(self) -> None:
        _required_text(
            {
                "artifact_path": self.artifact_path,
                "activation_id": self.activation_id,
                "league_code": self.league_code,
                "candidate_id": self.candidate_id,
                "model_identity": self.model_identity,
                "signal_time_experiment_id": self.signal_time_experiment_id,
                "source_sha": self.source_sha,
                "research_sha": self.research_sha,
                "model_artifact_hash": self.model_artifact_hash,
                "provider_authority": self.provider_authority,
                "result_authority": self.result_authority,
                "evidence_digest": self.evidence_digest,
            },
            "controlled publication artifact",
        )
        validate_artifact_ownership(self.artifact_path, ArtifactOwner.CONTROLLED_PUBLIC)
        for name, value in (
            ("source_sha", self.source_sha),
            ("research_sha", self.research_sha),
            ("model_artifact_hash", self.model_artifact_hash),
            ("evidence_digest", self.evidence_digest),
        ):
            _hash(value, name)
        if ActivationMode(self.activation_mode) is not ActivationMode.CONTROLLED:
            raise ProductionContractError("controlled publication requires controlled activation mode")
        if not self.no_bet or not self.publication_enabled:
            raise ProductionContractError("controlled publication must remain no-bet and explicitly published")
        if not self.football_records:
            raise ProductionContractError("controlled publication requires football records")
        for index, record in enumerate(self.football_records):
            if not isinstance(record, Mapping):
                raise ProductionContractError(f"football record {index} is not an object")
            _required_text(
                {
                    "fixture": record.get("fixture"),
                    "league": record.get("league"),
                    "model_identity": record.get("model_identity"),
                    "source_sha": record.get("source_sha"),
                    "research_sha": record.get("research_sha"),
                    "signal_time_experiment_id": record.get("signal_time_experiment_id"),
                    "activation_id": record.get("activation_id"),
                    "evidence_digest": record.get("evidence_digest"),
                },
                f"football record {index}",
            )
            if record.get("league") != self.league_code:
                raise ProductionContractError("football record league binding mismatch")
            for name in (
                "model_identity",
                "source_sha",
                "research_sha",
                "signal_time_experiment_id",
                "activation_id",
                "evidence_digest",
            ):
                if record.get(name) != getattr(self, name):
                    raise ProductionContractError(f"football record binding mismatch: {name}")
            if record.get("no_bet") is not True:
                raise ProductionContractError("public football record must remain no-bet")
            if record.get("closing_used_for_prediction") is True:
                raise ProductionContractError("closing odds cannot enter public prediction output")
            probabilities = record.get("probabilities")
            if not isinstance(probabilities, Mapping) or not probabilities:
                raise ProductionContractError("public football record requires probabilities")
            values = tuple(float(value) for value in probabilities.values())
            if any(not isfinite(value) or value < 0 or value > 1 for value in values):
                raise ProductionContractError("public football probabilities are invalid")
            if abs(sum(values) - 1.0) > 1e-6:
                raise ProductionContractError("public football probabilities must sum to one")
        if not isinstance(self.health, Mapping):
            raise ProductionContractError("controlled publication requires health data")
        if self.health.get("activation_state") != "controlled":
            raise ProductionContractError("health is not bound to controlled activation")
        if self.health.get("no_bet") is not True:
            raise ProductionContractError("public health must remain no-bet")

    def as_public_product(self) -> dict[str, object]:
        self.validate()
        return serialize_public_product(
            {
                "updated": self.generated_at.isoformat(),
                "football": [dict(record) for record in self.football_records],
                "health": {
                    **dict(self.health),
                    "top5_activation_id": self.activation_id,
                    "top5_league": self.league_code,
                    "top5_candidate_id": self.candidate_id,
                    "top5_model_identity": self.model_identity,
                    "top5_source_sha": self.source_sha,
                    "top5_research_sha": self.research_sha,
                    "top5_model_artifact_hash": self.model_artifact_hash,
                    "top5_provider_authority": self.provider_authority,
                    "top5_result_authority": self.result_authority,
                    "top5_evidence_digest": self.evidence_digest,
                    "publication_status": "PUBLISHED",
                    "publication_enabled": True,
                },
            }
        )


@dataclass(frozen=True)
class PublishedTop5Artifact:
    payload: ControlledTop5PublicationPayload
    public_product: Mapping[str, object]
    artifact_digest: str
    published_at: datetime

    def validate(self) -> None:
        self.payload.validate()
        _utc(self.published_at, "published_at")
        if self.artifact_digest != _digest(self.public_product):
            raise ProductionContractError("published Top-5 artifact digest mismatch")


@dataclass(frozen=True)
class PublicationRollback:
    restored_unpublished: bool
    previous_safe_artifact_digest: str | None
    active_artifact_digest: str | None = None
    scheduler_enabled: bool = False
    ledger_mutated: bool = False

    def validate(self) -> None:
        if not self.restored_unpublished or self.active_artifact_digest is not None:
            raise ProductionContractError("publication rollback did not restore unpublished state")
        if self.scheduler_enabled or self.ledger_mutated:
            raise ProductionContractError("publication rollback safety state is invalid")


class InMemoryTop5PublicationStore:
    """Atomic, side-effect-free publication store used by production adapters."""

    def __init__(self) -> None:
        self._current: PublishedTop5Artifact | None = None

    @property
    def current(self) -> PublishedTop5Artifact | None:
        return self._current

    def publish(
        self,
        payload: ControlledTop5PublicationPayload,
        authorization: Top5PublicationAuthorization,
        *,
        activation_bindings: Mapping[str, object],
        now: datetime,
    ) -> PublishedTop5Artifact:
        authorization.validate(now=now)
        payload.validate()
        authorization.binds(payload)
        if activation_bindings.get("active") is not True:
            raise ProductionContractError("publication requires the exact active controlled activation")
        for name in (
            "activation_id",
            "league_code",
            "candidate_id",
            "model_identity",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "signal_time_experiment_id",
            "provider_authority",
            "result_authority",
            "evidence_digest",
        ):
            expected = activation_bindings.get(name)
            actual = payload.activation_id if name == "activation_id" else getattr(payload, name, None)
            if actual != expected:
                raise ProductionContractError(f"publication activation binding mismatch: {name}")
        if self._current is not None and payload.generated_at <= self._current.payload.generated_at:
            raise ProductionContractError("stale Top-5 publication artifact rejected")
        public_product = payload.as_public_product()
        artifact = PublishedTop5Artifact(
            payload=payload,
            public_product=public_product,
            artifact_digest=_digest(public_product),
            published_at=_utc(now, "published_at"),
        )
        artifact.validate()
        self._current = artifact
        return artifact

    def rollback(self) -> PublicationRollback:
        previous = self._current.artifact_digest if self._current is not None else None
        self._current = None
        result = PublicationRollback(True, previous)
        result.validate()
        return result


class Top5PublisherContract:
    """A no-write interface that validates a future payload and stages in memory."""

    def stage_shadow(self, payload: Top5PublisherPayload) -> StagedTop5Artifact:
        payload.validate()
        staged = StagedTop5Artifact(payload)
        staged.validate()
        return staged

    def publish(self, _payload: Top5PublisherPayload) -> None:
        raise ProductionContractError("Top-5 publication is disabled")

    def publish_controlled(
        self,
        payload: ControlledTop5PublicationPayload,
        authorization: Top5PublicationAuthorization,
        *,
        store: InMemoryTop5PublicationStore,
        activation_bindings: Mapping[str, object],
        now: datetime,
    ) -> PublishedTop5Artifact:
        """Publish only through an injected atomic boundary after activation."""

        return store.publish(
            payload,
            authorization,
            activation_bindings=activation_bindings,
            now=now,
        )


def validate_top5_publisher_payload(payload: Top5PublisherPayload) -> None:
    payload.validate()


Top5Publisher = Top5PublisherContract

"""Top-5 shadow and controlled-publication boundaries.

The legacy ``Top5PublisherPayload`` remains shadow-only.  The controlled
publication seam below is deliberately an in-memory boundary: it validates a
separately authorized public artifact and atomically swaps an injected store,
but it never writes the checkout, Cloudflare, a scheduler, or the ledger.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import secrets
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from hmac import compare_digest
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from src.football.production_contracts import (
    ActivationMode,
    ArtifactOwner,
    ProductionContractError,
    _utc,
    validate_artifact_ownership,
)
from src.notifications.public_serializer import serialize_public_product

CONTROLLED_PUBLICATION_CAPABILITY_STATE = (
    "football/top5/controlled_publication_capability.json"
)


def controlled_publication_capability_state_path() -> Path:
    """Return the sole operator-owned state location for publication capability."""

    try:
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise ProductionContractError(
            "controlled publication capability operator account is unavailable"
        ) from exc
    if not account_home.is_absolute():
        raise ProductionContractError(
            "controlled publication capability operator account path is invalid"
        )
    return (
        account_home
        / "Library"
        / "Application Support"
        / "SportsBrain"
        / "runtime-state"
        / CONTROLLED_PUBLICATION_CAPABILITY_STATE
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _hash(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) not in (40, 64)
        or any(char not in "0123456789abcdefABCDEF" for char in value)
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
        object.__setattr__(
            self,
            "signal_generated_at",
            _utc(self.signal_generated_at, "signal_generated_at"),
        )

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
            raise ProductionContractError(
                f"Top-5 publisher provenance is incomplete: {', '.join(missing)}"
            )
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
            raise ProductionContractError(
                "Top-5 publisher must remain no-bet and unpublished"
            )
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
            raise ProductionContractError(
                "staged Top-5 artifact cannot overwrite or publish"
            )


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
            raise ProductionContractError(
                "publication authorization cannot enable betting"
            )
        if self.expires_at <= self.issued_at:
            raise ProductionContractError("publication authorization expiry is invalid")
        if (
            now is not None
            and not self.issued_at <= _utc(now, "now") <= self.expires_at
        ):
            raise ProductionContractError(
                "publication authorization is expired or not yet valid"
            )

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
    controlled_shadow_run_id: str
    qualification_session_id: str
    generated_at: datetime
    football_records: tuple[Mapping[str, object], ...]
    health: Mapping[str, object]
    activation_mode: ActivationMode = ActivationMode.CONTROLLED
    no_bet: bool = True
    publication_enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "generated_at", _utc(self.generated_at, "generated_at")
        )
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
                "controlled_shadow_run_id": self.controlled_shadow_run_id,
                "qualification_session_id": self.qualification_session_id,
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
            raise ProductionContractError(
                "controlled publication requires controlled activation mode"
            )
        if not self.no_bet or not self.publication_enabled:
            raise ProductionContractError(
                "controlled publication must remain no-bet and explicitly published"
            )
        if not self.football_records:
            raise ProductionContractError(
                "controlled publication requires football records"
            )
        for index, record in enumerate(self.football_records):
            if not isinstance(record, Mapping):
                raise ProductionContractError(
                    f"football record {index} is not an object"
                )
            fixture = record.get("fixture")
            fixture_key = record.get("fixture_key")
            if isinstance(fixture, Mapping):
                fixture_key = fixture.get("fixture_key") or fixture.get("fixture_id")
            else:
                fixture_key = fixture_key or fixture
            _required_text(
                {
                    "fixture": fixture_key,
                    "league": record.get("league"),
                    "model_identity": record.get("model_identity"),
                    "source_sha": record.get("source_sha"),
                    "research_sha": record.get("research_sha"),
                    "signal_time_experiment_id": record.get(
                        "signal_time_experiment_id"
                    ),
                    "activation_id": record.get("activation_id"),
                    "evidence_digest": record.get("evidence_digest"),
                    "controlled_shadow_run_id": record.get("controlled_shadow_run_id"),
                    "qualification_session_id": record.get("qualification_session_id"),
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
                "controlled_shadow_run_id",
                "qualification_session_id",
            ):
                if record.get(name) != getattr(self, name):
                    raise ProductionContractError(
                        f"football record binding mismatch: {name}"
                    )
            if record.get("no_bet") is not True:
                raise ProductionContractError(
                    "public football record must remain no-bet"
                )
            if record.get("closing_used_for_prediction") is True:
                raise ProductionContractError(
                    "closing odds cannot enter public prediction output"
                )
            probabilities = record.get("probabilities")
            if not isinstance(probabilities, Mapping) or not probabilities:
                raise ProductionContractError(
                    "public football record requires probabilities"
                )
            values = tuple(float(value) for value in probabilities.values())
            if any(not isfinite(value) or value < 0 or value > 1 for value in values):
                raise ProductionContractError(
                    "public football probabilities are invalid"
                )
            if abs(sum(values) - 1.0) > 1e-6:
                raise ProductionContractError(
                    "public football probabilities must sum to one"
                )
        if not isinstance(self.health, Mapping):
            raise ProductionContractError("controlled publication requires health data")
        if self.health.get("activation_state") != "controlled":
            raise ProductionContractError(
                "health is not bound to controlled activation"
            )
        if self.health.get("no_bet") is not True:
            raise ProductionContractError("public health must remain no-bet")

    def as_public_product(self) -> dict[str, object]:
        self.validate()
        from src.notifications.public_serializer import (
            map_prediction_to_public_football_signals,
        )

        public_records: list[dict[str, object]] = []
        for index, record in enumerate(self.football_records):
            fixture = record.get("fixture")
            fixture_data = dict(fixture) if isinstance(fixture, Mapping) else {}
            fixture_key = (
                record.get("fixture_key")
                or fixture_data.get("fixture_key")
                or fixture_data.get("fixture_id")
                or fixture
            )
            prediction_id = record.get("prediction_id") or (
                f"top5-prediction:{_digest((self.activation_id, fixture_key, index))[:32]}"
            )
            signal_timestamp = (
                record.get("signal_timestamp")
                or record.get("captured_at")
                or self.generated_at.isoformat()
            )
            envelope = {
                "record_type": "prediction_artifact",
                "prediction_artifact": {
                    "league_code": self.league_code,
                    "fixture_key": fixture_key,
                    "prediction_id": prediction_id,
                    "model_identity": self.model_identity,
                    "model_version": record.get("model_version") or self.model_identity,
                    "prediction_timestamp": record.get("prediction_timestamp")
                    or self.generated_at.isoformat(),
                    "probabilities": dict(record["probabilities"]),
                    "signal_timestamp": signal_timestamp,
                    "snapshot_id": record.get("snapshot_id")
                    or self.signal_time_experiment_id,
                    "snapshot_kind": "SIGNAL_TIME",
                    "odds": record.get("odds", {}),
                },
                "fixture": {
                    **fixture_data,
                    "fixture_key": fixture_key,
                },
                "provenance": {
                    "source": self.provider_authority,
                    "provider": self.provider_authority,
                    "source_sha": self.source_sha,
                    "research_sha": self.research_sha,
                    "model_artifact_hash": self.model_artifact_hash,
                    "snapshot_id": record.get("snapshot_id")
                    or self.signal_time_experiment_id,
                    "snapshot_kind": "SIGNAL_TIME",
                    "captured_at": signal_timestamp,
                },
                "activation_state": "controlled",
                "publication_status": "PUBLISHED",
                "publication_enabled": True,
                "no_bet": True,
                "result_status": record.get("result_status") or "PENDING",
                "run_id": record.get("controlled_shadow_run_id")
                or self.controlled_shadow_run_id,
                "session_id": record.get("qualification_session_id")
                or self.qualification_session_id,
            }
            public_records.extend(map_prediction_to_public_football_signals(envelope))
        return serialize_public_product(
            {
                "updated": self.generated_at.isoformat(),
                "football": public_records,
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


_CONTROLLED_PUBLICATION_BINDING_FIELDS = (
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
    "controlled_shadow_run_id",
    "qualification_session_id",
)


def _controlled_binding_values(
    values: Mapping[str, object],
) -> dict[str, object]:
    return {name: values.get(name) for name in _CONTROLLED_PUBLICATION_BINDING_FIELDS}


def _publication_authorization_values(
    values: Mapping[str, object],
) -> dict[str, object]:
    return {
        name: values.get(name)
        for name in (
            "publication_authorization_id",
            "activation_id",
            "league_code",
            "candidate_id",
            "model_identity",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "signal_time_experiment_id",
            "publication_authorized",
            "no_bet",
        )
    }


@dataclass(frozen=True)
class ControlledPublicationAttestation:
    """Detached, fail-closed proof for the controlled runtime gate.

    This is not an activation or publication authorization.  It is issued only
    after both have already been validated and binds the exact artifact hash to
    the active activation and the separate publication authorization.
    """

    artifact_path: str
    artifact_hash: str
    activation_id: str
    league_code: str
    candidate_id: str
    model_identity: str
    source_sha: str
    research_sha: str
    model_artifact_hash: str
    signal_time_experiment_id: str
    provider_authority: str
    result_authority: str
    evidence_digest: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    activation_binding_digest: str
    publication_authorization_id: str
    publication_authorization_digest: str
    active_activation: bool
    publication_authorized: bool
    no_bet: bool
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", _utc(self.issued_at, "issued_at"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))

    @classmethod
    def issue(
        cls,
        payload: ControlledTop5PublicationPayload,
        publication_authorization: Top5PublicationAuthorization,
        *,
        activation_bindings: Mapping[str, object],
        artifact: Mapping[str, object],
        now: datetime,
    ) -> ControlledPublicationAttestation:
        now = _utc(now, "now")
        payload.validate()
        publication_authorization.validate(now=now)
        publication_authorization.binds(payload)
        if activation_bindings.get("active") is not True:
            raise ProductionContractError(
                "controlled publication attestation requires the exact active activation"
            )
        payload_values = {
            name: payload.activation_id
            if name == "activation_id"
            else getattr(payload, name)
            for name in _CONTROLLED_PUBLICATION_BINDING_FIELDS
        }
        for name, expected in payload_values.items():
            if activation_bindings.get(name) != expected:
                raise ProductionContractError(
                    f"controlled publication activation binding mismatch: {name}"
                )
        authorization_claims = {
            name: getattr(publication_authorization, name)
            for name in (
                "publication_authorization_id",
                "activation_id",
                "league_code",
                "candidate_id",
                "model_identity",
                "source_sha",
                "research_sha",
                "model_artifact_hash",
                "signal_time_experiment_id",
                "publication_authorized",
                "no_bet",
            )
        }
        return cls(
            artifact_path=payload.artifact_path,
            artifact_hash=_digest(artifact),
            **{
                name: payload_values[name]
                for name in _CONTROLLED_PUBLICATION_BINDING_FIELDS
            },
            activation_binding_digest=_digest(
                {"active": True, **_controlled_binding_values(payload_values)}
            ),
            publication_authorization_id=publication_authorization.publication_authorization_id,
            publication_authorization_digest=_digest(
                _publication_authorization_values(authorization_claims)
            ),
            active_activation=True,
            publication_authorized=True,
            no_bet=True,
            issued_at=now,
            expires_at=publication_authorization.expires_at,
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> ControlledPublicationAttestation:
        expected = {
            "artifact_path",
            "artifact_hash",
            *_CONTROLLED_PUBLICATION_BINDING_FIELDS,
            "activation_binding_digest",
            "publication_authorization_id",
            "publication_authorization_digest",
            "active_activation",
            "publication_authorized",
            "no_bet",
            "issued_at",
            "expires_at",
        }
        if not isinstance(value, Mapping):
            raise ProductionContractError(
                "controlled publication attestation must be an object"
            )
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        if missing:
            raise ProductionContractError(
                "controlled publication attestation is missing: " + ", ".join(missing)
            )
        if extra:
            raise ProductionContractError(
                "controlled publication attestation has unknown fields: "
                + ", ".join(extra)
            )
        try:
            issued_at = datetime.fromisoformat(str(value["issued_at"]))
            expires_at = datetime.fromisoformat(str(value["expires_at"]))
        except (TypeError, ValueError) as exc:
            raise ProductionContractError(
                "controlled publication attestation timestamps are invalid"
            ) from exc
        return cls(
            **{
                name: value[name]
                for name in sorted(expected)
                if name not in {"issued_at", "expires_at"}
            },
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def validate(
        self,
        *,
        artifact: object,
        artifact_path: str,
        now: datetime,
    ) -> None:
        _required_text(
            {
                "artifact_path": self.artifact_path,
                "artifact_hash": self.artifact_hash,
                **{
                    name: getattr(self, name)
                    for name in _CONTROLLED_PUBLICATION_BINDING_FIELDS
                },
                "activation_binding_digest": self.activation_binding_digest,
                "publication_authorization_id": self.publication_authorization_id,
                "publication_authorization_digest": self.publication_authorization_digest,
            },
            "controlled publication attestation",
        )
        validate_artifact_ownership(self.artifact_path, ArtifactOwner.CONTROLLED_PUBLIC)
        if artifact_path != self.artifact_path:
            raise ProductionContractError(
                "controlled publication artifact path mismatch"
            )
        for name in (
            "artifact_hash",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "evidence_digest",
            "activation_binding_digest",
            "publication_authorization_digest",
        ):
            _hash(getattr(self, name), name)
        if self.active_activation is not True:
            raise ProductionContractError(
                "controlled publication requires active activation"
            )
        if self.publication_authorized is not True:
            raise ProductionContractError(
                "controlled publication requires separate publication authorization"
            )
        if self.no_bet is not True:
            raise ProductionContractError("controlled publication must remain no-bet")
        now = _utc(now, "now")
        if (
            self.expires_at <= self.issued_at
            or not self.issued_at <= now <= self.expires_at
        ):
            raise ProductionContractError(
                "stale or expired controlled publication attestation"
            )
        if self.artifact_hash != _digest(artifact):
            raise ProductionContractError(
                "controlled publication artifact hash mismatch"
            )
        if self.publication_authorization_digest != _digest(
            _publication_authorization_values(
                {
                    "publication_authorization_id": self.publication_authorization_id,
                    **{
                        name: getattr(self, name)
                        for name in (
                            "activation_id",
                            "league_code",
                            "candidate_id",
                            "model_identity",
                            "source_sha",
                            "research_sha",
                            "model_artifact_hash",
                            "signal_time_experiment_id",
                        )
                    },
                    "publication_authorized": self.publication_authorized,
                    "no_bet": self.no_bet,
                }
            )
        ):
            raise ProductionContractError(
                "controlled publication authorization digest mismatch"
            )
        binding_values = {
            name: getattr(self, name) for name in _CONTROLLED_PUBLICATION_BINDING_FIELDS
        }
        if self.activation_binding_digest != _digest(
            {"active": True, **_controlled_binding_values(binding_values)}
        ):
            raise ProductionContractError(
                "controlled publication activation digest mismatch"
            )

    def as_payload(self) -> dict[str, object]:
        return {
            "artifact_path": self.artifact_path,
            "artifact_hash": self.artifact_hash,
            **{
                name: getattr(self, name)
                for name in _CONTROLLED_PUBLICATION_BINDING_FIELDS
            },
            "activation_binding_digest": self.activation_binding_digest,
            "publication_authorization_id": self.publication_authorization_id,
            "publication_authorization_digest": self.publication_authorization_digest,
            "active_activation": self.active_activation,
            "publication_authorized": self.publication_authorized,
            "no_bet": self.no_bet,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class ControlledPublicationCapability:
    """One-time operator capability kept separate from public artifacts."""

    capability_id: str
    capability_nonce: str

    def validate(self) -> None:
        _required_text(
            {
                "capability_id": self.capability_id,
                "capability_nonce": self.capability_nonce,
            },
            "controlled publication capability",
        )
        if len(self.capability_nonce) < 32:
            raise ProductionContractError(
                "controlled publication capability nonce is too short"
            )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> ControlledPublicationCapability:
        expected = {"capability_id", "capability_nonce"}
        if not isinstance(value, Mapping):
            raise ProductionContractError(
                "controlled publication capability must be an object"
            )
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        if missing:
            raise ProductionContractError(
                "controlled publication capability is missing: " + ", ".join(missing)
            )
        if extra:
            raise ProductionContractError(
                "controlled publication capability has unknown fields: "
                + ", ".join(extra)
            )
        capability = cls(
            capability_id=value["capability_id"],
            capability_nonce=value["capability_nonce"],
        )
        capability.validate()
        return capability

    def as_payload(self) -> dict[str, str]:
        self.validate()
        return {
            "capability_id": self.capability_id,
            "capability_nonce": self.capability_nonce,
        }


class ControlledPublicationCapabilityStore(Protocol):
    def issue(
        self, attestation: ControlledPublicationAttestation
    ) -> ControlledPublicationCapability: ...


def _capability_nonce_digest(nonce: str) -> str:
    return sha256(nonce.encode("utf-8")).hexdigest()


class FileControlledPublicationCapabilityStore:
    """Operator-owned one-time capability state outside the repository."""

    _SCHEMA = "top5-controlled-publication-capability-v1"

    def __init__(self, state_path: str | Path | None = None) -> None:
        canonical_path = controlled_publication_capability_state_path()
        selected_path = canonical_path if state_path is None else Path(state_path)
        if not selected_path.is_absolute():
            raise ProductionContractError(
                "controlled publication capability state must be an absolute path"
            )
        if selected_path.resolve() != canonical_path.resolve():
            raise ProductionContractError(
                "controlled publication capability state must use the canonical operator path"
            )
        self.state_path = canonical_path
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")

    @contextmanager
    def _locked(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write_state(self, state: Mapping[str, object]) -> None:
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(state, sort_keys=True, separators=(",", ":"))
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
            os.chmod(self.state_path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_state(self) -> dict[str, object]:
        if not self.state_path.is_file() or self.state_path.is_symlink():
            raise ProductionContractError(
                "controlled publication capability state is unavailable"
            )
        try:
            value = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductionContractError(
                "controlled publication capability state is invalid"
            ) from exc
        if not isinstance(value, dict):
            raise ProductionContractError(
                "controlled publication capability state must be an object"
            )
        expected = {
            "schema",
            "capability_id",
            "nonce_digest",
            "attestation",
            "consumed",
        }
        if set(value) != expected or value.get("schema") != self._SCHEMA:
            raise ProductionContractError(
                "controlled publication capability state has an invalid shape"
            )
        if value.get("consumed") is not False:
            raise ProductionContractError(
                "controlled publication capability has already been consumed"
            )
        _required_text(
            {
                "capability_id": value.get("capability_id"),
                "nonce_digest": value.get("nonce_digest"),
            },
            "controlled publication capability state",
        )
        _hash(value["nonce_digest"], "capability nonce digest")
        if not isinstance(value.get("attestation"), Mapping):
            raise ProductionContractError(
                "controlled publication capability attestation is invalid"
            )
        return value

    def issue(
        self, attestation: ControlledPublicationAttestation
    ) -> ControlledPublicationCapability:
        capability_id = f"top5-capability:{uuid.uuid4().hex}"
        capability_nonce = secrets.token_urlsafe(32)
        capability = ControlledPublicationCapability(capability_id, capability_nonce)
        capability.validate()
        state = {
            "schema": self._SCHEMA,
            "capability_id": capability.capability_id,
            "nonce_digest": _capability_nonce_digest(capability.capability_nonce),
            "attestation": attestation.as_payload(),
            "consumed": False,
        }
        with self._locked():
            if self.state_path.exists() or self.state_path.is_symlink():
                raise ProductionContractError(
                    "controlled publication capability state already exists"
                )
            self._write_state(state)
        return capability

    def consume(
        self,
        capability: ControlledPublicationCapability,
        attestation: ControlledPublicationAttestation,
        *,
        artifact: object,
        artifact_path: str,
        now: datetime,
    ) -> None:
        capability.validate()
        with self._locked():
            state = self._read_state()
            if state["capability_id"] != capability.capability_id:
                raise ProductionContractError(
                    "controlled publication capability identity mismatch"
                )
            if not compare_digest(
                state["nonce_digest"],
                _capability_nonce_digest(capability.capability_nonce),
            ):
                raise ProductionContractError(
                    "controlled publication capability nonce mismatch"
                )
            stored = ControlledPublicationAttestation.from_mapping(state["attestation"])
            if stored.as_payload() != attestation.as_payload():
                raise ProductionContractError(
                    "controlled publication capability binding mismatch"
                )
            attestation.validate(
                artifact=artifact,
                artifact_path=artifact_path,
                now=now,
            )
            state["consumed"] = True
            self._write_state(state)


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
            raise ProductionContractError(
                "publication rollback did not restore unpublished state"
            )
        if self.scheduler_enabled or self.ledger_mutated:
            raise ProductionContractError(
                "publication rollback safety state is invalid"
            )


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
            raise ProductionContractError(
                "publication requires the exact active controlled activation"
            )
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
            "controlled_shadow_run_id",
            "qualification_session_id",
        ):
            expected = activation_bindings.get(name)
            actual = (
                payload.activation_id
                if name == "activation_id"
                else getattr(payload, name, None)
            )
            if actual != expected:
                raise ProductionContractError(
                    f"publication activation binding mismatch: {name}"
                )
        if (
            self._current is not None
            and payload.generated_at <= self._current.payload.generated_at
        ):
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

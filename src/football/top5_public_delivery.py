"""Governed delivery seam for an authorized controlled Top-5 generation.

This module is deliberately a preparation/staging boundary, not a publisher.
It accepts an already validated :class:`PublishedTop5BatchArtifact`, merges
only the five Top-5 football leagues into the existing public product, and
serializes that merged product exactly once.  The resulting immutable bytes
are the input for both the static fallback and the Worker ``/signals`` write
path.  No filesystem, network, Worker, scheduler, ledger, or Cloudflare
mutation occurs here.

The in-memory transaction below is an acceptance-test double for the future
bounded runtime transaction.  It proves failure restores the previous safe
generation and that a successful retry is idempotent without authorizing
publication itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from src.football.production_contracts import (
    ActivationMode,
    ProductionContractError,
)
from src.football.top5_publisher import (
    TOP5_PUBLIC_RELEASE_LEAGUES,
    ControlledPublicationAttestation,
    ControlledPublicationCapability,
    ControlledTop5PublicationPayload,
    FileControlledPublicationCapabilityStore,
    PublishedTop5BatchArtifact,
)
from src.notifications.public_serializer import serialize_public_product

TOP5_DELIVERY_STATIC_PATH = "docs/data/signals.json"
TOP5_DELIVERY_WORKER_PATH = "/signals"
_TOP5_LEAGUES = frozenset(TOP5_PUBLIC_RELEASE_LEAGUES)
_TOP5_HEALTH_KEYS = frozenset(
    {
        "top5_activation_id",
        "top5_leagues",
        "top5_candidate_id",
        "top5_model_identity",
        "top5_source_sha",
        "top5_research_sha",
        "top5_model_artifact_hash",
        "top5_provider_authority",
        "top5_result_authority",
        "top5_evidence_digests",
        "publication_status",
        "publication_enabled",
    }
)


class Top5DeliveryError(ProductionContractError):
    """Raised when a controlled delivery plan cannot be built safely."""


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise Top5DeliveryError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Top5DeliveryError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Top5DeliveryError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _as_dict(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise Top5DeliveryError(f"{field} must be an object")
    return {str(key): item for key, item in value.items()}


def _records(value: object, field: str) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise Top5DeliveryError(f"{field} must be a list")
    result: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise Top5DeliveryError(f"{field}[{index}] must be an object")
        result.append({str(key): child for key, child in item.items()})
    return result


def _top5_records(records: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        dict(record)
        for record in records
        if str(record.get("league", "")).upper() in _TOP5_LEAGUES
    ]


def _record_fingerprint(record: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(record)).hexdigest()


def _validate_artifact(artifact: PublishedTop5BatchArtifact) -> tuple[
    dict[str, object], list[dict[str, object]]
]:
    if not isinstance(artifact, PublishedTop5BatchArtifact):
        raise Top5DeliveryError("delivery requires a published Top-5 batch artifact")
    try:
        artifact.validate()
    except (ProductionContractError, TypeError, ValueError) as exc:
        raise Top5DeliveryError("published Top-5 batch artifact is invalid") from exc

    product = _as_dict(artifact.public_product, "artifact.public_product")
    release = _as_dict(product.get("top5_release"), "artifact.top5_release")
    required_release = {
        "schema_version",
        "generation_id",
        "activation_state",
        "activation_id",
        "publication_status",
        "publication_enabled",
        "publication_authorization_id",
        "provider_authority",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "league_codes",
        "no_bet",
    }
    missing = sorted(key for key in required_release if not release.get(key))
    if missing:
        raise Top5DeliveryError(
            "artifact release is incomplete: " + ", ".join(missing)
        )
    if (
        release.get("schema_version") != "top5-public-release-v1"
        or release.get("activation_state") != "CONTROLLED"
        or release.get("publication_status") != "PUBLISHED"
        or release.get("publication_enabled") is not True
        or release.get("no_bet") is not True
    ):
        raise Top5DeliveryError("artifact is not an authorized controlled no-bet release")
    codes = tuple(str(code).upper() for code in release["league_codes"])
    if set(codes) != _TOP5_LEAGUES or len(codes) != len(_TOP5_LEAGUES):
        raise Top5DeliveryError("artifact release must contain the five Top-5 leagues once")

    product_records = _records(product.get("football"), "artifact.football")
    top5 = _top5_records(product_records)
    if len(top5) != len(_TOP5_LEAGUES) * 3:
        raise Top5DeliveryError("artifact must contain three outcomes for each Top-5 league")
    by_league: dict[str, set[str]] = {league: set() for league in _TOP5_LEAGUES}
    for record in top5:
        league = str(record.get("league", "")).upper()
        fixture = record.get("fixture_key")
        if not isinstance(fixture, str) or not fixture:
            raise Top5DeliveryError("artifact Top-5 fixture identity is missing")
        by_league[league].add(fixture)
        if (
            record.get("activation_id") != release.get("activation_id")
            or record.get("provider") != release.get("provider_authority")
            or record.get("run_id") != release.get("controlled_shadow_run_id")
            or record.get("session_id") != release.get("qualification_session_id")
            or record.get("publication_status") != "PUBLISHED"
            or record.get("publication_enabled") is not True
            or record.get("no_bet") is not True
        ):
            raise Top5DeliveryError("artifact record/release binding mismatch")
        if record.get("synthetic") is True or str(
            record.get("evidence_kind", "")
        ).upper() in {"SYNTHETIC", "TEST_FIXTURE"}:
            raise Top5DeliveryError(
                "artifact contains synthetic or test evidence and cannot be delivered"
            )
    if set(by_league) != _TOP5_LEAGUES or any(
        len(fixtures) != 1 for fixtures in by_league.values()
    ):
        raise Top5DeliveryError("artifact has a mixed or incomplete Top-5 generation")
    if {str(record.get("league", "")).upper() for record in product_records} != _TOP5_LEAGUES:
        raise Top5DeliveryError("artifact public product contains unrelated football records")
    return release, top5


def _merge_health(
    current: Mapping[str, object], batch: Mapping[str, object]
) -> dict[str, object]:
    result = dict(current.get("health") or {}) if isinstance(current.get("health"), Mapping) else {}
    batch_health = batch.get("health")
    if isinstance(batch_health, Mapping):
        for key in _TOP5_HEALTH_KEYS:
            if key in batch_health:
                result[key] = batch_health[key]
    return result


@dataclass(frozen=True)
class Top5DeliveryPlan:
    """One immutable serialized payload for both public delivery targets."""

    public_product: Mapping[str, object]
    serialized_payload: bytes
    public_product_digest: str
    generation_id: str
    activation_id: str
    provider_authority: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    publication_authorization_id: str
    artifact_digest: str
    source_snapshot_digest: str
    static_path: str = TOP5_DELIVERY_STATIC_PATH
    worker_path: str = TOP5_DELIVERY_WORKER_PATH

    @property
    def static_payload(self) -> bytes:
        return self.serialized_payload

    @property
    def worker_payload(self) -> bytes:
        return self.serialized_payload

    def validate(self) -> None:
        if not isinstance(self.serialized_payload, bytes) or not self.serialized_payload:
            raise Top5DeliveryError("delivery plan payload is missing")
        if self.static_path != TOP5_DELIVERY_STATIC_PATH or self.worker_path != TOP5_DELIVERY_WORKER_PATH:
            raise Top5DeliveryError("delivery plan targets are not the governed paths")
        if self.static_payload != self.worker_payload:
            raise Top5DeliveryError("Static/Worker payload bytes differ")
        if _sha_bytes(self.serialized_payload) != self.public_product_digest:
            raise Top5DeliveryError("delivery plan public product digest mismatch")
        try:
            payload = json.loads(self.serialized_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Top5DeliveryError("delivery plan payload is not valid JSON") from exc
        if not isinstance(payload, dict) or payload != _thaw(self.public_product):
            raise Top5DeliveryError("delivery plan bytes do not match public product")
        release = _as_dict(payload.get("top5_release"), "delivery plan top5_release")
        if (
            release.get("generation_id") != self.generation_id
            or release.get("activation_id") != self.activation_id
            or release.get("provider_authority") != self.provider_authority
            or release.get("controlled_shadow_run_id") != self.controlled_shadow_run_id
            or release.get("qualification_session_id") != self.qualification_session_id
            or release.get("publication_authorization_id")
            != self.publication_authorization_id
            or release.get("activation_state") != "CONTROLLED"
            or release.get("publication_status") != "PUBLISHED"
            or release.get("publication_enabled") is not True
            or release.get("no_bet") is not True
        ):
            raise Top5DeliveryError("delivery plan release binding mismatch")
        codes = release.get("league_codes")
        if set(codes or ()) != _TOP5_LEAGUES or len(codes or ()) != len(_TOP5_LEAGUES):
            raise Top5DeliveryError(
                "delivery plan does not contain exactly five Top-5 leagues"
            )
        for name in ("generation_id", "activation_id", "provider_authority"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise Top5DeliveryError(f"delivery plan {name} is missing")
        for name in (
            "public_product_digest",
            "artifact_digest",
            "source_snapshot_digest",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64:
                raise Top5DeliveryError(f"delivery plan {name} is invalid")

    def manifest(self) -> dict[str, object]:
        return {
            "schema_version": "top5-delivery-plan-v1",
            "generation_id": self.generation_id,
            "activation_id": self.activation_id,
            "provider_authority": self.provider_authority,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "publication_authorization_id": self.publication_authorization_id,
            "artifact_digest": self.artifact_digest,
            "source_snapshot_digest": self.source_snapshot_digest,
            "public_product_digest": self.public_product_digest,
            "static_path": self.static_path,
            "worker_path": self.worker_path,
            "static_payload_digest": hashlib.sha256(self.static_payload).hexdigest(),
            "worker_payload_digest": hashlib.sha256(self.worker_payload).hexdigest(),
            "payload_bytes": len(self.serialized_payload),
        }


class Top5CanonicalDeliveryAdapter:
    """Build a safe canonical public snapshot without performing delivery."""

    def build_plan(
        self,
        current_public_snapshot: Mapping[str, object],
        artifact: PublishedTop5BatchArtifact,
    ) -> Top5DeliveryPlan:
        if not isinstance(current_public_snapshot, Mapping):
            raise Top5DeliveryError("current canonical public snapshot must be an object")
        release, incoming_top5 = _validate_artifact(artifact)
        current = {str(key): value for key, value in current_public_snapshot.items()}
        current_records = _records(current.get("football", []), "current.football")
        current_top5 = _top5_records(current_records)
        current_release = current.get("top5_release")
        if current_release is not None:
            current_release_dict = _as_dict(current_release, "current.top5_release")
            if current_release_dict != release:
                raise Top5DeliveryError(
                    "current canonical snapshot contains a conflicting Top-5 generation"
                )
            if sorted(map(_record_fingerprint, current_top5)) != sorted(
                map(_record_fingerprint, incoming_top5)
            ):
                raise Top5DeliveryError(
                    "current canonical snapshot conflicts with the authorized Top-5 records"
                )

        non_top5 = [
            record
            for record in current_records
            if str(record.get("league", "")).upper() not in _TOP5_LEAGUES
        ]
        batch_product = _as_dict(artifact.public_product, "artifact.public_product")
        merged = dict(current)
        merged["updated"] = batch_product.get("updated", current.get("updated"))
        merged["football"] = non_top5 + incoming_top5
        merged["top5_release"] = release
        merged["health"] = _merge_health(current, batch_product)

        try:
            serialized_product = serialize_public_product(merged)
        except (AssertionError, ValueError) as exc:
            raise Top5DeliveryError("canonical public product serialization failed") from exc
        serialized_payload = _canonical_bytes(serialized_product)
        digest = _digest(serialized_product)
        if hashlib.sha256(serialized_payload).hexdigest() != digest:
            raise Top5DeliveryError("canonical public product digest is not deterministic")
        plan = Top5DeliveryPlan(
            public_product=MappingProxyType(
                {str(key): _freeze(value) for key, value in serialized_product.items()}
            ),
            serialized_payload=serialized_payload,
            public_product_digest=digest,
            generation_id=str(release["generation_id"]),
            activation_id=str(release["activation_id"]),
            provider_authority=str(release["provider_authority"]),
            controlled_shadow_run_id=str(release["controlled_shadow_run_id"]),
            qualification_session_id=str(release["qualification_session_id"]),
            publication_authorization_id=str(
                release["publication_authorization_id"]
            ),
            artifact_digest=artifact.artifact_digest,
            source_snapshot_digest=_digest(current),
        )
        plan.validate()
        return plan


_CONTROLLED_PAYLOAD_FIELDS = {
    "artifact_path",
    "activation_id",
    "league_code",
    "candidate_id",
    "model_identity",
    "signal_time_experiment_id",
    "source_sha",
    "research_sha",
    "model_artifact_hash",
    "provider_authority",
    "result_authority",
    "evidence_digest",
    "controlled_shadow_run_id",
    "qualification_session_id",
    "generated_at",
    "football_records",
    "health",
    "activation_mode",
    "no_bet",
    "publication_enabled",
}


def artifact_to_mapping(artifact: PublishedTop5BatchArtifact) -> dict[str, object]:
    """Return the operator-safe JSON representation consumed by ``prepare``."""

    artifact.validate()
    return {
        "schema_version": "top5-published-batch-artifact-v1",
        "payloads": [
            {
                "artifact_path": payload.artifact_path,
                "activation_id": payload.activation_id,
                "league_code": payload.league_code,
                "candidate_id": payload.candidate_id,
                "model_identity": payload.model_identity,
                "signal_time_experiment_id": payload.signal_time_experiment_id,
                "source_sha": payload.source_sha,
                "research_sha": payload.research_sha,
                "model_artifact_hash": payload.model_artifact_hash,
                "provider_authority": payload.provider_authority,
                "result_authority": payload.result_authority,
                "evidence_digest": payload.evidence_digest,
                "controlled_shadow_run_id": payload.controlled_shadow_run_id,
                "qualification_session_id": payload.qualification_session_id,
                "generated_at": payload.generated_at.isoformat(),
                "football_records": _thaw(payload.football_records),
                "health": _thaw(payload.health),
                "activation_mode": ActivationMode(payload.activation_mode).value,
                "no_bet": payload.no_bet,
                "publication_enabled": payload.publication_enabled,
            }
            for payload in artifact.payloads
        ],
        "public_product": _thaw(artifact.public_product),
        "artifact_digest": artifact.artifact_digest,
        "published_at": artifact.published_at.isoformat(),
    }


def artifact_from_mapping(value: Mapping[str, object]) -> PublishedTop5BatchArtifact:
    """Load an exact batch artifact without accepting unknown private fields."""

    expected = {
        "schema_version",
        "payloads",
        "public_product",
        "artifact_digest",
        "published_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise Top5DeliveryError("published batch artifact has an invalid shape")
    if value.get("schema_version") != "top5-published-batch-artifact-v1":
        raise Top5DeliveryError("published batch artifact schema is unsupported")
    payload_values = value.get("payloads")
    if not isinstance(payload_values, Sequence) or isinstance(
        payload_values, (str, bytes)
    ):
        raise Top5DeliveryError("published batch artifact payloads must be a list")
    payloads: list[ControlledTop5PublicationPayload] = []
    for index, item in enumerate(payload_values):
        if not isinstance(item, Mapping) or set(item) != _CONTROLLED_PAYLOAD_FIELDS:
            raise Top5DeliveryError(f"published batch payload {index} has an invalid shape")
        try:
            payloads.append(
                ControlledTop5PublicationPayload(
                    artifact_path=item["artifact_path"],
                    activation_id=item["activation_id"],
                    league_code=item["league_code"],
                    candidate_id=item["candidate_id"],
                    model_identity=item["model_identity"],
                    signal_time_experiment_id=item["signal_time_experiment_id"],
                    source_sha=item["source_sha"],
                    research_sha=item["research_sha"],
                    model_artifact_hash=item["model_artifact_hash"],
                    provider_authority=item["provider_authority"],
                    result_authority=item["result_authority"],
                    evidence_digest=item["evidence_digest"],
                    controlled_shadow_run_id=item["controlled_shadow_run_id"],
                    qualification_session_id=item["qualification_session_id"],
                    generated_at=_parse_timestamp(item["generated_at"], "generated_at"),
                    football_records=tuple(_records(item["football_records"], "football_records")),
                    health=_as_dict(item["health"], "health"),
                    activation_mode=ActivationMode(item["activation_mode"]),
                    no_bet=item["no_bet"],
                    publication_enabled=item["publication_enabled"],
                )
            )
        except (KeyError, TypeError, ValueError, ProductionContractError) as exc:
            raise Top5DeliveryError(f"published batch payload {index} is invalid") from exc
    try:
        artifact = PublishedTop5BatchArtifact(
            payloads=tuple(payloads),
            public_product=_as_dict(value["public_product"], "public_product"),
            artifact_digest=value["artifact_digest"],
            published_at=_parse_timestamp(value["published_at"], "published_at"),
        )
        artifact.validate()
    except (KeyError, TypeError, ValueError, ProductionContractError) as exc:
        raise Top5DeliveryError("published batch artifact is invalid") from exc
    _validate_artifact(artifact)
    return artifact


def plan_to_mapping(plan: Top5DeliveryPlan) -> dict[str, object]:
    plan.validate()
    return {
        "schema_version": "top5-delivery-plan-v1",
        "manifest": plan.manifest(),
        "public_product": _thaw(plan.public_product),
        "serialized_payload_b64": base64.b64encode(plan.serialized_payload).decode(
            "ascii"
        ),
    }


def plan_from_mapping(value: Mapping[str, object]) -> Top5DeliveryPlan:
    expected = {
        "schema_version",
        "manifest",
        "public_product",
        "serialized_payload_b64",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise Top5DeliveryError("delivery plan has an invalid shape")
    if value.get("schema_version") != "top5-delivery-plan-v1":
        raise Top5DeliveryError("delivery plan schema is unsupported")
    manifest = _as_dict(value["manifest"], "delivery plan manifest")
    try:
        serialized_payload = base64.b64decode(
            str(value["serialized_payload_b64"]), validate=True
        )
    except (ValueError, TypeError) as exc:
        raise Top5DeliveryError("delivery plan payload encoding is invalid") from exc
    try:
        plan = Top5DeliveryPlan(
            public_product=MappingProxyType(
                {str(key): _freeze(item) for key, item in _as_dict(value["public_product"], "public_product").items()}
            ),
            serialized_payload=serialized_payload,
            public_product_digest=manifest["public_product_digest"],
            generation_id=manifest["generation_id"],
            activation_id=manifest["activation_id"],
            provider_authority=manifest["provider_authority"],
            controlled_shadow_run_id=manifest["controlled_shadow_run_id"],
            qualification_session_id=manifest["qualification_session_id"],
            publication_authorization_id=manifest["publication_authorization_id"],
            artifact_digest=manifest["artifact_digest"],
            source_snapshot_digest=manifest["source_snapshot_digest"],
            static_path=manifest["static_path"],
            worker_path=manifest["worker_path"],
        )
        plan.validate()
    except (KeyError, TypeError, ValueError, ProductionContractError) as exc:
        raise Top5DeliveryError(f"delivery plan is invalid: {exc}") from exc
    if plan.manifest() != manifest:
        raise Top5DeliveryError("delivery plan manifest does not match payload")
    return plan


@dataclass(frozen=True)
class Top5DeliveryAttestation:
    """Detached batch authorization consumed by the delivery executor."""

    schema_version: str
    attestation_digest: str
    artifact_digest: str
    generation_id: str
    activation_id: str
    provider_authority: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    publication_authorization_id: str
    league_codes: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    publication_authorized: bool
    no_bet: bool
    capability_id: str
    capability_nonce_digest: str
    controlled_publication_attestation: Mapping[str, object] | None = None

    def _unsigned_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "artifact_digest": self.artifact_digest,
            "generation_id": self.generation_id,
            "activation_id": self.activation_id,
            "provider_authority": self.provider_authority,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "publication_authorization_id": self.publication_authorization_id,
            "league_codes": list(self.league_codes),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "publication_authorized": self.publication_authorized,
            "no_bet": self.no_bet,
            "capability_id": self.capability_id,
            "capability_nonce_digest": self.capability_nonce_digest,
        }
        if self.controlled_publication_attestation is not None:
            payload["controlled_publication_attestation"] = _thaw(
                self.controlled_publication_attestation
            )
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Top5DeliveryAttestation:
        required = {
            "schema_version",
            "attestation_digest",
            "artifact_digest",
            "generation_id",
            "activation_id",
            "provider_authority",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "publication_authorization_id",
            "league_codes",
            "issued_at",
            "expires_at",
            "publication_authorized",
            "no_bet",
            "capability_id",
            "capability_nonce_digest",
        }
        optional = {"controlled_publication_attestation"}
        if not isinstance(value, Mapping) or set(value) - required - optional or not required.issubset(value):
            raise Top5DeliveryError("Top-5 delivery attestation has an invalid shape")
        codes = value["league_codes"]
        if not isinstance(codes, Sequence) or isinstance(codes, (str, bytes)):
            raise Top5DeliveryError("Top-5 delivery attestation leagues are invalid")
        try:
            return cls(
                schema_version=value["schema_version"],
                attestation_digest=value["attestation_digest"],
                artifact_digest=value["artifact_digest"],
                generation_id=value["generation_id"],
                activation_id=value["activation_id"],
                provider_authority=value["provider_authority"],
                controlled_shadow_run_id=value["controlled_shadow_run_id"],
                qualification_session_id=value["qualification_session_id"],
                publication_authorization_id=value["publication_authorization_id"],
                league_codes=tuple(str(code) for code in codes),
                issued_at=_parse_timestamp(value["issued_at"], "attestation.issued_at"),
                expires_at=_parse_timestamp(value["expires_at"], "attestation.expires_at"),
                publication_authorized=value["publication_authorized"],
                no_bet=value["no_bet"],
                capability_id=value["capability_id"],
                capability_nonce_digest=value["capability_nonce_digest"],
                controlled_publication_attestation=(
                    _as_dict(
                        value["controlled_publication_attestation"],
                        "controlled_publication_attestation",
                    )
                    if "controlled_publication_attestation" in value
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise Top5DeliveryError("Top-5 delivery attestation is invalid") from exc

    def as_payload(self) -> dict[str, object]:
        return {**self._unsigned_payload(), "attestation_digest": self.attestation_digest}

    def validate(
        self,
        *,
        plan: Top5DeliveryPlan,
        artifact: PublishedTop5BatchArtifact,
        capability: Mapping[str, object],
        now: datetime,
    ) -> None:
        plan.validate()
        _validate_artifact(artifact)
        if self.schema_version != "top5-delivery-attestation-v1":
            raise Top5DeliveryError("Top-5 delivery attestation schema is unsupported")
        if self.attestation_digest != _digest(self._unsigned_payload()):
            raise Top5DeliveryError("Top-5 delivery attestation digest mismatch")
        if self.artifact_digest != artifact.artifact_digest:
            raise Top5DeliveryError("Top-5 delivery artifact digest mismatch")
        if (
            self.generation_id != plan.generation_id
            or self.activation_id != plan.activation_id
            or self.provider_authority != plan.provider_authority
            or self.controlled_shadow_run_id != plan.controlled_shadow_run_id
            or self.qualification_session_id != plan.qualification_session_id
            or self.publication_authorization_id != plan.publication_authorization_id
            or self.league_codes != tuple(TOP5_PUBLIC_RELEASE_LEAGUES)
        ):
            raise Top5DeliveryError("Top-5 delivery attestation binding mismatch")
        if self.publication_authorized is not True:
            raise Top5DeliveryError("publication authorization is missing")
        if self.no_bet is not True:
            raise Top5DeliveryError("Top-5 delivery cannot enable betting")
        now_utc = _parse_timestamp(now.isoformat(), "now")
        if not self.issued_at <= now_utc <= self.expires_at:
            raise Top5DeliveryError("Top-5 delivery attestation is expired or not yet valid")
        try:
            token = ControlledPublicationCapability.from_mapping(capability)
        except (TypeError, ValueError) as exc:
            raise Top5DeliveryError("controlled publication capability is invalid") from exc
        if token.capability_id != self.capability_id:
            raise Top5DeliveryError("controlled publication capability identity mismatch")
        if _sha_bytes(token.capability_nonce.encode("utf-8")) != self.capability_nonce_digest:
            raise Top5DeliveryError("controlled publication capability binding mismatch")


class StaticDeliveryTransport(Protocol):
    def current_payload(self) -> bytes | None: ...

    def stage(self, path: str, payload: bytes) -> None: ...

    def commit(self) -> None: ...

    def rollback_stage(self) -> None: ...


class WorkerDeliveryTransport(Protocol):
    def current_payload(self) -> bytes | None: ...

    def write_signals(self, payload: bytes) -> None: ...

    def restore_signals(self, payload: bytes | None) -> None: ...


class DeliveryCapabilityConsumer(Protocol):
    def consume(
        self,
        attestation: Top5DeliveryAttestation,
        capability: Mapping[str, object],
        artifact: PublishedTop5BatchArtifact,
        now: datetime,
    ) -> None: ...


class InMemoryDeliveryCapabilityConsumer:
    """Offline capability consumer; it records, but never persists, consumption."""

    def __init__(self) -> None:
        self.consume_calls = 0

    def consume(
        self,
        attestation: Top5DeliveryAttestation,
        capability: Mapping[str, object],
        artifact: PublishedTop5BatchArtifact,
        now: datetime,
    ) -> None:
        del artifact, now
        ControlledPublicationCapability.from_mapping(capability)
        if attestation.controlled_publication_attestation is not None:
            ControlledPublicationAttestation.from_mapping(
                attestation.controlled_publication_attestation
            )
        self.consume_calls += 1


class FileControlledDeliveryCapabilityConsumer:
    """Consume the operator-owned existing one-time capability state."""

    def __init__(self, store: FileControlledPublicationCapabilityStore | None = None) -> None:
        self.store = store or FileControlledPublicationCapabilityStore()

    def consume(
        self,
        attestation: Top5DeliveryAttestation,
        capability: Mapping[str, object],
        artifact: PublishedTop5BatchArtifact,
        now: datetime,
    ) -> None:
        if attestation.controlled_publication_attestation is None:
            raise Top5DeliveryError(
                "existing controlled publication attestation is required for execution"
            )
        try:
            source_attestation = ControlledPublicationAttestation.from_mapping(
                attestation.controlled_publication_attestation
            )
            token = ControlledPublicationCapability.from_mapping(capability)
            self.store.consume(
                token,
                source_attestation,
                artifact=artifact.public_product,
                artifact_path=source_attestation.artifact_path,
                now=now,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise Top5DeliveryError(
                "existing controlled publication capability rejected delivery"
            ) from exc


class InMemoryStaticDeliveryTransport:
    """Offline static transport with explicit failure injection."""

    def __init__(self, initial_payload: bytes | None = b"safe-static") -> None:
        self.payload = initial_payload
        self.staged: bytes | None = None
        self.stage_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.fail_stage = False
        self.fail_commit = False

    def current_payload(self) -> bytes | None:
        return self.payload

    def stage(self, path: str, payload: bytes) -> None:
        if path != TOP5_DELIVERY_STATIC_PATH:
            raise Top5DeliveryError("unexpected static delivery path")
        self.stage_calls += 1
        if self.fail_stage:
            raise Top5DeliveryError("static staging failed")
        self.staged = bytes(payload)
        if _sha_bytes(self.staged) != _sha_bytes(payload):
            raise Top5DeliveryError("static staged payload digest mismatch")

    def commit(self) -> None:
        self.commit_calls += 1
        if self.fail_commit:
            raise Top5DeliveryError("static commit failed")
        if self.staged is None:
            raise Top5DeliveryError("static commit has no staged payload")
        self.payload = self.staged
        self.staged = None

    def rollback_stage(self) -> None:
        self.rollback_calls += 1
        self.staged = None


class InMemoryWorkerDeliveryTransport:
    """Offline Worker transport with exact-byte and rollback assertions."""

    def __init__(self, initial_payload: bytes | None = b"safe-worker") -> None:
        self.payload = initial_payload
        self.write_calls = 0
        self.restore_calls = 0
        self.fail_write = False
        self.fail_restore = False
        self.written_payloads: list[bytes] = []

    def current_payload(self) -> bytes | None:
        return self.payload

    def write_signals(self, payload: bytes) -> None:
        self.write_calls += 1
        if self.fail_write:
            raise Top5DeliveryError("Worker write failed")
        self.written_payloads.append(bytes(payload))
        self.payload = bytes(payload)

    def restore_signals(self, payload: bytes | None) -> None:
        self.restore_calls += 1
        if self.fail_restore:
            raise Top5DeliveryError("Worker rollback failed")
        if payload is None:
            raise Top5DeliveryError("Worker has no safe rollback payload")
        self.payload = bytes(payload)


class RuntimePublisherStaticTransport:
    """Real static transport backed by the existing isolated runtime publisher."""

    def __init__(
        self,
        *,
        active_checkout: str | Path,
        stage_directory: str | Path,
        log_path: str | Path,
        commit_message: str,
        publish_script: str | Path | None = None,
    ) -> None:
        self.active_checkout = Path(active_checkout).resolve()
        self.stage_directory = Path(stage_directory).resolve()
        self.log_path = Path(log_path).resolve()
        self.commit_message = commit_message
        self.publish_script = Path(
            publish_script or Path(__file__).parents[2] / "scripts" / "publish_runtime_artifacts.sh"
        ).resolve()
        if self.stage_directory == self.active_checkout or self.stage_directory.is_relative_to(self.active_checkout):
            raise Top5DeliveryError("static stage must be outside the active checkout")
        if not self.publish_script.is_file():
            raise Top5DeliveryError("runtime publisher script is unavailable")

    def _stage_path(self) -> Path:
        return self.stage_directory / TOP5_DELIVERY_STATIC_PATH

    def current_payload(self) -> bytes | None:
        path = self.active_checkout / TOP5_DELIVERY_STATIC_PATH
        return path.read_bytes() if path.is_file() else None

    def stage(self, path: str, payload: bytes) -> None:
        if path != TOP5_DELIVERY_STATIC_PATH:
            raise Top5DeliveryError("unexpected static delivery path")
        self.stage_directory.mkdir(parents=True, exist_ok=True)
        target = self._stage_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        actual = target.read_bytes()
        if actual != payload or _sha_bytes(actual) != _sha_bytes(payload):
            raise Top5DeliveryError("static staged payload digest mismatch")

    def commit(self) -> None:
        command = [
            "bash",
            str(self.publish_script),
            "publish-staged",
            str(self.active_checkout),
            str(self.stage_directory),
            str(self.log_path),
            self.commit_message,
            TOP5_DELIVERY_STATIC_PATH,
        ]
        completed = subprocess.run(command, capture_output=True, check=False)
        if completed.returncode != 0:
            raise Top5DeliveryError("isolated runtime publisher static commit failed")

    def rollback_stage(self) -> None:
        target = self._stage_path()
        target.unlink(missing_ok=True)
        try:
            target.parent.rmdir()
        except OSError:
            pass


class HttpWorkerDeliveryTransport:
    """Raw-byte Worker ``/signals`` transport using protected env config."""

    def __init__(
        self,
        *,
        signals_url: str | None = None,
        api_token: str | None = None,
        timeout_seconds: int = 15,
    ) -> None:
        self.signals_url = signals_url or os.environ.get("SIGNALS_CLOUD_URL", "")
        self.api_token = api_token or os.environ.get("SIGNALS_API_TOKEN", "")
        self.timeout_seconds = timeout_seconds
        if not self.signals_url or not self.api_token:
            raise Top5DeliveryError("Worker URL and token must come from protected configuration")
        self.post_url = (
            self.signals_url[: -len("/signals.json")] + "/signals"
            if self.signals_url.endswith("/signals.json")
            else self.signals_url
        )

    def _requests(self):
        try:
            import requests
        except ImportError as exc:
            raise Top5DeliveryError("Worker transport dependency is unavailable") from exc
        return requests

    def current_payload(self) -> bytes | None:
        response = self._requests().get(self.signals_url, timeout=self.timeout_seconds)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise Top5DeliveryError("Worker readback failed")
        return bytes(response.content)

    def write_signals(self, payload: bytes) -> None:
        response = self._requests().post(
            self.post_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code != 200:
            raise Top5DeliveryError("Worker write failed")

    def restore_signals(self, payload: bytes | None) -> None:
        if payload is None:
            raise Top5DeliveryError("Worker safe rollback payload is unavailable")
        self.write_signals(payload)


TOP5_DELIVERY_PREPARED = "PREPARED"
TOP5_DELIVERY_STATIC_STAGED = "STATIC_STAGED"
TOP5_DELIVERY_WORKER_WRITTEN = "WORKER_WRITTEN"
TOP5_DELIVERY_STATIC_COMMITTED = "STATIC_COMMITTED"
TOP5_DELIVERY_ACCEPTANCE_REQUIRED = "ACCEPTANCE_REQUIRED"
TOP5_DELIVERY_DRY_RUN = "TOP5_DELIVERY_DRY_RUN"
TOP5_DELIVERY_IDEMPOTENT = "TOP5_DELIVERY_IDEMPOTENT"
TOP5_DELIVERY_ROLLBACK_SUCCEEDED = "TOP5_DELIVERY_ROLLBACK_SUCCEEDED"
TOP5_DELIVERY_ROLLBACK_REQUIRED = "TOP5_DELIVERY_ROLLBACK_REQUIRED"


@dataclass(frozen=True)
class Top5DeliveryExecutionResult:
    status: str
    states: tuple[str, ...]
    generation_id: str
    public_product_digest: str
    reason: str = ""
    worker_restored: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "states": list(self.states),
            "generation_id": self.generation_id,
            "public_product_digest": self.public_product_digest,
            "reason": self.reason,
            "worker_restored": self.worker_restored,
        }


class Top5DeliveryExecutor:
    """Execute the bounded transaction only when explicitly enabled."""

    def __init__(
        self,
        *,
        static_transport: StaticDeliveryTransport | None = None,
        worker_transport: WorkerDeliveryTransport | None = None,
        capability_consumer: DeliveryCapabilityConsumer | None = None,
        clock=None,
    ) -> None:
        self.static_transport = static_transport
        self.worker_transport = worker_transport
        self.capability_consumer = capability_consumer
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _canonical_transport_payload(payload: bytes | None) -> bytes | None:
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
            if not isinstance(decoded, Mapping):
                raise TypeError("payload is not an object")
            return _canonical_bytes(serialize_public_product(_as_dict(decoded, "payload")))
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            AssertionError,
            TypeError,
            ValueError,
        ) as exc:
            raise Top5DeliveryError("current delivery payload is not valid canonical JSON") from exc

    def _current_state(
        self, current_public_snapshot: Mapping[str, object], plan: Top5DeliveryPlan
    ) -> tuple[dict[str, object], str]:
        current = _as_dict(_thaw(current_public_snapshot), "current public snapshot")
        try:
            public = serialize_public_product(current)
        except (AssertionError, ValueError) as exc:
            raise Top5DeliveryError("current canonical public snapshot is invalid") from exc
        digest = _digest(public)
        if digest == plan.public_product_digest:
            return public, TOP5_DELIVERY_IDEMPOTENT
        current_release = public.get("top5_release")
        if isinstance(current_release, Mapping):
            current_release_dict = _as_dict(current_release, "current.top5_release")
            if current_release_dict.get("generation_id") != plan.generation_id:
                current_published = _parse_timestamp(
                    current_release_dict.get("published_at"),
                    "current.top5_release.published_at",
                )
                planned_release = _as_dict(
                    _thaw(plan.public_product).get("top5_release"),
                    "plan.top5_release",
                )
                planned_published = _parse_timestamp(
                    planned_release.get("published_at"),
                    "plan.top5_release.published_at",
                )
                if current_published > planned_published:
                    raise Top5DeliveryError("current public generation is newer")
                if current_release_dict.get("activation_id") == plan.activation_id:
                    raise Top5DeliveryError("conflicting generation uses the same activation")
        if _digest(current) != plan.source_snapshot_digest:
            raise Top5DeliveryError("current canonical public snapshot changed since prepare")
        return public, ""

    def _rollback_static_stage(self) -> bool:
        try:
            assert self.static_transport is not None
            self.static_transport.rollback_stage()
        except (Top5DeliveryError, OSError, RuntimeError, ValueError):
            return False
        return True

    def _restore_worker(self, payload: bytes | None) -> bool:
        try:
            assert self.worker_transport is not None
            self.worker_transport.restore_signals(payload)
        except (Top5DeliveryError, OSError, RuntimeError, ValueError):
            return False
        return True

    def execute(
        self,
        *,
        artifact: PublishedTop5BatchArtifact,
        current_public_snapshot: Mapping[str, object],
        plan: Top5DeliveryPlan,
        attestation: Top5DeliveryAttestation,
        capability: Mapping[str, object],
        dry_run: bool = True,
    ) -> Top5DeliveryExecutionResult:
        plan.validate()
        if plan.artifact_digest != artifact.artifact_digest:
            raise Top5DeliveryError("delivery plan artifact digest mismatch")
        attestation.validate(
            plan=plan,
            artifact=artifact,
            capability=capability,
            now=self.clock(),
        )
        current, current_status = self._current_state(current_public_snapshot, plan)
        if current_status == TOP5_DELIVERY_IDEMPOTENT:
            if self.static_transport is not None and self.worker_transport is not None:
                expected = plan.serialized_payload
                if (
                    self._canonical_transport_payload(self.static_transport.current_payload())
                    != expected
                    or self._canonical_transport_payload(self.worker_transport.current_payload())
                    != expected
                ):
                    raise Top5DeliveryError(
                        "idempotent delivery target digest does not match the authorized payload"
                    )
            return Top5DeliveryExecutionResult(
                TOP5_DELIVERY_IDEMPOTENT,
                (TOP5_DELIVERY_PREPARED,),
                plan.generation_id,
                plan.public_product_digest,
            )
        if dry_run:
            return Top5DeliveryExecutionResult(
                TOP5_DELIVERY_DRY_RUN,
                (TOP5_DELIVERY_PREPARED,),
                plan.generation_id,
                plan.public_product_digest,
            )
        if self.static_transport is None or self.worker_transport is None:
            raise Top5DeliveryError("execute requires both governed delivery transports")
        if self.capability_consumer is None:
            raise Top5DeliveryError(
                "execute requires existing controlled publication capability validation"
            )

        states = [TOP5_DELIVERY_PREPARED]
        expected_current = _canonical_bytes(current)
        static_current = self.static_transport.current_payload()
        previous_worker = self.worker_transport.current_payload()
        if (
            self._canonical_transport_payload(static_current) != expected_current
            or self._canonical_transport_payload(previous_worker) != expected_current
        ):
            raise Top5DeliveryError(
                "current static/Worker payload digest does not match the canonical snapshot"
            )
        # Consume the one-time publication capability only after every
        # pre-mutation target check has passed. A drifted static/Worker target
        # must not burn the operator authorization or leave a retry without a
        # valid capability.
        self.capability_consumer.consume(
            attestation,
            capability,
            artifact,
            self.clock(),
        )
        try:
            self.static_transport.stage(plan.static_path, plan.static_payload)
            states.append(TOP5_DELIVERY_STATIC_STAGED)
        except (Top5DeliveryError, OSError, RuntimeError, ValueError) as exc:
            if not self._rollback_static_stage():
                return Top5DeliveryExecutionResult(
                    TOP5_DELIVERY_ROLLBACK_REQUIRED,
                    tuple(states),
                    plan.generation_id,
                    plan.public_product_digest,
                    "static stage and cleanup both failed",
                )
            return Top5DeliveryExecutionResult(
                TOP5_DELIVERY_ROLLBACK_SUCCEEDED,
                tuple(states),
                plan.generation_id,
                plan.public_product_digest,
                str(exc),
            )
        try:
            self.worker_transport.write_signals(plan.worker_payload)
            states.append(TOP5_DELIVERY_WORKER_WRITTEN)
        except (Top5DeliveryError, OSError, RuntimeError, ValueError) as exc:
            # A transport may have applied a remote write before surfacing an
            # error. Restore both targets; a failed restoration is an explicit
            # rollback-required outcome, never a successful delivery.
            worker_restored = self._restore_worker(previous_worker)
            static_rolled_back = self._rollback_static_stage()
            if not worker_restored or not static_rolled_back:
                return Top5DeliveryExecutionResult(
                    TOP5_DELIVERY_ROLLBACK_REQUIRED,
                    tuple(states),
                    plan.generation_id,
                    plan.public_product_digest,
                    "Worker failed and target restoration failed",
                    worker_restored,
                )
            return Top5DeliveryExecutionResult(
                TOP5_DELIVERY_ROLLBACK_SUCCEEDED,
                tuple(states),
                plan.generation_id,
                plan.public_product_digest,
                str(exc),
                worker_restored,
            )
        try:
            self.static_transport.commit()
            states.append(TOP5_DELIVERY_STATIC_COMMITTED)
        except (Top5DeliveryError, OSError, RuntimeError, ValueError) as exc:
            worker_restored = self._restore_worker(previous_worker)
            self._rollback_static_stage()
            return Top5DeliveryExecutionResult(
                TOP5_DELIVERY_ROLLBACK_REQUIRED,
                tuple(states),
                plan.generation_id,
                plan.public_product_digest,
                str(exc),
                worker_restored,
            )
        return Top5DeliveryExecutionResult(
            TOP5_DELIVERY_ACCEPTANCE_REQUIRED,
            tuple(states) + (TOP5_DELIVERY_ACCEPTANCE_REQUIRED,),
            plan.generation_id,
            plan.public_product_digest,
        )


@dataclass(frozen=True)
class Top5DeliveryCommit:
    """Result of the in-memory transaction used by offline acceptance tests."""

    status: str
    generation_id: str
    public_product_digest: str
    rolled_back: bool = False


class InMemoryTop5DeliveryTransaction:
    """Failure/rollback test double; it never touches runtime files or APIs."""

    def __init__(self, *, initial_payload: bytes = b"safe-generation") -> None:
        self._static_payload = bytes(initial_payload)
        self._worker_payload = bytes(initial_payload)
        self._committed_digest: str | None = None

    @property
    def static_payload(self) -> bytes:
        return self._static_payload

    @property
    def worker_payload(self) -> bytes:
        return self._worker_payload

    @property
    def committed_digest(self) -> str | None:
        return self._committed_digest

    def stage(
        self,
        plan: Top5DeliveryPlan,
        *,
        fail_static: bool = False,
        fail_worker: bool = False,
    ) -> Top5DeliveryCommit:
        previous_static = self._static_payload
        previous_worker = self._worker_payload
        previous_digest = self._committed_digest
        if previous_digest == plan.public_product_digest:
            return Top5DeliveryCommit(
                "TOP5_DELIVERY_IDEMPOTENT",
                plan.generation_id,
                plan.public_product_digest,
            )
        try:
            if fail_static:
                raise Top5DeliveryError("static staging failed")
            self._static_payload = plan.static_payload
            if fail_worker:
                raise Top5DeliveryError("Worker staging failed")
            self._worker_payload = plan.worker_payload
        except Top5DeliveryError:
            self._static_payload = previous_static
            self._worker_payload = previous_worker
            self._committed_digest = previous_digest
            raise
        self._committed_digest = plan.public_product_digest
        return Top5DeliveryCommit(
            "TOP5_DELIVERY_STAGED",
            plan.generation_id,
            plan.public_product_digest,
        )

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

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from src.football.production_contracts import ProductionContractError
from src.football.top5_publisher import (
    TOP5_PUBLIC_RELEASE_LEAGUES,
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
    static_path: str = TOP5_DELIVERY_STATIC_PATH
    worker_path: str = TOP5_DELIVERY_WORKER_PATH

    @property
    def static_payload(self) -> bytes:
        return self.serialized_payload

    @property
    def worker_payload(self) -> bytes:
        return self.serialized_payload

    def manifest(self) -> dict[str, object]:
        return {
            "schema_version": "top5-delivery-plan-v1",
            "generation_id": self.generation_id,
            "activation_id": self.activation_id,
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
        return Top5DeliveryPlan(
            public_product=MappingProxyType(
                {str(key): _freeze(value) for key, value in serialized_product.items()}
            ),
            serialized_payload=serialized_payload,
            public_product_digest=digest,
            generation_id=str(release["generation_id"]),
            activation_id=str(release["activation_id"]),
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

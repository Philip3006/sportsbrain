"""Strict, read-only acceptance of supplied Top-5 production evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.top5_publication_delivery_acceptance import (
    ACTIVE_PROVIDER,
    LEAGUES,
    DeliveryAcceptanceError,
    _assert_no_candidate_provider,
    _canonical_digest,
    _exact_leagues,
    _release,
    _required_text,
    _timestamp,
    _validate_payload,
    validate_delivery,
)

STATUS_PRODUCTION_VERIFIED = "TOP5_PRODUCTION_ACCEPTANCE_VERIFIED"
RUNTIME_SCHEMA = "top5-runtime-acceptance-v1"


def _validate_manifest(
    manifest: Mapping[str, Any], *, product_digest: str, release: Mapping[str, Any]
) -> None:
    required = {
        "public_product_digest",
        "static_payload_digest",
        "worker_payload_digest",
        "generation_id",
        "activation_id",
        "provider_authority",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "publication_authorization_id",
        "artifact_digest",
        "source_snapshot_digest",
        "static_path",
        "worker_path",
    }
    if not isinstance(manifest, Mapping) or not required.issubset(manifest):
        raise DeliveryAcceptanceError("delivery manifest is incomplete")
    for field in (
        "artifact_digest",
        "source_snapshot_digest",
        "static_path",
        "worker_path",
    ):
        _required_text(manifest.get(field), f"delivery_manifest.{field}")
    for field in (
        "public_product_digest",
        "static_payload_digest",
        "worker_payload_digest",
    ):
        if manifest.get(field) != product_digest:
            raise DeliveryAcceptanceError(f"delivery manifest {field} mismatch")
    for field in (
        "generation_id",
        "activation_id",
        "provider_authority",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "publication_authorization_id",
    ):
        if manifest.get(field) != release.get(field):
            raise DeliveryAcceptanceError(f"delivery manifest {field} mismatch")
    _assert_no_candidate_provider(manifest)


def _validate_capability(
    capability: Mapping[str, Any], *, attestation: Mapping[str, Any]
) -> None:
    if set(capability) != {"capability_id", "capability_nonce"}:
        raise DeliveryAcceptanceError(
            "controlled publication capability has an invalid shape"
        )
    capability_id = _required_text(
        capability.get("capability_id"), "capability.capability_id"
    )
    nonce = _required_text(
        capability.get("capability_nonce"), "capability.capability_nonce"
    )
    if len(nonce) < 32:
        raise DeliveryAcceptanceError(
            "controlled publication capability nonce is too short"
        )
    if capability_id != attestation.get("capability_id"):
        raise DeliveryAcceptanceError("capability identity mismatch")
    nonce_digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    if nonce_digest != attestation.get("capability_nonce_digest"):
        raise DeliveryAcceptanceError("capability nonce binding mismatch")


def _validate_attestation(
    attestation: Mapping[str, Any],
    *,
    release: Mapping[str, Any],
    manifest: Mapping[str, Any],
    expected_now: datetime,
) -> None:
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
    if (
        not isinstance(attestation, Mapping)
        or not required.issubset(attestation)
        or set(attestation) - required - optional
    ):
        raise DeliveryAcceptanceError(
            "Top-5 delivery attestation has an invalid shape"
        )
    if attestation.get("schema_version") != "top5-delivery-attestation-v1":
        raise DeliveryAcceptanceError(
            "Top-5 delivery attestation schema is unsupported"
        )
    if (
        attestation.get("publication_authorized") is not True
        or attestation.get("no_bet") is not True
    ):
        raise DeliveryAcceptanceError(
            "Top-5 delivery attestation safety state is invalid"
        )
    if attestation.get("artifact_digest") != manifest.get("artifact_digest"):
        raise DeliveryAcceptanceError("Top-5 delivery artifact digest mismatch")
    for field in (
        "generation_id",
        "activation_id",
        "provider_authority",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "publication_authorization_id",
    ):
        if attestation.get(field) != release.get(field):
            raise DeliveryAcceptanceError(f"attestation {field} mismatch")
    _exact_leagues(attestation.get("league_codes"), "attestation.league_codes")
    issued_at = _timestamp(attestation.get("issued_at"), "attestation.issued_at")
    expires_at = _timestamp(attestation.get("expires_at"), "attestation.expires_at")
    if not issued_at <= expected_now <= expires_at:
        raise DeliveryAcceptanceError(
            "Top-5 delivery attestation is expired or not yet valid"
        )
    unsigned = {
        key: value
        for key, value in attestation.items()
        if key != "attestation_digest"
    }
    if attestation.get("attestation_digest") != _canonical_digest(unsigned):
        raise DeliveryAcceptanceError("Top-5 delivery attestation digest mismatch")
    nested = attestation.get("controlled_publication_attestation")
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise DeliveryAcceptanceError(
                "controlled publication attestation is malformed"
            )
        if (
            nested.get("active_activation") is not True
            or nested.get("publication_authorized") is not True
            or nested.get("no_bet") is not True
            or nested.get("activation_id") != release.get("activation_id")
            or nested.get("provider_authority") != ACTIVE_PROVIDER
        ):
            raise DeliveryAcceptanceError(
                "controlled publication attestation binding mismatch"
            )
    _assert_no_candidate_provider(attestation)


def _validate_runtime_evidence(
    runtime: Mapping[str, Any],
    *,
    expected_runtime_root: str,
    expected_now: datetime,
    source_sha: str,
    max_age_seconds: float,
) -> None:
    required = {
        "schema_version",
        "runtime_root",
        "runtime_role",
        "checkout_clean",
        "publisher_clean",
        "health_authority",
        "health_status",
        "active_provider_order",
        "captured_at",
        "source_release_sha",
        "runtime_data_sha",
        "source_runtime_consistent",
    }
    if not isinstance(runtime, Mapping) or not required.issubset(runtime):
        raise DeliveryAcceptanceError("runtime evidence is incomplete")
    runtime_root = _required_text(runtime.get("runtime_root"), "runtime.runtime_root")
    expected_root = _required_text(expected_runtime_root, "expected_runtime_root")
    if not Path(runtime_root).is_absolute() or runtime_root != expected_root:
        raise DeliveryAcceptanceError(
            "runtime root does not match the governed runtime"
        )
    if (
        runtime.get("schema_version") != RUNTIME_SCHEMA
        or runtime.get("runtime_role") != "governed-runtime"
    ):
        raise DeliveryAcceptanceError(
            "runtime evidence is not governed-runtime evidence"
        )
    if (
        runtime.get("checkout_clean") is not True
        or runtime.get("publisher_clean") is not True
    ):
        raise DeliveryAcceptanceError("runtime checkout or publisher is not clean")
    if runtime.get("health_authority") not in {"local", "governed-runtime"}:
        raise DeliveryAcceptanceError("runtime health authority is not governed")
    if runtime.get("health_status") not in {"ok", "degraded"}:
        raise DeliveryAcceptanceError("runtime health status is not acceptable")
    provider_order = runtime.get("active_provider_order")
    if (
        not isinstance(provider_order, Sequence)
        or isinstance(provider_order, (str, bytes))
        or ACTIVE_PROVIDER not in provider_order
    ):
        raise DeliveryAcceptanceError("runtime provider authority is incomplete")
    captured_at = _timestamp(runtime.get("captured_at"), "runtime.captured_at")
    age = (expected_now - captured_at).total_seconds()
    if age < 0 or age > max_age_seconds:
        raise DeliveryAcceptanceError("runtime evidence is stale or from the future")
    if runtime.get("source_release_sha") != source_sha:
        raise DeliveryAcceptanceError(
            "runtime source release does not match product provenance"
        )
    _required_text(runtime.get("runtime_data_sha"), "runtime.runtime_data_sha")
    if runtime.get("source_runtime_consistent") is not True:
        raise DeliveryAcceptanceError("runtime/source provenance is unresolved")
    _assert_no_candidate_provider(runtime)


def validate_production_acceptance(
    worker_payload: dict[str, Any],
    static_payload: dict[str, Any],
    attestation: Mapping[str, Any],
    capability: Mapping[str, Any],
    runtime_evidence: Mapping[str, Any],
    *,
    expected_runtime_root: str,
    worker_status: int,
    pwa_status: int,
    now: datetime,
    delivery_manifest: Mapping[str, Any],
    max_runtime_age_seconds: float = 900,
) -> dict[str, Any]:
    """Verify supplied production evidence without contacting or mutating anything."""
    if not isinstance(worker_status, int) or isinstance(worker_status, bool):
        raise DeliveryAcceptanceError("worker status is invalid")
    if not isinstance(pwa_status, int) or isinstance(pwa_status, bool):
        raise DeliveryAcceptanceError("PWA status is invalid")
    expected_now = _timestamp(now.isoformat(), "now")
    delivery = validate_delivery(
        worker_payload,
        static_payload,
        attestation,
        expected_provider=ACTIVE_PROVIDER,
        worker_status=worker_status,
        pwa_status=pwa_status,
        now=expected_now,
        delivery_manifest=delivery_manifest,
    )
    release = _release(worker_payload)
    _validate_payload(
        worker_payload,
        source="worker",
        expected_provider=ACTIVE_PROVIDER,
        expected_now=expected_now,
        require_health_provenance=True,
    )
    _validate_payload(
        static_payload,
        source="static",
        expected_provider=ACTIVE_PROVIDER,
        expected_now=expected_now,
        require_health_provenance=True,
    )
    _validate_manifest(
        delivery_manifest,
        product_digest=delivery["public_product_digest"],
        release=release,
    )
    _validate_attestation(
        attestation,
        release=release,
        manifest=delivery_manifest,
        expected_now=expected_now,
    )
    _validate_capability(capability, attestation=attestation)
    first_record = next(
        record
        for record in worker_payload["football"]
        if isinstance(record, Mapping)
        and str(record.get("league", "")).upper() in LEAGUES
    )
    _validate_runtime_evidence(
        runtime_evidence,
        expected_runtime_root=expected_runtime_root,
        expected_now=expected_now,
        source_sha=first_record["provenance"]["source_sha"],
        max_age_seconds=max_runtime_age_seconds,
    )
    return {
        **delivery,
        "status": STATUS_PRODUCTION_VERIFIED,
        "attestation_digest": attestation["attestation_digest"],
        "capability_id": attestation["capability_id"],
        "runtime_root": expected_runtime_root,
    }

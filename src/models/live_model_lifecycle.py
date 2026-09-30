"""Domain-neutral, fail-closed model-release lifecycle primitives.

The module contains no trainer, provider, scheduler, publication or betting
code.  A caller supplies already verified, causal training state and may only
advance it through a deterministic technical validation and atomic pointer
change.  Product/financial authority is deliberately outside this contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "sportsbrain-live-model-lifecycle-v1"
TRAINING = "TRAINING"
VALIDATED = "VALIDATED"
ACTIVE = "ACTIVE"
REJECTED = "REJECTED"
_STATES = frozenset({TRAINING, VALIDATED, ACTIVE, REJECTED})


class ModelLifecycleError(ValueError):
    """A release cannot safely be built, promoted, read or rolled back."""


def canonical_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelLifecycleError("lifecycle payload is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _sha(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ModelLifecycleError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ModelLifecycleError(f"{field} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelLifecycleError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ModelLifecycleError(f"{field} must be UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def training_snapshot(
    *,
    model_family: str,
    algorithm_version: str,
    algorithm_digest: str,
    training_digest: str,
    training_cutoff: str,
    training_rows: int,
    result_watermark: str,
    feature_schema_digest: str,
    created_at: str,
    parent_release_id: str | None = None,
) -> dict[str, Any]:
    """Seal the causal input which a model state is allowed to consume."""
    if not isinstance(model_family, str) or not model_family:
        raise ModelLifecycleError("model_family is required")
    if not isinstance(algorithm_version, str) or not algorithm_version:
        raise ModelLifecycleError("algorithm_version is required")
    if (
        not isinstance(training_rows, int)
        or isinstance(training_rows, bool)
        or training_rows < 1
    ):
        raise ModelLifecycleError("training_rows must be a positive integer")
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "training_snapshot",
        "model_family": model_family,
        "algorithm_version": algorithm_version,
        "algorithm_digest": _sha(algorithm_digest, "algorithm_digest"),
        "training_digest": _sha(training_digest, "training_digest"),
        "training_cutoff": _timestamp(training_cutoff, "training_cutoff"),
        "training_rows": training_rows,
        "result_watermark": _timestamp(result_watermark, "result_watermark"),
        "feature_schema_digest": _sha(feature_schema_digest, "feature_schema_digest"),
        "created_at": _timestamp(created_at, "created_at"),
        "parent_release_id": parent_release_id,
    }
    if payload["result_watermark"] > payload["training_cutoff"]:
        raise ModelLifecycleError("result watermark exceeds causal training cutoff")
    if parent_release_id is not None and (
        not isinstance(parent_release_id, str) or not parent_release_id
    ):
        raise ModelLifecycleError("parent_release_id is malformed")
    payload["training_snapshot_id"] = canonical_digest(payload)
    return payload


def validate_training_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "kind",
        "model_family",
        "algorithm_version",
        "algorithm_digest",
        "training_digest",
        "training_cutoff",
        "training_rows",
        "result_watermark",
        "feature_schema_digest",
        "created_at",
        "parent_release_id",
        "training_snapshot_id",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ModelLifecycleError("training snapshot fields are not allowlisted")
    rebuilt = training_snapshot(
        **{
            key: value[key]
            for key in required - {"schema", "kind", "training_snapshot_id"}
        }
    )
    if rebuilt != dict(value):
        raise ModelLifecycleError("training snapshot digest mismatch")
    return rebuilt


def model_release(
    snapshot: Mapping[str, Any],
    *,
    state_digest: str,
    created_at: str,
    parent_release_id: str | None = None,
) -> dict[str, Any]:
    """Create an immutable TRAINING release; validation/promotion is separate."""
    sealed = validate_training_snapshot(snapshot)
    payload = {
        "schema": SCHEMA,
        "kind": "model_release",
        "release_state": TRAINING,
        "model_family": sealed["model_family"],
        "algorithm_version": sealed["algorithm_version"],
        "algorithm_digest": sealed["algorithm_digest"],
        "training_snapshot_id": sealed["training_snapshot_id"],
        "training_digest": sealed["training_digest"],
        "training_cutoff": sealed["training_cutoff"],
        "training_rows": sealed["training_rows"],
        "result_watermark": sealed["result_watermark"],
        "feature_schema_digest": sealed["feature_schema_digest"],
        "state_digest": _sha(state_digest, "state_digest"),
        "created_at": _timestamp(created_at, "created_at"),
        "parent_release_id": parent_release_id
        if parent_release_id is not None
        else sealed["parent_release_id"],
    }
    payload["release_id"] = canonical_digest(payload)
    return payload


def validate_release(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "kind",
        "release_state",
        "model_family",
        "algorithm_version",
        "algorithm_digest",
        "training_snapshot_id",
        "training_digest",
        "training_cutoff",
        "training_rows",
        "result_watermark",
        "feature_schema_digest",
        "state_digest",
        "created_at",
        "parent_release_id",
        "release_id",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ModelLifecycleError("model release fields are not allowlisted")
    if value.get("release_state") not in _STATES:
        raise ModelLifecycleError("release state is unsupported")
    base = dict(value)
    release_id = base.pop("release_id")
    for field in (
        "algorithm_digest",
        "training_digest",
        "feature_schema_digest",
        "state_digest",
    ):
        _sha(base[field], field)
    _sha(base["training_snapshot_id"], "training_snapshot_id")
    _timestamp(base["training_cutoff"], "training_cutoff")
    _timestamp(base["result_watermark"], "result_watermark")
    _timestamp(base["created_at"], "created_at")
    if base["result_watermark"] > base["training_cutoff"]:
        raise ModelLifecycleError("release contains noncausal result watermark")
    if (
        not isinstance(base["training_rows"], int)
        or isinstance(base["training_rows"], bool)
        or base["training_rows"] < 1
    ):
        raise ModelLifecycleError("release training rows are invalid")
    if not isinstance(base["model_family"], str) or not base["model_family"]:
        raise ModelLifecycleError("release model family is invalid")
    if canonical_digest(base) != release_id:
        raise ModelLifecycleError("model release identity mismatch")
    return deepcopy(dict(value))


def transition_release(release: Mapping[str, Any], state: str) -> dict[str, Any]:
    """Return a new immutable state record for the same trained model state."""
    sealed = validate_release(release)
    allowed = {
        TRAINING: {VALIDATED, REJECTED},
        VALIDATED: {ACTIVE, REJECTED},
        ACTIVE: set(),
        REJECTED: set(),
    }
    if state not in _STATES or state not in allowed[sealed["release_state"]]:
        raise ModelLifecycleError("release transition is not permitted")
    updated = dict(sealed)
    updated["release_state"] = state
    updated.pop("release_id")
    updated["release_id"] = canonical_digest(updated)
    return updated


def validate_technical(
    release: Mapping[str, Any], *, checked_at: str
) -> dict[str, Any]:
    """Technical validation deliberately has no sample-size/promotion policy."""
    sealed = validate_release(release)
    if sealed["release_state"] != TRAINING:
        raise ModelLifecycleError("only training releases can be technically validated")
    validated = transition_release(sealed, VALIDATED)
    return {
        "schema": SCHEMA,
        "kind": "validation_receipt",
        "release_id": validated["release_id"],
        "training_release_id": sealed["release_id"],
        "model_family": sealed["model_family"],
        "technical_validation": "PASS",
        "sample_threshold_required": False,
        "checked_at": _timestamp(checked_at, "checked_at"),
        "receipt_digest": canonical_digest(
            {
                "release_id": validated["release_id"],
                "training_release_id": sealed["release_id"],
                "checked_at": _timestamp(checked_at, "checked_at"),
                "technical_validation": "PASS",
                "sample_threshold_required": False,
            }
        ),
        "validated_release": validated,
    }


def activation_receipt(
    release: Mapping[str, Any],
    *,
    previous_release_id: str | None,
    activated_at: str,
    reason: str,
) -> dict[str, Any]:
    sealed = validate_release(release)
    if sealed["release_state"] not in {VALIDATED, ACTIVE}:
        raise ModelLifecycleError("only validated releases can activate")
    if not isinstance(reason, str) or not reason:
        raise ModelLifecycleError("activation reason is required")
    payload = {
        "schema": SCHEMA,
        "kind": "activation_receipt",
        "model_family": sealed["model_family"],
        "release_id": sealed["release_id"],
        "previous_release_id": previous_release_id,
        "activated_at": _timestamp(activated_at, "activated_at"),
        "reason": reason,
        "no_bet_authority": True,
        "no_publication_authority": True,
    }
    payload["activation_receipt_id"] = canonical_digest(payload)
    return payload


def active_pointer(
    release: Mapping[str, Any],
    *,
    previous_release_id: str | None,
    activated_at: str,
    reason: str,
) -> dict[str, Any]:
    receipt = activation_receipt(
        release,
        previous_release_id=previous_release_id,
        activated_at=activated_at,
        reason=reason,
    )
    payload = {
        "schema": SCHEMA,
        "kind": "active_model_pointer",
        "model_family": receipt["model_family"],
        "active_release_id": receipt["release_id"],
        "previous_release_id": previous_release_id,
        "activated_at": receipt["activated_at"],
        "activation_receipt_id": receipt["activation_receipt_id"],
    }
    payload["pointer_digest"] = canonical_digest(payload)
    return payload


def validate_active_pointer(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "kind",
        "model_family",
        "active_release_id",
        "previous_release_id",
        "activated_at",
        "activation_receipt_id",
        "pointer_digest",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ModelLifecycleError("active pointer fields are not allowlisted")
    raw = dict(value)
    digest = raw.pop("pointer_digest")
    if (
        raw.get("schema") != SCHEMA
        or raw.get("kind") != "active_model_pointer"
        or canonical_digest(raw) != digest
    ):
        raise ModelLifecycleError("active pointer digest mismatch")
    _sha(raw["active_release_id"], "active_release_id")
    _sha(raw["activation_receipt_id"], "activation_receipt_id")
    _timestamp(raw["activated_at"], "activated_at")
    return deepcopy(dict(value))


def retrain_receipt(
    snapshot: Mapping[str, Any],
    release: Mapping[str, Any],
    *,
    triggered_by: str,
    created_at: str,
) -> dict[str, Any]:
    snap, rel = validate_training_snapshot(snapshot), validate_release(release)
    if rel["training_snapshot_id"] != snap["training_snapshot_id"]:
        raise ModelLifecycleError("release does not bind supplied training snapshot")
    if triggered_by != "RESULT_WATERMARK_ADVANCED":
        raise ModelLifecycleError("only result watermark advancement may retrain")
    payload = {
        "schema": SCHEMA,
        "kind": "retrain_receipt",
        "model_family": rel["model_family"],
        "training_snapshot_id": snap["training_snapshot_id"],
        "release_id": rel["release_id"],
        "triggered_by": triggered_by,
        "created_at": _timestamp(created_at, "created_at"),
    }
    payload["retrain_receipt_id"] = canonical_digest(payload)
    return payload


class FileActiveModelPointer:
    """Small atomic pointer store. Used with temp paths in tests; no default path."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return validate_active_pointer(
            json.loads(self.path.read_text(encoding="utf-8"))
        )

    def activate(
        self, release: Mapping[str, Any], *, activated_at: str, reason: str
    ) -> dict[str, Any]:
        prior = self.read()
        if prior and prior["model_family"] != release.get("model_family"):
            raise ModelLifecycleError("atomic pointer family mismatch")
        active = (
            transition_release(release, ACTIVE)
            if release.get("release_state") == VALIDATED
            else release
        )
        pointer = active_pointer(
            active,
            previous_release_id=prior["active_release_id"] if prior else None,
            activated_at=activated_at,
            reason=reason,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".model-pointer-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(pointer, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return pointer

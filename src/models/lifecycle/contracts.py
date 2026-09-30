"""Immutable release, validation, pointer, and rollback primitives.

This is intentionally a small in-process state machine.  Its serialized
objects are portable JSON and its caller owns persistence.  No state transition
can contact a network service or mutate a prediction, publication, betting, or
ledger system.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

TRAINING = "TRAINING"
VALIDATED = "VALIDATED"
ACTIVE = "ACTIVE"
REJECTED = "REJECTED"
_DIGEST_LENGTH = 64
_VALID_STATUSES = frozenset({TRAINING, VALIDATED, ACTIVE, REJECTED})


class LifecycleError(ValueError):
    """Raised when immutable lifecycle evidence or a transition is invalid."""


def canonical_json(value: Any) -> bytes:
    """Return the one canonical JSON encoding used for release identity."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LifecycleError("lifecycle value is not canonical JSON") from exc


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _utc(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise LifecycleError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise LifecycleError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _DIGEST_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise LifecycleError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(f"{field} is required")
    return value


def validate_causal_training_rows(
    rows: Iterable[Mapping[str, Any]], training_cutoff: str
) -> tuple[dict[str, Any], ...]:
    """Validate the non-negotiable result-availability causal boundary.

    Adapters remain responsible for their own fixture/result schema.  The
    lifecycle layer only requires a stable row identity and a timestamp proving
    the result was available *strictly before* training begins.
    """

    cutoff = _utc(training_cutoff, "training_cutoff")
    checked: list[dict[str, Any]] = []
    identities: set[str] = set()
    for raw in rows:
        row = dict(raw)
        identity = _nonempty(row.get("fixture_id"), "training fixture_id")
        if identity in identities:
            raise LifecycleError("duplicate training fixture_id")
        identities.add(identity)
        available = _utc(
            row.get("result_safe_available_at"), "result_safe_available_at"
        )
        if not available < cutoff:
            raise LifecycleError(
                "result_safe_available_at must be strictly before training_cutoff"
            )
        checked.append(row)
    return tuple(checked)


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, Mapping):
        return all(_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite(item) for item in value)
    return False


def _probability_smoke(value: Mapping[str, Any] | None) -> None:
    if value is None:
        return
    required = {"home", "draw", "away"}
    if set(value) != required:
        raise LifecycleError("prediction smoke output must be home/draw/away")
    probabilities = [value[key] for key in ("home", "draw", "away")]
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in probabilities):
        raise LifecycleError("prediction smoke output is not numeric")
    if any(not math.isfinite(float(item)) or not 0.0 <= float(item) <= 1.0 for item in probabilities):
        raise LifecycleError("prediction smoke output is outside [0, 1]")
    if abs(sum(float(item) for item in probabilities) - 1.0) > 1e-9:
        raise LifecycleError("prediction smoke output is not normalized")


@dataclass(frozen=True)
class TrainingSnapshot:
    """The immutable, causally bounded source state for one release."""

    model_family: str
    sport: str
    scope: str
    algorithm_version: str
    algorithm_digest: str
    training_data_digest: str
    training_cutoff: str
    training_row_count: int
    result_safe_watermark: str
    feature_schema_digest: str
    trained_state_digest: str
    source_release_sha: str
    captured_at: str

    def __post_init__(self) -> None:
        for field in (
            "model_family",
            "sport",
            "scope",
            "algorithm_version",
            "source_release_sha",
        ):
            _nonempty(getattr(self, field), field)
        for field in (
            "algorithm_digest",
            "training_data_digest",
            "feature_schema_digest",
            "trained_state_digest",
        ):
            _digest(getattr(self, field), field)
        if (
            not isinstance(self.training_row_count, int)
            or isinstance(self.training_row_count, bool)
            or self.training_row_count < 1
        ):
            raise LifecycleError("training_row_count must be a positive integer")
        cutoff = _utc(self.training_cutoff, "training_cutoff")
        watermark = _utc(self.result_safe_watermark, "result_safe_watermark")
        _utc(self.captured_at, "captured_at")
        if watermark >= cutoff:
            raise LifecycleError("result_safe_watermark must precede training_cutoff")

    def identity_payload(self) -> dict[str, Any]:
        """Fields that determine a release; capture time intentionally does not."""

        return {
            "model_family": self.model_family,
            "sport": self.sport,
            "scope": self.scope,
            "algorithm_version": self.algorithm_version,
            "algorithm_digest": self.algorithm_digest,
            "training_data_digest": self.training_data_digest,
            "training_cutoff": self.training_cutoff,
            "training_row_count": self.training_row_count,
            "result_safe_watermark": self.result_safe_watermark,
            "feature_schema_digest": self.feature_schema_digest,
            "trained_state_digest": self.trained_state_digest,
            "source_release_sha": self.source_release_sha,
        }

    @property
    def snapshot_digest(self) -> str:
        return canonical_digest(self.identity_payload())

    def to_payload(self) -> dict[str, Any]:
        return {**self.identity_payload(), "captured_at": self.captured_at, "snapshot_digest": self.snapshot_digest}


@dataclass(frozen=True)
class ModelRelease:
    """An immutable release; its identifier excludes mutable lifecycle state."""

    release_id: str
    snapshot: TrainingSnapshot
    parameter_digest: str
    status: str = TRAINING
    parent_release_id: str | None = None
    validation_digest: str | None = None
    rejection_reason: str | None = None

    @classmethod
    def create(
        cls,
        snapshot: TrainingSnapshot,
        *,
        parameter_digest: str,
        parent_release_id: str | None = None,
    ) -> ModelRelease:
        _digest(parameter_digest, "parameter_digest")
        if parent_release_id is not None:
            _digest(parent_release_id, "parent_release_id")
        release_id = canonical_digest(
            {
                "release_schema": "model-release-v1",
                "snapshot": snapshot.identity_payload(),
                "parameter_digest": parameter_digest,
                "parent_release_id": parent_release_id,
            }
        )
        return cls(
            release_id=release_id,
            snapshot=snapshot,
            parameter_digest=parameter_digest,
            parent_release_id=parent_release_id,
        )

    def __post_init__(self) -> None:
        _digest(self.release_id, "release_id")
        _digest(self.parameter_digest, "parameter_digest")
        if self.parent_release_id is not None:
            _digest(self.parent_release_id, "parent_release_id")
        if self.status not in _VALID_STATUSES:
            raise LifecycleError("unsupported release status")
        if self.validation_digest is not None:
            _digest(self.validation_digest, "validation_digest")

    def to_payload(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "model_family": self.snapshot.model_family,
            "algorithm_version": self.snapshot.algorithm_version,
            "algorithm_digest": self.snapshot.algorithm_digest,
            "training_snapshot": self.snapshot.to_payload(),
            "parameter_digest": self.parameter_digest,
            "status": self.status,
            "parent_release_id": self.parent_release_id,
            "validation_digest": self.validation_digest,
            "rejection_reason": self.rejection_reason,
        }


@dataclass(frozen=True)
class ActiveModelPointer:
    model_family: str
    release_id: str
    previous_release_id: str | None
    activated_at: str
    revision: int

    @property
    def pointer_digest(self) -> str:
        return canonical_digest(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "model_family": self.model_family,
            "release_id": self.release_id,
            "previous_release_id": self.previous_release_id,
            "activated_at": self.activated_at,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class RetrainReceipt:
    receipt_id: str
    model_family: str
    release_id: str
    outcome: str
    training_snapshot_digest: str
    created_at: str
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ActivationReceipt:
    receipt_id: str
    model_family: str
    release_id: str
    previous_release_id: str | None
    action: str
    pointer_digest: str
    activated_at: str

    def to_payload(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ModelHealth:
    model_family: str
    status: str
    active_release_id: str | None
    active_training_cutoff: str | None
    last_result_watermark: str | None
    training_row_count: int | None
    last_successful_retrain: str | None
    last_retrain_outcome: str | None
    last_failure_reason: str | None
    updated_at: str

    def to_payload(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ModelLifecycle:
    """Thread-safe lifecycle state with atomic active-pointer swaps.

    Persistence is intentionally outside this class.  A caller serializes
    ``to_payload`` only after a completed operation, never a partial transition.
    """

    def __init__(self) -> None:
        self._releases: dict[str, ModelRelease] = {}
        self._pointers: dict[str, ActiveModelPointer] = {}
        self._receipts: list[RetrainReceipt | ActivationReceipt] = []
        self._lock = threading.RLock()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ModelLifecycle:
        """Restore an already validated lifecycle without replaying transitions.

        Persistence adapters use this for the committed registry.  The
        serialized state is revalidated in full before it becomes readable;
        no release is created, activated, or rewritten during restoration.
        """

        if not isinstance(payload, Mapping) or payload.get("schema") != "continuous-model-lifecycle-state-v1":
            raise LifecycleError("unsupported lifecycle state schema")
        release_rows = payload.get("releases")
        pointer_rows = payload.get("active_pointers")
        receipt_rows = payload.get("receipts")
        if not all(isinstance(value, list) for value in (release_rows, pointer_rows, receipt_rows)):
            raise LifecycleError("lifecycle state collections are missing")
        lifecycle = cls()
        releases: dict[str, ModelRelease] = {}
        for raw in release_rows:
            if not isinstance(raw, Mapping):
                raise LifecycleError("lifecycle release is malformed")
            snapshot_raw = raw.get("training_snapshot")
            if not isinstance(snapshot_raw, Mapping):
                raise LifecycleError("lifecycle training snapshot is missing")
            snapshot = TrainingSnapshot(
                model_family=_nonempty(snapshot_raw.get("model_family"), "model_family"),
                sport=_nonempty(snapshot_raw.get("sport"), "sport"),
                scope=_nonempty(snapshot_raw.get("scope"), "scope"),
                algorithm_version=_nonempty(snapshot_raw.get("algorithm_version"), "algorithm_version"),
                algorithm_digest=_digest(snapshot_raw.get("algorithm_digest"), "algorithm_digest"),
                training_data_digest=_digest(snapshot_raw.get("training_data_digest"), "training_data_digest"),
                training_cutoff=_nonempty(snapshot_raw.get("training_cutoff"), "training_cutoff"),
                training_row_count=snapshot_raw.get("training_row_count"),
                result_safe_watermark=_nonempty(snapshot_raw.get("result_safe_watermark"), "result_safe_watermark"),
                feature_schema_digest=_digest(snapshot_raw.get("feature_schema_digest"), "feature_schema_digest"),
                trained_state_digest=_digest(snapshot_raw.get("trained_state_digest"), "trained_state_digest"),
                source_release_sha=_nonempty(snapshot_raw.get("source_release_sha"), "source_release_sha"),
                captured_at=_nonempty(snapshot_raw.get("captured_at"), "captured_at"),
            )
            if snapshot_raw.get("snapshot_digest") != snapshot.snapshot_digest:
                raise LifecycleError("lifecycle training snapshot digest mismatch")
            candidate = ModelRelease.create(
                snapshot,
                parameter_digest=_digest(raw.get("parameter_digest"), "parameter_digest"),
                parent_release_id=raw.get("parent_release_id"),
            )
            if candidate.release_id != raw.get("release_id"):
                raise LifecycleError("lifecycle release identity mismatch")
            release = replace(
                candidate,
                status=raw.get("status"),
                validation_digest=raw.get("validation_digest"),
                rejection_reason=raw.get("rejection_reason"),
            )
            releases[release.release_id] = release
        pointers: dict[str, ActiveModelPointer] = {}
        for raw in pointer_rows:
            if not isinstance(raw, Mapping):
                raise LifecycleError("lifecycle pointer is malformed")
            pointer = ActiveModelPointer(
                model_family=_nonempty(raw.get("model_family"), "pointer.model_family"),
                release_id=_digest(raw.get("release_id"), "pointer.release_id"),
                previous_release_id=(
                    _digest(raw.get("previous_release_id"), "pointer.previous_release_id")
                    if raw.get("previous_release_id") is not None
                    else None
                ),
                activated_at=_nonempty(raw.get("activated_at"), "pointer.activated_at"),
                revision=raw.get("revision"),
            )
            if not isinstance(pointer.revision, int) or isinstance(pointer.revision, bool) or pointer.revision < 1:
                raise LifecycleError("pointer revision is invalid")
            if pointer.model_family in pointers:
                raise LifecycleError("duplicate lifecycle pointer")
            pointers[pointer.model_family] = pointer
        receipts: list[RetrainReceipt | ActivationReceipt] = []
        for raw in receipt_rows:
            if not isinstance(raw, Mapping):
                raise LifecycleError("lifecycle receipt is malformed")
            if "outcome" in raw:
                receipts.append(
                    RetrainReceipt(
                        receipt_id=_digest(raw.get("receipt_id"), "receipt_id"),
                        model_family=_nonempty(raw.get("model_family"), "receipt.model_family"),
                        release_id=_digest(raw.get("release_id"), "receipt.release_id"),
                        outcome=_nonempty(raw.get("outcome"), "receipt.outcome"),
                        training_snapshot_digest=_digest(raw.get("training_snapshot_digest"), "training_snapshot_digest"),
                        created_at=_nonempty(raw.get("created_at"), "receipt.created_at"),
                        reason=raw.get("reason"),
                    )
                )
            elif "action" in raw:
                receipts.append(
                    ActivationReceipt(
                        receipt_id=_digest(raw.get("receipt_id"), "receipt_id"),
                        model_family=_nonempty(raw.get("model_family"), "receipt.model_family"),
                        release_id=_digest(raw.get("release_id"), "receipt.release_id"),
                        previous_release_id=(
                            _digest(raw.get("previous_release_id"), "receipt.previous_release_id")
                            if raw.get("previous_release_id") is not None
                            else None
                        ),
                        action=_nonempty(raw.get("action"), "receipt.action"),
                        pointer_digest=_digest(raw.get("pointer_digest"), "pointer_digest"),
                        activated_at=_nonempty(raw.get("activated_at"), "receipt.activated_at"),
                    )
                )
            else:
                raise LifecycleError("unknown lifecycle receipt")
        lifecycle._releases = releases
        lifecycle._pointers = pointers
        lifecycle._receipts = receipts
        lifecycle._validate_active_invariants()
        return lifecycle

    def _validate_active_invariants(
        self,
        *,
        releases: Mapping[str, ModelRelease] | None = None,
        pointers: Mapping[str, ActiveModelPointer] | None = None,
    ) -> None:
        """Reject any serialized state whose ACTIVE set disagrees with pointers."""

        release_map = self._releases if releases is None else releases
        pointer_map = self._pointers if pointers is None else pointers
        active_by_family: dict[str, list[str]] = {}
        for release_id, release in release_map.items():
            family = release.snapshot.model_family
            if release.status == ACTIVE:
                active_by_family.setdefault(family, []).append(release_id)
        for family, pointer in pointer_map.items():
            release = release_map.get(pointer.release_id)
            if release is None:
                raise LifecycleError("active pointer references an unknown release")
            if release.snapshot.model_family != family:
                raise LifecycleError("active pointer family does not match release")
            if release.status != ACTIVE:
                raise LifecycleError("active pointer does not reference an ACTIVE release")
            active_ids = active_by_family.get(family, [])
            if active_ids != [pointer.release_id]:
                raise LifecycleError("active release set is not exclusive")
        for family, active_ids in active_by_family.items():
            pointer = pointer_map.get(family)
            if pointer is None or active_ids != [pointer.release_id]:
                raise LifecycleError("ACTIVE release has no matching active pointer")

    @property
    def releases(self) -> dict[str, ModelRelease]:
        return dict(self._releases)

    @property
    def pointers(self) -> dict[str, ActiveModelPointer]:
        return dict(self._pointers)

    def create_or_noop(
        self,
        snapshot: TrainingSnapshot,
        *,
        parameter_digest: str,
        created_at: str,
    ) -> tuple[ModelRelease, RetrainReceipt]:
        """Create an immutable candidate or return the exact existing release."""

        _utc(created_at, "created_at")
        with self._lock:
            current = self._pointers.get(snapshot.model_family)
            # A result-driven repeat must not manufacture a new child release
            # merely because an identical release is now active.  Parentage is
            # recorded for genuinely new immutable material only.
            existing = next(
                (
                    release
                    for release in self._releases.values()
                    if release.snapshot.model_family == snapshot.model_family
                    and release.snapshot.algorithm_version == snapshot.algorithm_version
                    and release.snapshot.algorithm_digest == snapshot.algorithm_digest
                    and release.snapshot.training_data_digest
                    == snapshot.training_data_digest
                    and release.snapshot.trained_state_digest
                    == snapshot.trained_state_digest
                    and release.snapshot.feature_schema_digest
                    == snapshot.feature_schema_digest
                    and release.parameter_digest == parameter_digest
                ),
                None,
            )
            if existing is None:
                release = ModelRelease.create(
                    snapshot,
                    parameter_digest=parameter_digest,
                    parent_release_id=current.release_id if current else None,
                )
                outcome = "CREATED"
                self._releases[release.release_id] = release
            else:
                release = existing
                outcome = "NO_OP"
            self._validate_active_invariants()
            receipt = RetrainReceipt(
                receipt_id=canonical_digest(
                    {
                        "kind": "retrain",
                        "outcome": outcome,
                        "release_id": release.release_id,
                        "created_at": created_at,
                    }
                ),
                model_family=snapshot.model_family,
                release_id=release.release_id,
                outcome=outcome,
                training_snapshot_digest=snapshot.snapshot_digest,
                created_at=created_at,
            )
            self._receipts.append(receipt)
            return release, receipt

    def validate(
        self,
        release_id: str,
        *,
        training_rows: Iterable[Mapping[str, Any]],
        trained_state: Any,
        parameter_payload: Any,
        prediction_smoke: Mapping[str, Any] | None = None,
        validated_at: str | None = None,
    ) -> ModelRelease:
        """Perform only reproducibility/integrity technical validation.

        Accuracy, ROI, forward samples, publication authority, and activation
        policy deliberately do not appear here.
        """

        with self._lock:
            release = self._releases.get(release_id)
            if release is None:
                raise LifecycleError("release is unknown")
            if release.status not in {TRAINING, VALIDATED}:
                raise LifecycleError("only a training release can be validated")
            try:
                checked_rows = validate_causal_training_rows(
                    training_rows, release.snapshot.training_cutoff
                )
                if len(checked_rows) != release.snapshot.training_row_count:
                    raise LifecycleError("training row count does not match snapshot")
                if max(
                    _utc(row["result_safe_available_at"], "result_safe_available_at")
                    for row in checked_rows
                ) != _utc(
                    release.snapshot.result_safe_watermark,
                    "result_safe_watermark",
                ):
                    raise LifecycleError("result-safe watermark does not match training rows")
                if canonical_digest(list(checked_rows)) != release.snapshot.training_data_digest:
                    raise LifecycleError("training data digest does not match snapshot")
                if canonical_digest(trained_state) != release.snapshot.trained_state_digest:
                    raise LifecycleError("trained state digest does not match snapshot")
                if canonical_digest(parameter_payload) != release.parameter_digest:
                    raise LifecycleError("parameter digest does not match release")
                if not _finite(trained_state) or not _finite(parameter_payload):
                    raise LifecycleError("trained state or parameters contain non-finite values")
                _probability_smoke(prediction_smoke)
            except LifecycleError as exc:
                rejected = replace(release, status=REJECTED, rejection_reason=str(exc))
                self._releases[release_id] = rejected
                failure_at = validated_at or release.snapshot.captured_at
                _utc(failure_at, "validated_at")
                self._receipts.append(
                    RetrainReceipt(
                        receipt_id=canonical_digest(
                            {
                                "kind": "retrain-validation",
                                "outcome": REJECTED,
                                "release_id": release_id,
                                "created_at": failure_at,
                                "reason": str(exc),
                            }
                        ),
                        model_family=release.snapshot.model_family,
                        release_id=release_id,
                        outcome=REJECTED,
                        training_snapshot_digest=release.snapshot.snapshot_digest,
                        created_at=failure_at,
                        reason=str(exc),
                    )
                )
                self._validate_active_invariants()
                return rejected
            validation_digest = canonical_digest(
                {
                    "release_id": release_id,
                    "training_data_digest": release.snapshot.training_data_digest,
                    "trained_state_digest": release.snapshot.trained_state_digest,
                    "parameter_digest": release.parameter_digest,
                    "prediction_smoke": prediction_smoke,
                }
            )
            validated = replace(
                release, status=VALIDATED, validation_digest=validation_digest
            )
            self._releases[release_id] = validated
            self._validate_active_invariants()
            return validated

    def activate(self, release_id: str, *, activated_at: str) -> ActivationReceipt:
        """Atomically point one family at a technically validated release."""

        _utc(activated_at, "activated_at")
        with self._lock:
            self._validate_active_invariants()
            release = self._releases.get(release_id)
            if release is None or release.status != VALIDATED:
                raise LifecycleError("only a VALIDATED release may become active")
            current = self._pointers.get(release.snapshot.model_family)
            pointer = ActiveModelPointer(
                model_family=release.snapshot.model_family,
                release_id=release_id,
                previous_release_id=current.release_id if current else None,
                activated_at=activated_at,
                revision=(current.revision + 1) if current else 1,
            )
            next_releases = dict(self._releases)
            if current is not None:
                next_releases[current.release_id] = replace(
                    next_releases[current.release_id], status=VALIDATED
                )
            next_releases[release_id] = replace(release, status=ACTIVE)
            next_pointers = dict(self._pointers)
            next_pointers[release.snapshot.model_family] = pointer
            self._validate_active_invariants(
                releases=next_releases, pointers=next_pointers
            )
            # Commit all three related changes only after the candidate state
            # has passed the invariant checker while still holding the lock.
            self._releases = next_releases
            self._pointers = next_pointers
            receipt = ActivationReceipt(
                receipt_id=canonical_digest(
                    {
                        "kind": "activation",
                        "release_id": release_id,
                        "pointer_digest": pointer.pointer_digest,
                        "activated_at": activated_at,
                    }
                ),
                model_family=release.snapshot.model_family,
                release_id=release_id,
                previous_release_id=pointer.previous_release_id,
                action="ACTIVATE",
                pointer_digest=pointer.pointer_digest,
                activated_at=activated_at,
            )
            self._receipts.append(receipt)
            self._validate_active_invariants()
            return receipt

    def rollback(self, model_family: str, *, activated_at: str) -> ActivationReceipt:
        """Atomically restore the pointer's immediately prior validated release."""

        _utc(activated_at, "activated_at")
        with self._lock:
            self._validate_active_invariants()
            current = self._pointers.get(model_family)
            if current is None or current.previous_release_id is None:
                raise LifecycleError("no previous release is available for rollback")
            target = self._releases.get(current.previous_release_id)
            if target is None or target.status not in {VALIDATED, ACTIVE}:
                raise LifecycleError("previous release is not rollback-eligible")
            pointer = ActiveModelPointer(
                model_family=model_family,
                release_id=target.release_id,
                previous_release_id=current.release_id,
                activated_at=activated_at,
                revision=current.revision + 1,
            )
            next_releases = dict(self._releases)
            next_releases[current.release_id] = replace(
                next_releases[current.release_id], status=VALIDATED
            )
            next_releases[target.release_id] = replace(target, status=ACTIVE)
            next_pointers = dict(self._pointers)
            next_pointers[model_family] = pointer
            self._validate_active_invariants(
                releases=next_releases, pointers=next_pointers
            )
            self._releases = next_releases
            self._pointers = next_pointers
            receipt = ActivationReceipt(
                receipt_id=canonical_digest(
                    {
                        "kind": "rollback",
                        "release_id": target.release_id,
                        "pointer_digest": pointer.pointer_digest,
                        "activated_at": activated_at,
                    }
                ),
                model_family=model_family,
                release_id=target.release_id,
                previous_release_id=current.release_id,
                action="ROLLBACK",
                pointer_digest=pointer.pointer_digest,
                activated_at=activated_at,
            )
            self._receipts.append(receipt)
            self._validate_active_invariants()
            return receipt

    def health(self, model_family: str, *, updated_at: str) -> ModelHealth:
        with self._lock:
            _utc(updated_at, "updated_at")
            self._validate_active_invariants()
            pointer = self._pointers.get(model_family)
            active = self._releases.get(pointer.release_id) if pointer else None
            family_receipts = [
                item for item in self._receipts if item.model_family == model_family
            ]
            retrain_receipts = [
                item for item in family_receipts if isinstance(item, RetrainReceipt)
            ]
            latest_retrain = retrain_receipts[-1] if retrain_receipts else None
            successful = [
                item
                for item in retrain_receipts
                if item.outcome in {"CREATED", "NO_OP"}
            ]
            last_successful = successful[-1] if successful else None
            latest_rejection = next(
                (
                    index
                    for index in range(len(family_receipts) - 1, -1, -1)
                    if isinstance(family_receipts[index], RetrainReceipt)
                    and family_receipts[index].outcome == REJECTED
                ),
                None,
            )
            latest_activation = next(
                (
                    index
                    for index in range(len(family_receipts) - 1, -1, -1)
                    if isinstance(family_receipts[index], ActivationReceipt)
                    and family_receipts[index].action == "ACTIVATE"
                ),
                None,
            )
            failure_active = latest_rejection is not None and (
                latest_activation is None or latest_rejection > latest_activation
            )
            return ModelHealth(
                model_family=model_family,
                status=(
                    "ACTIVE_WITH_RETRAIN_FAILURE"
                    if pointer and failure_active
                    else "ACTIVE"
                    if pointer
                    else "NO_ACTIVE_RELEASE"
                ),
                active_release_id=pointer.release_id if pointer else None,
                active_training_cutoff=(
                    active.snapshot.training_cutoff if active else None
                ),
                last_result_watermark=(
                    active.snapshot.result_safe_watermark if active else None
                ),
                training_row_count=(
                    active.snapshot.training_row_count if active else None
                ),
                last_successful_retrain=(
                    last_successful.created_at if last_successful else None
                ),
                last_retrain_outcome=(
                    latest_retrain.outcome if latest_retrain else None
                ),
                last_failure_reason=(
                    family_receipts[latest_rejection].reason
                    if failure_active and latest_rejection is not None
                    else None
                ),
                updated_at=updated_at,
            )

    def to_payload(self) -> dict[str, Any]:
        with self._lock:
            self._validate_active_invariants()
            return {
                "schema": "continuous-model-lifecycle-state-v1",
                "releases": [
                    self._releases[key].to_payload()
                    for key in sorted(self._releases)
                ],
                "active_pointers": [
                    self._pointers[key].to_payload()
                    for key in sorted(self._pointers)
                ],
                "receipts": [item.to_payload() for item in self._receipts],
            }

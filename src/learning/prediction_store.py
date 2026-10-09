"""Explicit append-only storage for immutable prediction evidence."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from src.learning.outcome_contracts import (
    LifecycleError,
    PredictionSnapshotV1,
)


class PredictionEvidenceStore(Protocol):
    def append_prediction(self, prediction: PredictionSnapshotV1) -> bool: ...

    def load_predictions(self) -> tuple[PredictionSnapshotV1, ...]: ...


class InMemoryPredictionStore:
    """Reference store used by tests and explicit offline rehearsals."""

    def __init__(self) -> None:
        self.predictions: dict[str, PredictionSnapshotV1] = {}

    def append_prediction(self, prediction: PredictionSnapshotV1) -> bool:
        existing = self.predictions.get(prediction.prediction_id)
        if existing is not None:
            if existing.to_payload() != prediction.to_payload():
                raise LifecycleError("conflicting prediction replay")
            return False
        if any(
            existing.source_record_id == prediction.source_record_id
            and prediction.source_record_id is not None
            and existing.to_payload() != prediction.to_payload()
            for existing in self.predictions.values()
        ):
            raise LifecycleError("conflicting prediction source identity")
        self.predictions[prediction.prediction_id] = prediction
        return True

    def load_predictions(self) -> tuple[PredictionSnapshotV1, ...]:
        return tuple(self.predictions.values())


class JsonlPredictionEvidenceStore:
    """Durable JSONL prediction evidence store at an explicit caller path."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._thread_lock = threading.RLock()

    def _validated_locked(self) -> dict[str, PredictionSnapshotV1]:
        if not self.path.exists():
            return {}
        predictions: dict[str, PredictionSnapshotV1] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LifecycleError("prediction evidence store cannot be read") from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LifecycleError(
                    "prediction evidence store contains invalid JSON"
                ) from exc
            if not isinstance(raw, dict):
                raise LifecycleError("prediction evidence record is not an object")
            prediction = PredictionSnapshotV1.from_payload(raw)
            prior = predictions.get(prediction.prediction_id)
            if prior is not None and prior.to_payload() != prediction.to_payload():
                raise LifecycleError(
                    "prediction evidence contains a conflicting replay"
                )
            if any(
                item.source_record_id == prediction.source_record_id
                and prediction.source_record_id is not None
                and item.to_payload() != prediction.to_payload()
                for item in predictions.values()
            ):
                raise LifecycleError(
                    "prediction evidence contains conflicting source identity"
                )
            predictions[prediction.prediction_id] = prediction
        return predictions

    def append_prediction(self, prediction: PredictionSnapshotV1) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
                with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    existing = self._validated_locked()
                    prior = existing.get(prediction.prediction_id)
                    payload = prediction.to_payload()
                    if prior is not None:
                        if prior.to_payload() != payload:
                            raise LifecycleError("conflicting prediction replay")
                        return False
                    if any(
                        item.source_record_id == prediction.source_record_id
                        and prediction.source_record_id is not None
                        and item.to_payload() != payload
                        for item in existing.values()
                    ):
                        raise LifecycleError("conflicting prediction source identity")
                    handle.seek(0, os.SEEK_END)
                    handle.write(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                    return True
            except OSError as exc:
                raise LifecycleError("prediction evidence append failed") from exc

    def load_predictions(self) -> tuple[PredictionSnapshotV1, ...]:
        if not self.path.exists():
            return ()
        with self._thread_lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                    return tuple(self._validated_locked().values())
            except OSError as exc:
                raise LifecycleError(
                    "prediction evidence store cannot be loaded"
                ) from exc


def capture_predictions(
    predictions: Iterable[PredictionSnapshotV1],
    *,
    store: PredictionEvidenceStore,
    write: bool = False,
) -> tuple[PredictionSnapshotV1, ...]:
    """Validate a batch and optionally append it to an explicit evidence store."""

    normalized = tuple(sorted(predictions, key=lambda item: item.prediction_id))
    by_id: dict[str, PredictionSnapshotV1] = {}
    by_source: dict[str, PredictionSnapshotV1] = {}
    for prediction in normalized:
        prior = by_id.get(prediction.prediction_id)
        if prior is not None and prior.to_payload() != prediction.to_payload():
            raise LifecycleError("conflicting prediction replay")
        by_id[prediction.prediction_id] = prediction
        if prediction.source_record_id is not None:
            source_prior = by_source.get(prediction.source_record_id)
            if (
                source_prior is not None
                and source_prior.to_payload() != prediction.to_payload()
            ):
                raise LifecycleError("conflicting prediction source identity")
            by_source[prediction.source_record_id] = prediction
    if write:
        existing = tuple(store.load_predictions())
        existing_by_id = {item.prediction_id: item for item in existing}
        for prediction in by_id.values():
            prior = existing_by_id.get(prediction.prediction_id)
            if prior is not None and prior.to_payload() != prediction.to_payload():
                raise LifecycleError("conflicting prediction replay")
            if any(
                item.source_record_id == prediction.source_record_id
                and prediction.source_record_id is not None
                and item.to_payload() != prediction.to_payload()
                for item in existing
            ):
                raise LifecycleError("conflicting prediction source identity")
        for prediction in by_id.values():
            store.append_prediction(prediction)
    return tuple(by_id[key] for key in sorted(by_id))

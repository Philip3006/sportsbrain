"""Universal immutable result, prediction, and causal-training contracts.

The contracts in this module are intentionally transport- and sport-neutral.
Sport adapters provide the authoritative result payload and the market-label
function; this module enforces identity, time, digest, replay, and causality
rules.  It never calls a provider, writes a model, changes an active pointer,
publishes a signal, or touches a betting ledger.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import threading
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

AUTHORITATIVE_RESULT_SCHEMA = "sportsbrain-authoritative-result-v1"
PREDICTION_SCHEMA = "sportsbrain-prediction-snapshot-v1"
ATTACHMENT_SCHEMA = "sportsbrain-outcome-attachment-v1"
CAUSAL_TRAINING_ROW_SCHEMA = "sportsbrain-causal-training-row-v1"
_DIGEST_LENGTH = 64


class LifecycleError(ValueError):
    """Raised when immutable evidence or a lifecycle transition is unsafe."""


class SettlementState(StrEnum):
    WON = "won"
    LOST = "lost"
    PUSH = "push"
    VOID = "void"
    CANCELLED = "cancelled"


class RetrainDecision(StrEnum):
    NO_OP = "NO_OP"
    RETRAIN_REQUIRED = "RETRAIN_REQUIRED"


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LifecycleError("value is not canonical JSON") from exc


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _immutable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_immutable(item) for item in value)
    return value


def _public(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _DIGEST_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise LifecycleError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(f"{field} is required")
    return value.strip()


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise LifecycleError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


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


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifecycleError(f"{field} must be an object")
    result = dict(value)
    if not _finite(result):
        raise LifecycleError(f"{field} contains non-finite values")
    return result


@dataclass(frozen=True)
class AuthoritativeResultV1:
    """One result-safe authoritative result, independent of betting."""

    result_id: str
    fixture_id: str
    sport: str
    competition: str
    source: str
    source_record_id: str
    completed_at: str
    result_safe_available_at: str
    actual_result: dict[str, Any]
    result_digest: str
    provenance_digest: str

    def __post_init__(self) -> None:
        for field in (
            "fixture_id",
            "sport",
            "competition",
            "source",
            "source_record_id",
        ):
            _text(getattr(self, field), field)
        _digest(self.result_id, "result_id")
        _digest(self.result_digest, "result_digest")
        _digest(self.provenance_digest, "provenance_digest")
        completed = _utc(self.completed_at, "completed_at")
        safe = _utc(self.result_safe_available_at, "result_safe_available_at")
        if safe <= completed:
            raise LifecycleError("result_safe_available_at must follow completed_at")
        object.__setattr__(
            self,
            "actual_result",
            _immutable(_mapping(self.actual_result, "actual_result")),
        )
        if canonical_digest(self.actual_result) != self.result_digest:
            raise LifecycleError("result_digest does not match actual_result")
        expected_id = canonical_digest(self.identity_payload())
        if self.result_id != expected_id:
            raise LifecycleError("result_id does not match result identity")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": AUTHORITATIVE_RESULT_SCHEMA,
            "fixture_id": self.fixture_id,
            "sport": self.sport,
            "competition": self.competition,
            "source": self.source,
            "source_record_id": self.source_record_id,
            "completed_at": self.completed_at,
            "result_safe_available_at": self.result_safe_available_at,
            "result_digest": self.result_digest,
            "provenance_digest": self.provenance_digest,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "result_id": self.result_id,
            "actual_result": _public(self.actual_result),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AuthoritativeResultV1:
        if payload.get("schema") != AUTHORITATIVE_RESULT_SCHEMA:
            raise LifecycleError("unsupported authoritative result schema")
        return cls(
            result_id=payload.get("result_id", ""),
            fixture_id=payload.get("fixture_id", ""),
            sport=payload.get("sport", ""),
            competition=payload.get("competition", ""),
            source=payload.get("source", ""),
            source_record_id=payload.get("source_record_id", ""),
            completed_at=payload.get("completed_at", ""),
            result_safe_available_at=payload.get("result_safe_available_at", ""),
            actual_result=payload.get("actual_result", {}),
            result_digest=payload.get("result_digest", ""),
            provenance_digest=payload.get("provenance_digest", ""),
        )


@dataclass(frozen=True)
class PredictionSnapshotV1:
    """Immutable pre-result prediction evidence used by settlement."""

    prediction_id: str
    signal_id: str
    fixture_id: str
    sport: str
    competition: str
    model_family: str
    model_release_id: str
    lifecycle_version: str
    phase: str
    prediction_timestamp: str
    feature_cutoff: str
    prediction: dict[str, Any]
    prediction_digest: str
    source_record_id: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "signal_id",
            "fixture_id",
            "sport",
            "competition",
            "model_family",
            "model_release_id",
            "lifecycle_version",
            "phase",
        ):
            _text(getattr(self, field), field)
        _digest(self.prediction_id, "prediction_id")
        if self.source_record_id is not None:
            _text(self.source_record_id, "source_record_id")
        _utc(self.prediction_timestamp, "prediction_timestamp")
        _utc(self.feature_cutoff, "feature_cutoff")
        if _utc(self.feature_cutoff, "feature_cutoff") > _utc(
            self.prediction_timestamp, "prediction_timestamp"
        ):
            raise LifecycleError("feature_cutoff cannot follow prediction_timestamp")
        object.__setattr__(
            self, "prediction", _immutable(_mapping(self.prediction, "prediction"))
        )
        if canonical_digest(self.prediction_payload()) != self.prediction_digest:
            raise LifecycleError("prediction_digest does not match prediction")
        if self.prediction_id != canonical_digest(self.identity_payload()):
            raise LifecycleError("prediction_id does not match prediction identity")

    def prediction_payload(self) -> dict[str, Any]:
        return {"prediction": _public(self.prediction)}

    def identity_payload(self) -> dict[str, Any]:
        identity = {
            "schema": PREDICTION_SCHEMA,
            "signal_id": self.signal_id,
            "fixture_id": self.fixture_id,
            "sport": self.sport,
            "competition": self.competition,
            "model_family": self.model_family,
            "model_release_id": self.model_release_id,
            "lifecycle_version": self.lifecycle_version,
            "phase": self.phase,
            "prediction_timestamp": self.prediction_timestamp,
            "feature_cutoff": self.feature_cutoff,
            "prediction_digest": self.prediction_digest,
        }
        if self.source_record_id is not None:
            identity["source_record_id"] = self.source_record_id
        return identity

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "prediction_id": self.prediction_id,
            **self.prediction_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> PredictionSnapshotV1:
        if payload.get("schema") != PREDICTION_SCHEMA:
            raise LifecycleError("unsupported prediction schema")
        return cls(
            prediction_id=payload.get("prediction_id", ""),
            signal_id=payload.get("signal_id", ""),
            fixture_id=payload.get("fixture_id", ""),
            sport=payload.get("sport", ""),
            competition=payload.get("competition", ""),
            model_family=payload.get("model_family", ""),
            model_release_id=payload.get("model_release_id", ""),
            lifecycle_version=payload.get("lifecycle_version", ""),
            phase=payload.get("phase", ""),
            prediction_timestamp=payload.get("prediction_timestamp", ""),
            feature_cutoff=payload.get("feature_cutoff", ""),
            prediction=payload.get("prediction", {}),
            prediction_digest=payload.get("prediction_digest", ""),
            source_record_id=payload.get("source_record_id"),
        )


def _market_outcome(
    actual_result: Mapping[str, Any], prediction: Mapping[str, Any]
) -> str:
    """Return an adapter-independent outcome for common 1X2 predictions."""

    market = prediction.get("market")
    predicted = prediction.get("selection")
    home_score = actual_result.get("home_score")
    away_score = actual_result.get("away_score")
    if market != "1X2" or predicted not in {"HOME", "DRAW", "AWAY"}:
        raise LifecycleError("no universal market outcome resolver for prediction")
    if not isinstance(home_score, int) or not isinstance(away_score, int):
        raise LifecycleError("1X2 actual_result requires integer scores")
    actual = (
        "HOME"
        if home_score > away_score
        else "AWAY"
        if away_score > home_score
        else "DRAW"
    )
    return SettlementState.WON if actual == predicted else SettlementState.LOST


@dataclass(frozen=True)
class OutcomeAttachmentV1:
    """Immutable linkage between one prediction and one authoritative result."""

    attachment_id: str
    prediction_id: str
    signal_id: str
    fixture_id: str
    sport: str
    competition: str
    model_family: str
    model_release_id: str
    lifecycle_version: str
    phase: str
    prediction_timestamp: str
    result_id: str
    result_safe_available_at: str
    authoritative_result_source: str
    actual_result: dict[str, Any]
    market: str
    predicted_selection: str
    settlement_state: str
    settled_at: str
    prediction_digest: str
    result_digest: str
    provenance_digest: str

    def __post_init__(self) -> None:
        for field in (
            "prediction_id",
            "signal_id",
            "fixture_id",
            "sport",
            "competition",
            "model_family",
            "model_release_id",
            "lifecycle_version",
            "phase",
            "authoritative_result_source",
            "market",
            "predicted_selection",
            "settlement_state",
        ):
            _text(getattr(self, field), field)
        for field in (
            "attachment_id",
            "prediction_id",
            "result_id",
            "prediction_digest",
            "result_digest",
            "provenance_digest",
        ):
            _digest(getattr(self, field), field)
        if self.settlement_state not in {state.value for state in SettlementState}:
            raise LifecycleError("unsupported settlement_state")
        _utc(self.prediction_timestamp, "prediction_timestamp")
        _utc(self.result_safe_available_at, "result_safe_available_at")
        _utc(self.settled_at, "settled_at")
        if _utc(self.settled_at, "settled_at") < _utc(
            self.result_safe_available_at, "result_safe_available_at"
        ):
            raise LifecycleError("settled_at cannot precede result_safe_available_at")
        object.__setattr__(
            self,
            "actual_result",
            _immutable(_mapping(self.actual_result, "actual_result")),
        )
        if canonical_digest(self.actual_result) != self.result_digest:
            raise LifecycleError("attachment result_digest mismatch")
        if self.attachment_id != canonical_digest(self.identity_payload()):
            raise LifecycleError("attachment_id does not match attachment identity")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": ATTACHMENT_SCHEMA,
            "prediction_id": self.prediction_id,
            "signal_id": self.signal_id,
            "fixture_id": self.fixture_id,
            "sport": self.sport,
            "competition": self.competition,
            "model_family": self.model_family,
            "model_release_id": self.model_release_id,
            "lifecycle_version": self.lifecycle_version,
            "phase": self.phase,
            "prediction_timestamp": self.prediction_timestamp,
            "result_id": self.result_id,
            "result_safe_available_at": self.result_safe_available_at,
            "authoritative_result_source": self.authoritative_result_source,
            "market": self.market,
            "predicted_selection": self.predicted_selection,
            "settlement_state": self.settlement_state,
            "settled_at": self.settled_at,
            "prediction_digest": self.prediction_digest,
            "result_digest": self.result_digest,
            "provenance_digest": self.provenance_digest,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "attachment_id": self.attachment_id,
            "actual_result": _public(self.actual_result),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> OutcomeAttachmentV1:
        if payload.get("schema") != ATTACHMENT_SCHEMA:
            raise LifecycleError("unsupported outcome attachment schema")
        return cls(
            attachment_id=payload.get("attachment_id", ""),
            prediction_id=payload.get("prediction_id", ""),
            signal_id=payload.get("signal_id", ""),
            fixture_id=payload.get("fixture_id", ""),
            sport=payload.get("sport", ""),
            competition=payload.get("competition", ""),
            model_family=payload.get("model_family", ""),
            model_release_id=payload.get("model_release_id", ""),
            lifecycle_version=payload.get("lifecycle_version", ""),
            phase=payload.get("phase", ""),
            prediction_timestamp=payload.get("prediction_timestamp", ""),
            result_id=payload.get("result_id", ""),
            result_safe_available_at=payload.get("result_safe_available_at", ""),
            authoritative_result_source=payload.get("authoritative_result_source", ""),
            actual_result=payload.get("actual_result", {}),
            market=payload.get("market", ""),
            predicted_selection=payload.get("predicted_selection", ""),
            settlement_state=payload.get("settlement_state", ""),
            settled_at=payload.get("settled_at", ""),
            prediction_digest=payload.get("prediction_digest", ""),
            result_digest=payload.get("result_digest", ""),
            provenance_digest=payload.get("provenance_digest", ""),
        )


class ResultAdapter(Protocol):
    """Provider/sport boundary; implementations must return validated results."""

    sport: str

    def normalize(self, raw: Mapping[str, Any]) -> AuthoritativeResultV1:
        """Convert one provider record without contacting a provider here."""


class AppendOnlyOutcomeStore(Protocol):
    """Minimal persistence boundary for adapters and orchestrators."""

    def append_result(self, result: AuthoritativeResultV1) -> bool: ...

    def append_attachment(self, attachment: OutcomeAttachmentV1) -> bool: ...


class InMemoryOutcomeStore:
    """Deterministic test/reference store implementing append-only semantics."""

    def __init__(self) -> None:
        self.results: dict[str, AuthoritativeResultV1] = {}
        self.attachments: dict[str, OutcomeAttachmentV1] = {}

    def append_result(self, result: AuthoritativeResultV1) -> bool:
        existing = self.results.get(result.result_id)
        if existing is not None:
            if existing.to_payload() != result.to_payload():
                raise LifecycleError("conflicting authoritative result replay")
            return False
        if any(
            row.source == result.source
            and row.source_record_id == result.source_record_id
            and row.to_payload() != result.to_payload()
            for row in self.results.values()
        ):
            raise LifecycleError("conflicting source result identity")
        self.results[result.result_id] = result
        return True

    def append_attachment(self, attachment: OutcomeAttachmentV1) -> bool:
        if attachment.result_id not in self.results:
            raise LifecycleError("outcome attachment references an unknown result")
        existing = self.attachments.get(attachment.attachment_id)
        if existing is not None:
            if existing.to_payload() != attachment.to_payload():
                raise LifecycleError("conflicting outcome attachment replay")
            return False
        if any(
            row.prediction_id == attachment.prediction_id
            and row.to_payload() != attachment.to_payload()
            for row in self.attachments.values()
        ):
            raise LifecycleError("prediction already has a conflicting attachment")
        self.attachments[attachment.attachment_id] = attachment
        return True


class JsonlOutcomeStore:
    """Durable append-only store for one explicitly supplied state path.

    The path is caller-owned and there is deliberately no repository or
    production default.  Each append revalidates the complete existing file
    under an OS file lock, then appends one canonical record and fsyncs it.
    Identical replays are no-ops; identity conflicts are rejected before any
    write.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._thread_lock = threading.RLock()

    def _records_locked(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        records: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LifecycleError("outcome store cannot be read") from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LifecycleError("outcome store contains invalid JSON") from exc
            if not isinstance(raw, Mapping):
                raise LifecycleError("outcome store record is not an object")
            records.append(dict(raw))
        return tuple(records)

    def _validated_locked(
        self,
    ) -> tuple[
        dict[str, Any], dict[str, AuthoritativeResultV1], dict[str, OutcomeAttachmentV1]
    ]:
        raw_records = self._records_locked()
        raw_by_key: dict[str, dict[str, Any]] = {}
        results: dict[str, AuthoritativeResultV1] = {}
        attachments: dict[str, OutcomeAttachmentV1] = {}
        for raw in raw_records:
            schema = raw.get("schema")
            if schema == AUTHORITATIVE_RESULT_SCHEMA:
                item = AuthoritativeResultV1.from_payload(raw)
                key = item.result_id
                target = results
            elif schema == ATTACHMENT_SCHEMA:
                item = OutcomeAttachmentV1.from_payload(raw)
                key = item.attachment_id
                target = attachments
            else:
                raise LifecycleError("outcome store contains an unknown schema")
            previous = raw_by_key.get(key)
            canonical = json.loads(_canonical_json(raw))
            if previous is not None and previous != canonical:
                raise LifecycleError("outcome store contains a conflicting replay")
            raw_by_key[key] = canonical
            if key in target and target[key].to_payload() != canonical:
                raise LifecycleError("outcome store contains a conflicting replay")
            target[key] = item
        for attachment in attachments.values():
            if attachment.result_id not in results:
                raise LifecycleError(
                    "outcome store attachment references unknown result"
                )
        return raw_by_key, results, attachments

    def _append(self, item: AuthoritativeResultV1 | OutcomeAttachmentV1) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
                with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    raw_by_key, results, attachments = self._validated_locked()
                    key = (
                        item.result_id
                        if isinstance(item, AuthoritativeResultV1)
                        else item.attachment_id
                    )
                    payload = item.to_payload()
                    canonical = json.loads(_canonical_json(payload))
                    prior = raw_by_key.get(key)
                    if prior is not None:
                        if prior != canonical:
                            raise LifecycleError("conflicting immutable replay")
                        return False
                    if (
                        isinstance(item, OutcomeAttachmentV1)
                        and item.result_id not in results
                    ):
                        raise LifecycleError(
                            "outcome attachment references unknown result"
                        )
                    if isinstance(item, AuthoritativeResultV1) and any(
                        existing.source == item.source
                        and existing.source_record_id == item.source_record_id
                        and existing.to_payload() != payload
                        for existing in results.values()
                    ):
                        raise LifecycleError("conflicting source result identity")
                    if isinstance(item, OutcomeAttachmentV1) and any(
                        existing.prediction_id == item.prediction_id
                        and existing.to_payload() != payload
                        for existing in attachments.values()
                    ):
                        raise LifecycleError(
                            "prediction already has a conflicting attachment"
                        )
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
                raise LifecycleError("outcome store append failed") from exc

    def append_result(self, result: AuthoritativeResultV1) -> bool:
        return self._append(result)

    def append_attachment(self, attachment: OutcomeAttachmentV1) -> bool:
        return self._append(attachment)

    def load(
        self,
    ) -> tuple[tuple[AuthoritativeResultV1, ...], tuple[OutcomeAttachmentV1, ...]]:
        if not self.path.exists():
            return (), ()
        with self._thread_lock:
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                    _, results, attachments = self._validated_locked()
                    return tuple(results.values()), tuple(attachments.values())
            except OSError as exc:
                raise LifecycleError("outcome store cannot be loaded") from exc


def build_outcome_attachment(
    prediction: PredictionSnapshotV1,
    result: AuthoritativeResultV1,
    *,
    settled_at: str,
    provenance_digest: str,
    outcome_resolver: Callable[[Mapping[str, Any], Mapping[str, Any]], str]
    | None = None,
) -> OutcomeAttachmentV1:
    """Create the only supported prediction→result attachment."""

    for field in ("fixture_id", "sport", "competition"):
        if getattr(prediction, field) != getattr(result, field):
            raise LifecycleError(f"{field} binding mismatch")
    if _utc(result.result_safe_available_at, "result_safe_available_at") <= _utc(
        prediction.prediction_timestamp, "prediction_timestamp"
    ):
        raise LifecycleError("result must become safe after prediction")
    state = (
        outcome_resolver(result.actual_result, prediction.prediction)
        if outcome_resolver is not None
        else _market_outcome(result.actual_result, prediction.prediction)
    )
    if state not in {item.value for item in SettlementState}:
        raise LifecycleError("outcome resolver returned an unsupported state")
    base = {
        "schema": ATTACHMENT_SCHEMA,
        "prediction_id": prediction.prediction_id,
        "signal_id": prediction.signal_id,
        "fixture_id": prediction.fixture_id,
        "sport": prediction.sport,
        "competition": prediction.competition,
        "model_family": prediction.model_family,
        "model_release_id": prediction.model_release_id,
        "lifecycle_version": prediction.lifecycle_version,
        "phase": prediction.phase,
        "prediction_timestamp": prediction.prediction_timestamp,
        "result_id": result.result_id,
        "result_safe_available_at": result.result_safe_available_at,
        "authoritative_result_source": result.source,
        "market": prediction.prediction.get("market", ""),
        "predicted_selection": prediction.prediction.get("selection", ""),
        "settlement_state": state,
        "settled_at": settled_at,
        "prediction_digest": prediction.prediction_digest,
        "result_digest": result.result_digest,
        "provenance_digest": provenance_digest,
    }
    return OutcomeAttachmentV1(
        attachment_id=canonical_digest(base),
        actual_result=result.actual_result,
        **{key: value for key, value in base.items() if key != "schema"},
    )


@dataclass(frozen=True)
class CausalTrainingRowV1:
    """One result-labelled row with explicit feature/result time boundaries."""

    row_id: str
    fixture_id: str
    sport: str
    competition: str
    training_cutoff: str
    feature_available_at: str
    event_completed_at: str
    result_safe_available_at: str
    features: dict[str, Any]
    label: dict[str, Any]
    result_id: str
    feature_digest: str
    row_digest: str

    def __post_init__(self) -> None:
        for field in ("fixture_id", "sport", "competition"):
            _text(getattr(self, field), field)
        _digest(self.row_id, "row_id")
        _digest(self.result_id, "result_id")
        _digest(self.feature_digest, "feature_digest")
        _digest(self.row_digest, "row_digest")
        cutoff = _utc(self.training_cutoff, "training_cutoff")
        feature = _utc(self.feature_available_at, "feature_available_at")
        completed = _utc(self.event_completed_at, "event_completed_at")
        safe = _utc(self.result_safe_available_at, "result_safe_available_at")
        if feature > cutoff:
            raise LifecycleError("feature_available_at follows training_cutoff")
        if safe <= completed:
            raise LifecycleError(
                "result_safe_available_at must follow event completion"
            )
        if safe > cutoff:
            raise LifecycleError("result is not safe at training_cutoff")
        object.__setattr__(
            self, "features", _immutable(_mapping(self.features, "features"))
        )
        object.__setattr__(self, "label", _immutable(_mapping(self.label, "label")))
        if canonical_digest(self.features) != self.feature_digest:
            raise LifecycleError("feature_digest mismatch")
        if self.row_id != canonical_digest(self.identity_payload()):
            raise LifecycleError("row_id mismatch")
        if self.row_digest != canonical_digest(self.to_payload_without_row_digest()):
            raise LifecycleError("row_digest mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": CAUSAL_TRAINING_ROW_SCHEMA,
            "fixture_id": self.fixture_id,
            "sport": self.sport,
            "competition": self.competition,
            "training_cutoff": self.training_cutoff,
            "feature_available_at": self.feature_available_at,
            "event_completed_at": self.event_completed_at,
            "result_safe_available_at": self.result_safe_available_at,
            "feature_digest": self.feature_digest,
            "result_id": self.result_id,
        }

    def to_payload_without_row_digest(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "features": _public(self.features),
            "label": _public(self.label),
            "row_id": self.row_id,
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self.to_payload_without_row_digest(), "row_digest": self.row_digest}


def append_training_rows(
    existing: Iterable[CausalTrainingRowV1],
    incoming: Iterable[CausalTrainingRowV1],
) -> tuple[CausalTrainingRowV1, ...]:
    """Deterministically extend a causal dataset, rejecting conflicting rows."""

    by_id: MutableMapping[str, CausalTrainingRowV1] = {
        row.row_id: row for row in existing
    }
    for row in incoming:
        prior = by_id.get(row.row_id)
        if prior is not None and prior.to_payload() != row.to_payload():
            raise LifecycleError("conflicting causal training row")
        by_id[row.row_id] = row
    return tuple(by_id[key] for key in sorted(by_id))


def decide_retrain(
    existing_rows: Iterable[CausalTrainingRowV1],
    candidate_rows: Iterable[CausalTrainingRowV1],
) -> tuple[RetrainDecision, tuple[CausalTrainingRowV1, ...], str]:
    """Return a digest-driven NO_OP or RETRAIN_REQUIRED decision."""

    current = append_training_rows((), existing_rows)
    merged = append_training_rows(current, candidate_rows)
    current_digest = canonical_digest([row.to_payload() for row in current])
    merged_digest = canonical_digest([row.to_payload() for row in merged])
    decision = (
        RetrainDecision.NO_OP
        if merged_digest == current_digest
        else RetrainDecision.RETRAIN_REQUIRED
    )
    return decision, merged, merged_digest

"""Offline Top-5 replay lifecycle; engineering-validation only, never real evidence."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from types import MappingProxyType

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    SignalTimeContract,
    _utc,
)
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_dispatch import signal_time_contract_id
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA, M5_CANDIDATE_ID

OFFLINE_REPLAY_MARKER = "OFFLINE_REPLAY"
ENGINEERING_VALIDATION_MARKER = "ENGINEERING_VALIDATION_ONLY"
OFFLINE_REPLAY_NAMESPACE = "results/offline_replay/top5/"
REAL_SHADOW_NAMESPACE = "results/shadow/top5/"
TOP5_REPLAY_LEAGUES = tuple(sorted(TOP5_LEAGUE_ADAPTERS))
SEALED_PARTITIONS = frozenset({"2425", "2526"})
ALLOWED_DEV_PARTITIONS = frozenset({"2021", "2122", "2223", "2324"})
_OUTCOMES = ("away", "draw", "home")


class OfflineReplayError(ProductionContractError):
    """Raised when replay input or an attachment crosses a hard boundary."""

class ReplayResultStatus(str, Enum):
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"

def validate_replay_partition(partition: str) -> None:
    """Allow only explicitly approved DEV partitions and reject sealed data."""

    if not isinstance(partition, str) or not partition.strip():
        raise OfflineReplayError("offline replay requires a partition")
    normalized = partition.strip()
    if normalized in SEALED_PARTITIONS:
        raise OfflineReplayError(f"sealed partition is forbidden: {normalized}")
    if normalized not in ALLOWED_DEV_PARTITIONS:
        raise OfflineReplayError(f"partition is not an approved DEV partition: {normalized}")


def _stable_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_probabilities(values: Mapping[str, float], field_name: str) -> Mapping[str, float]:
    if set(values) != set(_OUTCOMES):
        raise OfflineReplayError(f"{field_name} requires away, draw, and home")
    try:
        normalized = {name: float(values[name]) for name in _OUTCOMES}
    except (TypeError, ValueError) as exc:
        raise OfflineReplayError(f"{field_name} contains a non-numeric value") from exc
    if any(not isfinite(value) or not 0 <= value <= 1 for value in normalized.values()):
        raise OfflineReplayError(f"{field_name} contains an invalid probability")
    if abs(sum(normalized.values()) - 1.0) > 1e-9:
        raise OfflineReplayError(f"{field_name} must sum to one")
    return MappingProxyType(normalized)


def _market_probabilities(snapshot: MarketSnapshot) -> Mapping[str, float]:
    snapshot.validate()
    inverse = {name: 1.0 / float(snapshot.odds[name]) for name in _OUTCOMES}
    total = sum(inverse.values())
    return MappingProxyType({name: inverse[name] / total for name in _OUTCOMES})


@dataclass(frozen=True)
class HistoricalReplayInput:
    """One historical candidate and its optional legitimate pre-closing input."""

    partition: str
    fixture: Fixture
    source_identity: str
    replayed_at: datetime
    signal_snapshot: MarketSnapshot | None = None

    def validate(self) -> None:
        validate_replay_partition(self.partition)
        self.fixture.validate()
        if self.fixture.league_code not in TOP5_REPLAY_LEAGUES:
            raise OfflineReplayError("replay fixture is outside the five approved leagues")
        replayed_at = _utc(self.replayed_at, "replayed_at")
        if not self.source_identity.startswith("historical:"):
            raise OfflineReplayError("replay source must be explicitly historical")
        if self.signal_snapshot is None:
            return
        self.signal_snapshot.validate()
        if self.signal_snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise OfflineReplayError("replay input must be a pre-closing signal snapshot")
        if self.signal_snapshot.fixture_key != self.fixture.fixture_key:
            raise OfflineReplayError("replay snapshot belongs to another fixture")
        if self.signal_snapshot.captured_at > replayed_at:
            raise OfflineReplayError("replay source timestamp is after replay time")


@dataclass(frozen=True)
class ReplayPredictionArtifact:
    """Immutable prediction payload; later records reference its digest only."""

    prediction_id: str
    fixture_key: str
    league_code: str
    home_team: str
    away_team: str
    kickoff: datetime
    partition: str
    source_identity: str
    source_timestamp: datetime
    replayed_at: datetime
    snapshot_id: str
    signal_time_contract_id: str
    signal_time_maximum_age_seconds: int
    model_identity: str
    research_sha: str
    source_sha: str
    probabilities: Mapping[str, float]
    market_probabilities: Mapping[str, float]
    artifact_sha: str
    marker: str = OFFLINE_REPLAY_MARKER
    engineering_status: str = ENGINEERING_VALIDATION_MARKER
    no_bet: bool = True
    publication: bool = False

    @classmethod
    def create(
        cls,
        prediction_id: str,
        input_data: HistoricalReplayInput,
        signal_time: SignalTimeContract,
        probabilities: Mapping[str, float],
        source_sha: str,
        snapshot_id: str,
    ) -> ReplayPredictionArtifact:
        input_data.validate()
        if input_data.signal_snapshot is None:
            raise OfflineReplayError("prediction requires a legitimate signal snapshot")
        signal_time.validate()
        normalized_probabilities = _required_probabilities(probabilities, "prediction probabilities")
        payload = {
            "prediction_id": prediction_id,
            "fixture_key": input_data.fixture.fixture_key,
            "league_code": input_data.fixture.league_code,
            "home_team": input_data.fixture.home_team,
            "away_team": input_data.fixture.away_team,
            "kickoff": _utc(input_data.fixture.kickoff, "kickoff").isoformat(),
            "partition": input_data.partition,
            "source_identity": input_data.source_identity,
            "source_timestamp": input_data.signal_snapshot.captured_at.isoformat(),
            "replayed_at": _utc(input_data.replayed_at, "replayed_at").isoformat(),
            "snapshot_id": snapshot_id,
            "signal_time_contract_id": signal_time_contract_id(signal_time),
            "signal_time_maximum_age_seconds": signal_time.maximum_odds_age_seconds,
            "model_identity": M5_CANDIDATE_ID,
            "research_sha": FROZEN_RESEARCH_SHA,
            "source_sha": source_sha,
            "probabilities": dict(normalized_probabilities),
            "market_probabilities": dict(_market_probabilities(input_data.signal_snapshot)),
            "marker": OFFLINE_REPLAY_MARKER,
            "engineering_status": ENGINEERING_VALIDATION_MARKER,
            "no_bet": True,
            "publication": False,
        }
        return cls(
            prediction_id=prediction_id,
            fixture_key=input_data.fixture.fixture_key,
            league_code=input_data.fixture.league_code,
            home_team=input_data.fixture.home_team,
            away_team=input_data.fixture.away_team,
            kickoff=input_data.fixture.kickoff,
            partition=input_data.partition,
            source_identity=input_data.source_identity,
            source_timestamp=input_data.signal_snapshot.captured_at,
            replayed_at=input_data.replayed_at,
            snapshot_id=snapshot_id,
            signal_time_contract_id=signal_time_contract_id(signal_time),
            signal_time_maximum_age_seconds=signal_time.maximum_odds_age_seconds,
            model_identity=M5_CANDIDATE_ID,
            research_sha=FROZEN_RESEARCH_SHA,
            source_sha=source_sha,
            probabilities=normalized_probabilities,
            market_probabilities=_market_probabilities(input_data.signal_snapshot),
            artifact_sha=_stable_digest(payload),
        )

    def _payload(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id,
            "fixture_key": self.fixture_key,
            "league_code": self.league_code,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": _utc(self.kickoff, "kickoff").isoformat(),
            "partition": self.partition,
            "source_identity": self.source_identity,
            "source_timestamp": _utc(self.source_timestamp, "source_timestamp").isoformat(),
            "replayed_at": _utc(self.replayed_at, "replayed_at").isoformat(),
            "snapshot_id": self.snapshot_id,
            "signal_time_contract_id": self.signal_time_contract_id,
            "signal_time_maximum_age_seconds": self.signal_time_maximum_age_seconds,
            "model_identity": self.model_identity,
            "research_sha": self.research_sha,
            "source_sha": self.source_sha,
            "probabilities": dict(self.probabilities),
            "market_probabilities": dict(self.market_probabilities),
            "marker": self.marker,
            "engineering_status": self.engineering_status,
            "no_bet": self.no_bet,
            "publication": self.publication,
        }

    def validate(self) -> None:
        validate_replay_partition(self.partition)
        if self.league_code not in TOP5_REPLAY_LEAGUES:
            raise OfflineReplayError("prediction league is outside the five approved leagues")
        if self.model_identity != M5_CANDIDATE_ID or self.research_sha != FROZEN_RESEARCH_SHA:
            raise OfflineReplayError("replay prediction is not bound to frozen M5 research")
        if len(self.source_sha) != 40 or any(
            char not in "0123456789abcdef" for char in self.source_sha.lower()
        ):
            raise OfflineReplayError("prediction source SHA is invalid")
        if not self.source_identity.startswith("historical:"):
            raise OfflineReplayError("prediction source is not historical")
        if self.marker != OFFLINE_REPLAY_MARKER or self.engineering_status != ENGINEERING_VALIDATION_MARKER:
            raise OfflineReplayError("prediction is missing offline replay markers")
        if not self.no_bet or self.publication:
            raise OfflineReplayError("offline replay prediction violates no-bet/publication safety")
        if self.signal_time_maximum_age_seconds <= 0:
            raise OfflineReplayError("replay signal-time age bound must be positive")
        if self.source_timestamp > self.replayed_at:
            raise OfflineReplayError("prediction source timestamp is after replay time")
        _required_probabilities(self.probabilities, "prediction probabilities")
        _required_probabilities(self.market_probabilities, "market probabilities")
        if _stable_digest(self._payload()) != self.artifact_sha:
            raise OfflineReplayError("prediction artifact digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload(), "artifact_sha": self.artifact_sha}


def _result_outcome(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "home"
    if home_score < away_score:
        return "away"
    return "draw"


@dataclass(frozen=True)
class ReplayResultAttachment:
    prediction_id: str
    prediction_artifact_sha: str
    fixture_key: str
    league_code: str
    result_source: str
    result_timestamp: datetime
    attached_at: datetime
    status: ReplayResultStatus
    home_score: int | None
    away_score: int | None
    actual_outcome: str | None
    attachment_sha: str = ""
    marker: str = OFFLINE_REPLAY_MARKER

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_timestamp", _utc(self.result_timestamp, "result_timestamp"))
        object.__setattr__(self, "attached_at", _utc(self.attached_at, "attached_at"))
        if not self.attachment_sha:
            object.__setattr__(self, "attachment_sha", _stable_digest(self._payload()))

    @classmethod
    def from_scores(
        cls,
        prediction: ReplayPredictionArtifact,
        *,
        result_source: str,
        result_timestamp: datetime,
        attached_at: datetime,
        home_score: int,
        away_score: int,
        status: ReplayResultStatus = ReplayResultStatus.FINAL,
    ) -> ReplayResultAttachment:
        outcome = (
            _result_outcome(home_score, away_score)
            if ReplayResultStatus(status) is ReplayResultStatus.FINAL
            else None
        )
        return cls(
            prediction_id=prediction.prediction_id,
            prediction_artifact_sha=prediction.artifact_sha,
            fixture_key=prediction.fixture_key,
            league_code=prediction.league_code,
            result_source=result_source,
            result_timestamp=result_timestamp,
            attached_at=attached_at,
            status=status,
            home_score=home_score,
            away_score=away_score,
            actual_outcome=outcome,
        )

    def _payload(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id,
            "prediction_artifact_sha": self.prediction_artifact_sha,
            "fixture_key": self.fixture_key,
            "league_code": self.league_code,
            "result_source": self.result_source,
            "result_timestamp": self.result_timestamp.isoformat(),
            "attached_at": self.attached_at.isoformat(),
            "status": ReplayResultStatus(self.status).value,
            "home_score": self.home_score,
            "away_score": self.away_score,
            "actual_outcome": self.actual_outcome,
            "marker": self.marker,
        }

    def validate(self) -> None:
        if not self.prediction_id.strip() or not self.prediction_artifact_sha.strip():
            raise OfflineReplayError("result attachment lacks prediction provenance")
        if not self.fixture_key.strip() or self.league_code not in TOP5_REPLAY_LEAGUES:
            raise OfflineReplayError("result attachment identity is invalid")
        if not self.result_source.startswith("historical:"):
            raise OfflineReplayError("result source must be explicitly historical")
        if self.attached_at < self.result_timestamp:
            raise OfflineReplayError("result attachment precedes its source timestamp")
        status = ReplayResultStatus(self.status)
        scores = (self.home_score, self.away_score)
        if status is ReplayResultStatus.FINAL:
            if any(not isinstance(score, int) or score < 0 for score in scores):
                raise OfflineReplayError("final result requires non-negative scores")
            if self.actual_outcome != _result_outcome(self.home_score, self.away_score):
                raise OfflineReplayError("result outcome does not match the scores")
        elif any(score is not None for score in scores) or self.actual_outcome is not None:
            raise OfflineReplayError("non-final result cannot contain a score or outcome")
        if self.marker != OFFLINE_REPLAY_MARKER or _stable_digest(self._payload()) != self.attachment_sha:
            raise OfflineReplayError("result attachment digest or marker changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload(), "attachment_sha": self.attachment_sha}


@dataclass(frozen=True)
class ReplayClosingAttachment:
    prediction_id: str
    prediction_artifact_sha: str
    fixture_key: str
    league_code: str
    closing_source: str
    bookmaker: str
    closing_timestamp: datetime
    attached_at: datetime
    odds: Mapping[str, float]
    attachment_sha: str = ""
    marker: str = OFFLINE_REPLAY_MARKER
    used_for_prediction: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "closing_timestamp", _utc(self.closing_timestamp, "closing_timestamp"))
        object.__setattr__(self, "attached_at", _utc(self.attached_at, "attached_at"))
        object.__setattr__(self, "odds", MappingProxyType({name: float(value) for name, value in self.odds.items()}))
        if not self.attachment_sha:
            object.__setattr__(self, "attachment_sha", _stable_digest(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id,
            "prediction_artifact_sha": self.prediction_artifact_sha,
            "fixture_key": self.fixture_key,
            "league_code": self.league_code,
            "closing_source": self.closing_source,
            "bookmaker": self.bookmaker,
            "closing_timestamp": self.closing_timestamp.isoformat(),
            "attached_at": self.attached_at.isoformat(),
            "odds": dict(self.odds),
            "marker": self.marker,
            "used_for_prediction": self.used_for_prediction,
        }

    def validate(self) -> None:
        if not self.prediction_id.strip() or not self.prediction_artifact_sha.strip():
            raise OfflineReplayError("closing attachment lacks prediction provenance")
        if not self.fixture_key.strip() or self.league_code not in TOP5_REPLAY_LEAGUES:
            raise OfflineReplayError("closing attachment identity is invalid")
        if not self.closing_source.startswith("historical:") or not self.bookmaker.strip():
            raise OfflineReplayError("closing provenance must identify historical source and bookmaker")
        if self.attached_at < self.closing_timestamp or self.used_for_prediction:
            raise OfflineReplayError("closing benchmark cannot enter prediction inputs")
        if set(self.odds) != set(_OUTCOMES) or any(
            not isfinite(value) or value <= 1.0 for value in self.odds.values()
        ):
            raise OfflineReplayError("closing attachment odds are invalid")
        if self.marker != OFFLINE_REPLAY_MARKER or _stable_digest(self._payload()) != self.attachment_sha:
            raise OfflineReplayError("closing attachment digest or marker changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload(), "attachment_sha": self.attachment_sha}


@dataclass
class OfflineReplayArchive:
    """In-memory/test archive with a namespace distinct from real shadow."""

    predictions: dict[str, ReplayPredictionArtifact] = field(default_factory=dict)
    results: dict[str, ReplayResultAttachment] = field(default_factory=dict)
    closings: dict[str, ReplayClosingAttachment] = field(default_factory=dict)

    def _prediction(self, prediction_id: str) -> ReplayPredictionArtifact:
        prediction = self.predictions.get(prediction_id)
        if prediction is None:
            raise OfflineReplayError("attachment references a missing prediction")
        prediction.validate()
        return prediction

    def add_prediction(self, prediction: ReplayPredictionArtifact) -> bool:
        prediction.validate()
        old = self.predictions.get(prediction.prediction_id)
        if old is not None:
            if old == prediction:
                return False
            raise OfflineReplayError("prediction identity collision")
        self.predictions[prediction.prediction_id] = prediction
        return True

    def attach_result(self, result: ReplayResultAttachment) -> bool:
        result.validate()
        prediction = self._prediction(result.prediction_id)
        if result.prediction_artifact_sha != prediction.artifact_sha:
            raise OfflineReplayError("result references a changed prediction artifact")
        if result.fixture_key != prediction.fixture_key or result.league_code != prediction.league_code:
            raise OfflineReplayError("result fixture or league identity does not match prediction")
        if result.attached_at < prediction.replayed_at:
            raise OfflineReplayError("result attachment precedes prediction creation")
        old = self.results.get(result.prediction_id)
        if old is not None:
            if old == result:
                return False
            raise OfflineReplayError("conflicting result attachment")
        self.results[result.prediction_id] = result
        return True

    def attach_closing(self, closing: ReplayClosingAttachment) -> bool:
        closing.validate()
        prediction = self._prediction(closing.prediction_id)
        if closing.prediction_artifact_sha != prediction.artifact_sha:
            raise OfflineReplayError("closing references a changed prediction artifact")
        if closing.fixture_key != prediction.fixture_key or closing.league_code != prediction.league_code:
            raise OfflineReplayError("closing fixture or league identity does not match prediction")
        if closing.closing_timestamp < prediction.source_timestamp:
            raise OfflineReplayError("closing benchmark precedes signal-time source")
        if closing.attached_at < prediction.replayed_at:
            raise OfflineReplayError("closing attachment precedes prediction creation")
        old = self.closings.get(closing.prediction_id)
        if old is not None:
            if old == closing:
                return False
            raise OfflineReplayError("conflicting closing attachment")
        self.closings[closing.prediction_id] = closing
        return True

    def as_payload(self) -> dict[str, object]:
        for prediction in self.predictions.values():
            prediction.validate()
        for result in self.results.values():
            self.attach_result(result)
        for closing in self.closings.values():
            self.attach_closing(closing)
        return {
            "marker": OFFLINE_REPLAY_MARKER,
            "status": ENGINEERING_VALIDATION_MARKER,
            "namespace": OFFLINE_REPLAY_NAMESPACE,
            "real_shadow_namespace": REAL_SHADOW_NAMESPACE,
            "promotable_to_real_observed": False,
            "predictions": [
                item.as_payload() for item in sorted(self.predictions.values(), key=lambda value: value.prediction_id)
            ],
            "results": [
                item.as_payload() for item in sorted(self.results.values(), key=lambda value: value.prediction_id)
            ],
            "closing_benchmarks": [
                item.as_payload() for item in sorted(self.closings.values(), key=lambda value: value.prediction_id)
            ],
        }

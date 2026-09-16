"""Append-only result and closing attachments for real-shadow sessions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite
from types import MappingProxyType

from src.football.top5_real_shadow_contracts import (
    TOP5_REAL_SHADOW_LEAGUES,
    RealShadowContractError,
    _digest,
    _parse_datetime,
    _required_text,
    _sha,
)


class RealShadowResultStatus(str, Enum):
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class RealShadowResultAttachment:
    prediction_id: str
    prediction_artifact_sha: str
    fixture_key: str
    league_code: str
    result_source: str
    provider_result_id: str | None
    result_timestamp: datetime
    attached_at: datetime
    status: RealShadowResultStatus
    home_score: int | None
    away_score: int | None
    actual_outcome: str | None
    attachment_sha: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_timestamp", _parse_datetime(self.result_timestamp, "result_timestamp"))
        object.__setattr__(self, "attached_at", _parse_datetime(self.attached_at, "attached_at"))
        if not self.attachment_sha:
            object.__setattr__(self, "attachment_sha", _digest(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id,
            "prediction_artifact_sha": self.prediction_artifact_sha,
            "fixture_key": self.fixture_key,
            "league_code": self.league_code,
            "result_source": self.result_source,
            "provider_result_id": self.provider_result_id,
            "result_timestamp": self.result_timestamp.isoformat(),
            "attached_at": self.attached_at.isoformat(),
            "status": RealShadowResultStatus(self.status).value,
            "home_score": self.home_score,
            "away_score": self.away_score,
            "actual_outcome": self.actual_outcome,
        }

    def validate(self) -> None:
        for name, value in (
            ("prediction_id", self.prediction_id),
            ("prediction_artifact_sha", self.prediction_artifact_sha),
            ("fixture_key", self.fixture_key),
            ("league_code", self.league_code),
            ("result_source", self.result_source),
        ):
            _required_text(value, name)
        _sha(self.prediction_artifact_sha, "prediction_artifact_sha")
        if self.league_code not in TOP5_REAL_SHADOW_LEAGUES or self.attached_at < self.result_timestamp:
            raise RealShadowContractError("result attachment identity or timestamp is invalid")
        status = RealShadowResultStatus(self.status)
        if status is RealShadowResultStatus.FINAL:
            scores = (self.home_score, self.away_score)
            if any(not isinstance(score, int) or isinstance(score, bool) or score < 0 for score in scores):
                raise RealShadowContractError("final result requires non-negative scores")
            expected = "home" if self.home_score > self.away_score else "away" if self.home_score < self.away_score else "draw"
            if self.actual_outcome != expected:
                raise RealShadowContractError("result outcome does not match scores")
        elif any(score is not None for score in (self.home_score, self.away_score)) or self.actual_outcome is not None:
            raise RealShadowContractError("non-final result cannot contain scores or outcome")
        if _digest(self._payload()) != self.attachment_sha:
            raise RealShadowContractError("result attachment digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload(), "attachment_sha": self.attachment_sha}

    @classmethod
    def from_payload(cls, payload: object) -> RealShadowResultAttachment:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            prediction_id=raw.get("prediction_id", ""), prediction_artifact_sha=raw.get("prediction_artifact_sha", ""),
            fixture_key=raw.get("fixture_key", ""), league_code=raw.get("league_code", ""),
            result_source=raw.get("result_source", ""), provider_result_id=raw.get("provider_result_id"),
            result_timestamp=_parse_datetime(raw.get("result_timestamp"), "result_timestamp"),
            attached_at=_parse_datetime(raw.get("attached_at"), "attached_at"), status=raw.get("status", ""),
            home_score=raw.get("home_score"), away_score=raw.get("away_score"), actual_outcome=raw.get("actual_outcome"),
            attachment_sha=raw.get("attachment_sha", ""),
        )


@dataclass(frozen=True)
class RealShadowClosingAttachment:
    prediction_id: str
    prediction_artifact_sha: str
    fixture_key: str
    league_code: str
    closing_source: str
    bookmaker: str
    closing_timestamp: datetime
    attached_at: datetime
    odds: Mapping[str, float]
    closing_snapshot_id: str
    attachment_sha: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "closing_timestamp", _parse_datetime(self.closing_timestamp, "closing_timestamp"))
        object.__setattr__(self, "attached_at", _parse_datetime(self.attached_at, "attached_at"))
        if not isinstance(self.odds, Mapping):
            raise RealShadowContractError("closing odds must be a mapping")
        object.__setattr__(self, "odds", MappingProxyType({name: float(value) for name, value in self.odds.items()}))
        if not self.attachment_sha:
            object.__setattr__(self, "attachment_sha", _digest(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id, "prediction_artifact_sha": self.prediction_artifact_sha,
            "fixture_key": self.fixture_key, "league_code": self.league_code, "closing_source": self.closing_source,
            "bookmaker": self.bookmaker, "closing_timestamp": self.closing_timestamp.isoformat(),
            "attached_at": self.attached_at.isoformat(), "odds": dict(self.odds),
            "closing_snapshot_id": self.closing_snapshot_id,
        }

    def validate(self) -> None:
        for name, value in (
            ("prediction_id", self.prediction_id), ("prediction_artifact_sha", self.prediction_artifact_sha),
            ("fixture_key", self.fixture_key), ("league_code", self.league_code),
            ("closing_source", self.closing_source), ("bookmaker", self.bookmaker),
            ("closing_snapshot_id", self.closing_snapshot_id),
        ):
            _required_text(value, name)
        _sha(self.prediction_artifact_sha, "prediction_artifact_sha")
        if self.league_code not in TOP5_REAL_SHADOW_LEAGUES or self.attached_at < self.closing_timestamp:
            raise RealShadowContractError("closing attachment identity or timestamp is invalid")
        if set(self.odds) != {"home", "draw", "away"} or any(not isfinite(value) or value <= 1.0 for value in self.odds.values()):
            raise RealShadowContractError("closing odds are invalid")
        if _digest(self._payload()) != self.attachment_sha:
            raise RealShadowContractError("closing attachment digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload(), "attachment_sha": self.attachment_sha}

    @classmethod
    def from_payload(cls, payload: object) -> RealShadowClosingAttachment:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            prediction_id=raw.get("prediction_id", ""), prediction_artifact_sha=raw.get("prediction_artifact_sha", ""),
            fixture_key=raw.get("fixture_key", ""), league_code=raw.get("league_code", ""),
            closing_source=raw.get("closing_source", ""), bookmaker=raw.get("bookmaker", ""),
            closing_timestamp=_parse_datetime(raw.get("closing_timestamp"), "closing_timestamp"),
            attached_at=_parse_datetime(raw.get("attached_at"), "attached_at"), odds=raw.get("odds", {}),
            closing_snapshot_id=raw.get("closing_snapshot_id", ""), attachment_sha=raw.get("attachment_sha", ""),
        )


__all__ = ["RealShadowClosingAttachment", "RealShadowResultAttachment", "RealShadowResultStatus"]

"""Disabled-by-default contracts for future league-isolated football production.

This module intentionally registers no league, model, job, provider request, or
publisher path. It is a boundary contract for a later CEO-approved adapter.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable


class ProductionContractError(ValueError):
    """Raised when a future league adapter violates a production boundary."""


class ActivationMode(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    CONTROLLED = "controlled"
    LIVE = "live"


class MarketSnapshotKind(str, Enum):
    SIGNAL_TIME = "signal_time"
    CLOSING = "closing"


TOP5_STAGED_ARTIFACT_PREFIX = "docs/data/top5/shadow/"


@dataclass(frozen=True)
class SignalTimeContract:
    """An explicitly approved pre-kickoff window for one league adapter.

    There is deliberately no default timing. A future rollout must supply a
    CEO-approved contract based on actual schedule and provider evidence.
    """

    minimum_minutes_before_kickoff: int
    maximum_minutes_before_kickoff: int
    maximum_odds_age_seconds: int

    def validate(self) -> None:
        if self.minimum_minutes_before_kickoff < 0:
            raise ProductionContractError("minimum lead time must be non-negative")
        if self.maximum_minutes_before_kickoff < self.minimum_minutes_before_kickoff:
            raise ProductionContractError("maximum lead time must be >= minimum lead time")
        if self.maximum_odds_age_seconds <= 0:
            raise ProductionContractError("maximum odds age must be positive")

    def accepts(self, kickoff: datetime, odds_captured_at: datetime, now: datetime) -> bool:
        self.validate()
        kickoff = _utc(kickoff, "kickoff")
        odds_captured_at = _utc(odds_captured_at, "odds_captured_at")
        now = _utc(now, "now")
        lead_minutes = (kickoff - now).total_seconds() / 60
        odds_age = (now - odds_captured_at).total_seconds()
        return (
            self.minimum_minutes_before_kickoff <= lead_minutes <= self.maximum_minutes_before_kickoff
            and 0 <= odds_age <= self.maximum_odds_age_seconds
        )


@dataclass(frozen=True)
class LeagueProductionConfig:
    """League-owned metadata without registering any live production league."""

    league_code: str
    display_name: str
    provider_sport_key: str
    fixture_source: str
    result_source: str
    model_adapter_id: str
    activation_mode: ActivationMode = ActivationMode.DISABLED
    signal_time: SignalTimeContract | None = None

    def validate(self) -> None:
        required = {
            "league_code": self.league_code,
            "display_name": self.display_name,
            "provider_sport_key": self.provider_sport_key,
            "fixture_source": self.fixture_source,
            "result_source": self.result_source,
            "model_adapter_id": self.model_adapter_id,
        }
        missing = [name for name, value in required.items() if not value or not value.strip()]
        if missing:
            raise ProductionContractError(f"missing required league fields: {', '.join(missing)}")
        if not self.provider_sport_key.startswith("soccer_"):
            raise ProductionContractError("provider_sport_key must be a soccer key")
        if self.activation_mode is not ActivationMode.DISABLED and self.signal_time is None:
            raise ProductionContractError("an enabled adapter requires an approved signal-time contract")
        if self.signal_time is not None:
            self.signal_time.validate()

    def assert_disabled(self) -> None:
        self.validate()
        if self.activation_mode is not ActivationMode.DISABLED:
            raise ProductionContractError("Top-5 production adapters must remain disabled")


@dataclass(frozen=True)
class Fixture:
    fixture_key: str
    league_code: str
    home_team: str
    away_team: str
    kickoff: datetime


@dataclass(frozen=True)
class MarketSnapshot:
    fixture_key: str
    captured_at: datetime
    kind: MarketSnapshotKind
    source: str
    odds: Mapping[str, float]

@dataclass(frozen=True)
class PredictionInput:
    """Model-facing input. Closing snapshots are structurally excluded."""

    fixture: Fixture
    signal_snapshot: MarketSnapshot
    features: Mapping[str, float]

    @classmethod
    def create(
        cls,
        fixture: Fixture,
        signal_snapshot: MarketSnapshot,
        features: Mapping[str, float],
        signal_time: SignalTimeContract,
        now: datetime,
    ) -> PredictionInput:
        if signal_snapshot.fixture_key != fixture.fixture_key:
            raise ProductionContractError("fixture and market snapshot identity differ")
        if signal_snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ProductionContractError("closing odds are not valid prediction input")
        if not signal_time.accepts(fixture.kickoff, signal_snapshot.captured_at, now):
            raise ProductionContractError("signal-time contract rejected this snapshot")
        return cls(fixture=fixture, signal_snapshot=signal_snapshot, features=features)


@dataclass(frozen=True)
class ShadowSignalArtifact:
    """In-memory shadow output; no ledger or publisher side effect is available here."""

    signal_id: str
    fixture_key: str
    league_code: str
    source: str
    model_adapter_id: str
    activation_mode: ActivationMode
    no_bet_flag: bool
    reason: str

    def validate(self) -> None:
        if self.activation_mode is not ActivationMode.SHADOW:
            raise ProductionContractError("new Top-5 artifacts may only be shadow artifacts")
        missing = [
            name for name, value in {
                "signal_id": self.signal_id,
                "fixture_key": self.fixture_key,
                "league_code": self.league_code,
                "source": self.source,
                "model_adapter_id": self.model_adapter_id,
                "reason": self.reason,
            }.items() if not value
        ]
        if missing:
            raise ProductionContractError(f"shadow artifact lacks provenance: {', '.join(missing)}")
        if not self.no_bet_flag:
            raise ProductionContractError("shadow artifacts must remain no-bet")


@runtime_checkable
class FixtureIngestor(Protocol):
    def fetch(self, config: LeagueProductionConfig) -> Sequence[Fixture]: ...


class FeatureAdapter(Protocol):
    def build(self, fixture: Fixture, snapshot: MarketSnapshot) -> Mapping[str, float]: ...


class ModelAdapter(Protocol):
    def predict(self, model_input: PredictionInput) -> Mapping[str, float]: ...


class SignalDecider(Protocol):
    def decide(
        self,
        fixture: Fixture,
        prediction: Mapping[str, float],
        snapshot: MarketSnapshot,
    ) -> ShadowSignalArtifact: ...


def validate_disabled_top5_configs(configs: Sequence[LeagueProductionConfig]) -> None:
    """Guard used by a future registration point before any activation work."""
    seen_codes: set[str] = set()
    seen_sport_keys: set[str] = set()
    for config in configs:
        config.assert_disabled()
        if config.league_code in seen_codes:
            raise ProductionContractError(f"duplicate league_code: {config.league_code}")
        if config.provider_sport_key in seen_sport_keys:
            raise ProductionContractError(f"duplicate provider_sport_key: {config.provider_sport_key}")
        seen_codes.add(config.league_code)
        seen_sport_keys.add(config.provider_sport_key)


def validate_artifact_ownership(path: str) -> None:
    """Allow only a future Top-5 shadow artifact in its staged-public namespace."""
    normalized = path.strip()
    parts = normalized.split("/")
    if (
        not normalized.startswith(TOP5_STAGED_ARTIFACT_PREFIX)
        or len(parts) < 5
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
        or "secret" in normalized.lower()
        or not normalized.endswith(".json")
    ):
        raise ProductionContractError("Top-5 artifact path is not runtime-safe")


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProductionContractError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)

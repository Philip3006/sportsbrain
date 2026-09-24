"""Disabled-by-default Champions League model and shadow-signal contracts.

The module reuses the generic football signal-time and prediction contracts, but
keeps Champions League identity and model-artifact provenance explicit.  It
does not register a league, call a provider, publish a signal, or write betting
or runtime state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import exp, isfinite
from types import MappingProxyType

from src.football.odds.base import canonical_team
from src.football.production_contracts import (
    ActivationMode,
    Fixture,
    LeagueProductionConfig,
    MarketSnapshot,
    MarketSnapshotKind,
    PredictionArtifact,
    PredictionInput,
    ProductionContractError,
    ProviderMapping,
    ShadowSignalArtifact,
    SignalTimeContract,
    _utc,
)

CHAMPIONS_LEAGUE_CODE = "UCL"
CHAMPIONS_LEAGUE_NAME = "UEFA Champions League"
CHAMPIONS_LEAGUE_SPORT_KEY = "soccer_uefa_champs_league"
CHAMPIONS_LEAGUE_PROVIDER = "the_odds_api"
CHAMPIONS_LEAGUE_MODEL_ID = "champions-league-elo-shadow-v1"
CHAMPIONS_LEAGUE_FEATURE_SCHEMA = ("away_elo", "home_elo", "neutral_ground")
CHAMPIONS_LEAGUE_OUTCOMES = ("away", "draw", "home")
_SHA_LENGTH = 64
_FORBIDDEN_FEATURE_TOKENS = (
    "closing",
    "odds",
    "result",
    "settlement",
    "post_kickoff",
    "future",
)


class ChampionsLeagueContractError(ProductionContractError):
    """Raised when CL model or signal provenance cannot be established."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChampionsLeagueContractError(f"{field} is required")
    return value.strip()


def _sha(value: object, field: str) -> str:
    candidate = _text(value, field).lower()
    if len(candidate) != _SHA_LENGTH or any(
        char not in "0123456789abcdef" for char in candidate
    ):
        raise ChampionsLeagueContractError(f"{field} must be a SHA-256 hex digest")
    return candidate


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_team_identity(value: object) -> str:
    """Normalize a display name without inferring or inventing a team ID."""

    name = _text(value, "team name")
    normalized = canonical_team(name)
    if not normalized:
        raise ChampionsLeagueContractError("team name normalizes to empty")
    return normalized


def canonical_fixture_key(provider_event_id: object) -> str:
    """Build the only accepted CL fixture-key form from a provider event ID."""

    event_id = _text(provider_event_id, "provider_event_id")
    return f"{CHAMPIONS_LEAGUE_CODE}:{event_id}"


def champions_league_config() -> LeagueProductionConfig:
    """Return CL metadata without adding it to the active league registry."""

    return LeagueProductionConfig(
        league_code=CHAMPIONS_LEAGUE_CODE,
        display_name=CHAMPIONS_LEAGUE_NAME,
        provider_sport_key=CHAMPIONS_LEAGUE_SPORT_KEY,
        fixture_source=CHAMPIONS_LEAGUE_PROVIDER,
        result_source="unbound",
        model_adapter_id=CHAMPIONS_LEAGUE_MODEL_ID,
        activation_mode=ActivationMode.DISABLED,
        provider_mapping=ProviderMapping(
            provider_name=CHAMPIONS_LEAGUE_PROVIDER,
            competition_id=CHAMPIONS_LEAGUE_CODE,
            sport_key=CHAMPIONS_LEAGUE_SPORT_KEY,
            markets=("h2h",),
            regions=("eu",),
        ),
    )


@dataclass(frozen=True)
class ChampionsLeagueFixture:
    """Canonical CL fixture identity from a real provider event binding."""

    fixture_key: str
    provider_event_id: str
    home_team_id: str
    away_team_id: str
    home_team: str
    away_team: str
    kickoff: datetime
    neutral_ground: bool = False
    competition_code: str = CHAMPIONS_LEAGUE_CODE

    def __post_init__(self) -> None:
        object.__setattr__(self, "kickoff", _utc(self.kickoff, "kickoff"))

    def validate(self) -> None:
        _utc(self.kickoff, "kickoff")
        if self.competition_code != CHAMPIONS_LEAGUE_CODE:
            raise ChampionsLeagueContractError(
                "fixture is not a canonical Champions League fixture"
            )
        if self.fixture_key != canonical_fixture_key(self.provider_event_id):
            raise ChampionsLeagueContractError(
                "fixture key is not bound to provider event identity"
            )
        home_id = _text(self.home_team_id, "home_team_id")
        away_id = _text(self.away_team_id, "away_team_id")
        if home_id == away_id:
            raise ChampionsLeagueContractError("fixture team IDs must be distinct")
        if canonical_team_identity(self.home_team) == canonical_team_identity(
            self.away_team
        ):
            raise ChampionsLeagueContractError("fixture team names must be distinct")

    def as_generic_fixture(self) -> Fixture:
        self.validate()
        return Fixture(
            fixture_key=self.fixture_key,
            league_code=CHAMPIONS_LEAGUE_CODE,
            home_team=self.home_team.strip(),
            away_team=self.away_team.strip(),
            kickoff=self.kickoff,
        )


@dataclass(frozen=True)
class ChampionsLeagueFeatureSnapshot:
    """Point-in-time feature values required by the deterministic CL adapter."""

    values: Mapping[str, float]
    observed_at: datetime
    source_id: str
    source_sha: str
    research_sha: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "observed_at", _utc(self.observed_at, "feature_observed_at")
        )
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def validate(self, *, kickoff: datetime, neutral_ground: bool) -> None:
        kickoff = _utc(kickoff, "kickoff")
        if _utc(self.observed_at, "feature_observed_at") >= kickoff:
            raise ChampionsLeagueContractError(
                "feature snapshot is not strictly pre-kickoff"
            )
        _text(self.source_id, "feature source_id")
        _sha(self.source_sha, "feature source_sha")
        _sha(self.research_sha, "feature research_sha")
        if tuple(self.values) != CHAMPIONS_LEAGUE_FEATURE_SCHEMA:
            raise ChampionsLeagueContractError(
                "feature schema is not the canonical CL schema"
            )
        expected_neutral = 1.0 if neutral_ground else 0.0
        for name, value in self.values.items():
            if any(token in name.lower() for token in _FORBIDDEN_FEATURE_TOKENS):
                raise ChampionsLeagueContractError(
                    f"future or market feature is forbidden: {name}"
                )
            if not isfinite(float(value)):
                raise ChampionsLeagueContractError(f"feature {name!r} is not finite")
        if float(self.values["neutral_ground"]) != expected_neutral:
            raise ChampionsLeagueContractError(
                "neutral_ground feature disagrees with fixture"
            )


@dataclass(frozen=True)
class ChampionsLeagueModelArtifact:
    """Explicit model/calibration binding; only offline candidates are accepted."""

    model_id: str
    model_version: str
    artifact_sha: str
    calibration_artifact_sha: str
    research_sha: str
    feature_schema: tuple[str, ...] = CHAMPIONS_LEAGUE_FEATURE_SCHEMA
    release_state: str = "offline_candidate"

    def validate(self) -> None:
        _text(self.model_id, "model_id")
        _text(self.model_version, "model_version")
        _sha(self.artifact_sha, "model artifact_sha")
        _sha(self.calibration_artifact_sha, "calibration_artifact_sha")
        _sha(self.research_sha, "model research_sha")
        if tuple(self.feature_schema) != CHAMPIONS_LEAGUE_FEATURE_SCHEMA:
            raise ChampionsLeagueContractError(
                "model feature schema does not match CL input schema"
            )
        if self.release_state != "offline_candidate":
            raise ChampionsLeagueContractError(
                "CL model is not approved beyond offline candidate state"
            )


@dataclass(frozen=True)
class ChampionsLeagueEloModel:
    """Deterministic offline candidate model with no provider or publication behavior."""

    artifact: ChampionsLeagueModelArtifact

    def predict(self, model_input: PredictionInput) -> Mapping[str, float]:
        self.artifact.validate()
        if model_input.fixture.league_code != CHAMPIONS_LEAGUE_CODE:
            raise ChampionsLeagueContractError("model input is not Champions League")
        if tuple(model_input.features) != CHAMPIONS_LEAGUE_FEATURE_SCHEMA:
            raise ChampionsLeagueContractError(
                "model input feature schema is not canonical"
            )
        home_elo = float(model_input.features["home_elo"])
        away_elo = float(model_input.features["away_elo"])
        if not all(isfinite(value) for value in (home_elo, away_elo)):
            raise ChampionsLeagueContractError("Elo features must be finite")
        margin = (home_elo - away_elo) / 400.0
        home_share = 1.0 / (1.0 + 10.0 ** (-margin))
        draw = 0.24 * exp(-abs(home_elo - away_elo) / 400.0)
        remainder = 1.0 - draw
        probabilities = {
            "away": remainder * (1.0 - home_share),
            "draw": draw,
            "home": remainder * home_share,
        }
        return MappingProxyType(probabilities)


@dataclass(frozen=True)
class ChampionsLeagueShadowResult:
    """Generic prediction/signal artifacts with CL-specific artifact provenance."""

    fixture: ChampionsLeagueFixture
    model_input: PredictionInput
    prediction: PredictionArtifact
    signal: ShadowSignalArtifact
    model_artifact: ChampionsLeagueModelArtifact
    feature_snapshot: ChampionsLeagueFeatureSnapshot

    def validate(self) -> None:
        self.model_artifact.validate()
        self.fixture.validate()
        self.model_input.fixture.validate()
        self.model_input.signal_snapshot.validate()
        if self.model_input.fixture != self.fixture.as_generic_fixture():
            raise ChampionsLeagueContractError(
                "model input fixture differs from canonical CL fixture"
            )
        self.feature_snapshot.validate(
            kickoff=self.fixture.kickoff,
            neutral_ground=self.fixture.neutral_ground,
        )
        if self.feature_snapshot.research_sha != self.model_artifact.research_sha:
            raise ChampionsLeagueContractError(
                "feature/model research identity differs"
            )
        if self.model_input.signal_snapshot.source != CHAMPIONS_LEAGUE_PROVIDER:
            raise ChampionsLeagueContractError("model input provider identity differs")
        self.prediction.validate()
        self.signal.validate()
        if self.prediction.model_adapter_id != self.model_artifact.model_id:
            raise ChampionsLeagueContractError(
                "prediction/model artifact identity differs"
            )
        if self.prediction.fixture_key != self.model_input.fixture.fixture_key:
            raise ChampionsLeagueContractError("prediction fixture identity differs")
        if self.prediction.snapshot_id != self.model_input.signal_snapshot.snapshot_id:
            raise ChampionsLeagueContractError("prediction snapshot identity differs")
        if self.prediction.league_code != CHAMPIONS_LEAGUE_CODE:
            raise ChampionsLeagueContractError("prediction league identity differs")
        if set(self.prediction.probabilities) != set(CHAMPIONS_LEAGUE_OUTCOMES):
            raise ChampionsLeagueContractError("prediction outcomes are not canonical")
        if abs(sum(self.prediction.probabilities.values()) - 1.0) > 1e-9:
            raise ChampionsLeagueContractError(
                "prediction probabilities must sum to one"
            )
        if self.signal.prediction_id != self.prediction.prediction_id:
            raise ChampionsLeagueContractError("signal/prediction identity differs")
        if self.signal.snapshot_id != self.prediction.snapshot_id:
            raise ChampionsLeagueContractError("signal/snapshot identity differs")
        if self.signal.model_adapter_id != self.model_artifact.model_id:
            raise ChampionsLeagueContractError("signal/model artifact identity differs")
        if (
            not self.signal.no_bet_flag
            or self.signal.activation_mode is not ActivationMode.SHADOW
        ):
            raise ChampionsLeagueContractError(
                "CL shadow signals must remain no-bet shadow artifacts"
            )


@dataclass(frozen=True)
class ClosingOddsBenchmark:
    """Closing odds are retained for evaluation only and never enter model input."""

    prediction_id: str
    fixture_key: str
    snapshot_id: str
    captured_at: datetime
    odds: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "captured_at", _utc(self.captured_at, "closing_captured_at")
        )
        object.__setattr__(self, "odds", MappingProxyType(dict(self.odds)))

    def validate(self) -> None:
        _text(self.prediction_id, "benchmark prediction_id")
        _text(self.fixture_key, "benchmark fixture_key")
        _text(self.snapshot_id, "benchmark snapshot_id")
        _utc(self.captured_at, "closing_captured_at")
        if not self.odds:
            raise ChampionsLeagueContractError("closing benchmark requires odds")
        for name, value in self.odds.items():
            if not name.strip() or not isfinite(float(value)) or float(value) <= 1.0:
                raise ChampionsLeagueContractError(
                    "closing benchmark contains invalid odds"
                )


def build_shadow_prediction(
    fixture: ChampionsLeagueFixture,
    signal_snapshot: MarketSnapshot,
    features: ChampionsLeagueFeatureSnapshot,
    model: ChampionsLeagueEloModel,
    signal_time: SignalTimeContract,
    now: datetime,
) -> ChampionsLeagueShadowResult:
    """Build one deterministic, no-bet CL shadow result from signal-time data."""

    fixture.validate()
    features.validate(kickoff=fixture.kickoff, neutral_ground=fixture.neutral_ground)
    if signal_snapshot.source != CHAMPIONS_LEAGUE_PROVIDER:
        raise ChampionsLeagueContractError(
            "CL model input must use the canonical production provider"
        )
    if not signal_snapshot.snapshot_id.strip():
        raise ChampionsLeagueContractError("signal snapshot identity is required")
    model_input = PredictionInput.create(
        fixture=fixture.as_generic_fixture(),
        signal_snapshot=signal_snapshot,
        features=features.values,
        signal_time=signal_time,
        now=now,
    )
    now_utc = _utc(now, "now")
    if now_utc >= fixture.kickoff:
        raise ChampionsLeagueContractError("CL prediction time must be before kickoff")
    model_artifact = model.artifact
    probabilities = model.predict(model_input)
    prediction_id = _digest(
        {
            "fixture_key": fixture.fixture_key,
            "snapshot_id": signal_snapshot.snapshot_id,
            "model_artifact_sha": model_artifact.artifact_sha,
            "calibration_artifact_sha": model_artifact.calibration_artifact_sha,
            "feature_values": dict(features.values),
        }
    )
    prediction = PredictionArtifact(
        prediction_id=prediction_id,
        fixture_key=fixture.fixture_key,
        league_code=CHAMPIONS_LEAGUE_CODE,
        model_adapter_id=model_artifact.model_id,
        generated_at=now_utc,
        snapshot_id=signal_snapshot.snapshot_id,
        snapshot_kind=MarketSnapshotKind.SIGNAL_TIME,
        probabilities=probabilities,
    )
    signal = ShadowSignalArtifact(
        signal_id=_digest({"prediction_id": prediction_id, "signal": "shadow-no-bet"}),
        fixture_key=fixture.fixture_key,
        league_code=CHAMPIONS_LEAGUE_CODE,
        source=CHAMPIONS_LEAGUE_PROVIDER,
        model_adapter_id=model_artifact.model_id,
        activation_mode=ActivationMode.SHADOW,
        no_bet_flag=True,
        reason="champions_league_offline_candidate_shadow_no_bet",
        prediction_id=prediction_id,
        snapshot_id=signal_snapshot.snapshot_id,
        snapshot_kind=MarketSnapshotKind.SIGNAL_TIME,
    )
    result = ChampionsLeagueShadowResult(
        fixture=fixture,
        model_input=model_input,
        prediction=prediction,
        signal=signal,
        model_artifact=model_artifact,
        feature_snapshot=features,
    )
    result.validate()
    return result


def build_closing_benchmark(
    result: ChampionsLeagueShadowResult,
    closing_snapshot: MarketSnapshot,
) -> ClosingOddsBenchmark:
    """Record a closing snapshot without making it available to prediction code."""

    result.validate()
    closing_snapshot.validate()
    if closing_snapshot.kind is not MarketSnapshotKind.CLOSING:
        raise ChampionsLeagueContractError("benchmark requires a closing snapshot")
    if closing_snapshot.fixture_key != result.prediction.fixture_key:
        raise ChampionsLeagueContractError(
            "closing benchmark fixture differs from prediction"
        )
    benchmark = ClosingOddsBenchmark(
        prediction_id=result.prediction.prediction_id,
        fixture_key=result.prediction.fixture_key,
        snapshot_id=closing_snapshot.snapshot_id,
        captured_at=closing_snapshot.captured_at,
        odds=closing_snapshot.odds,
    )
    benchmark.validate()
    return benchmark


__all__ = [
    "CHAMPIONS_LEAGUE_CODE",
    "CHAMPIONS_LEAGUE_FEATURE_SCHEMA",
    "CHAMPIONS_LEAGUE_MODEL_ID",
    "CHAMPIONS_LEAGUE_NAME",
    "CHAMPIONS_LEAGUE_PROVIDER",
    "CHAMPIONS_LEAGUE_SPORT_KEY",
    "ChampionsLeagueContractError",
    "ChampionsLeagueEloModel",
    "ChampionsLeagueFeatureSnapshot",
    "ChampionsLeagueFixture",
    "ChampionsLeagueModelArtifact",
    "ChampionsLeagueShadowResult",
    "ClosingOddsBenchmark",
    "build_closing_benchmark",
    "build_shadow_prediction",
    "canonical_fixture_key",
    "canonical_team_identity",
    "champions_league_config",
]

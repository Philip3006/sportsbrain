"""Static compatibility harness and shadow artifact flow for Top-5 readiness."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from src.football.production_contracts import (
    TOP5_SHADOW_ARCHIVE_PREFIX,
    TOP5_STAGED_ARTIFACT_PREFIX,
    ActivationMode,
    ArtifactOwner,
    FeatureAdapter,
    Fixture,
    LeagueProductionConfig,
    MarketSnapshot,
    MarketSnapshotKind,
    ModelAdapter,
    OddsRequest,
    PredictionInput,
    ProductionContractError,
    ShadowSignalArtifact,
    SignalDecider,
    SignalTimeContract,
    validate_artifact_ownership,
)
from src.football.production_pipeline import ShadowPipelineResult, run_shadow_pipeline
from src.football.top5_adapters import Top5LeagueAdapter
from src.football.top5_dispatch import signal_time_contract_id
from src.football.top5_health import Top5ShadowHealth


@dataclass
class StaticFixtureIngestor:
    fixtures: tuple[Fixture, ...]

    def fetch(self, config: LeagueProductionConfig) -> Sequence[Fixture]:
        return self.fixtures


@dataclass
class StaticOddsProvider:
    snapshots: tuple[MarketSnapshot, ...]
    name: str = "the_odds_api"
    requests: list[OddsRequest] = field(default_factory=list)

    def fetch(self, request: OddsRequest) -> Sequence[MarketSnapshot]:
        self.requests.append(request)
        wanted = set(request.fixture_keys)
        return tuple(snapshot for snapshot in self.snapshots if snapshot.fixture_key in wanted)


class DeterministicFeatureAdapter:
    """Feature boundary for tests; it does not load research or a model."""

    def build(self, fixture: Fixture, snapshot: MarketSnapshot) -> Mapping[str, float]:
        fixture.validate()
        snapshot.validate()
        if snapshot.fixture_key != fixture.fixture_key:
            raise ProductionContractError("offline feature boundary received a foreign fixture")
        return {
            "home_odds": float(snapshot.odds.get("home", 0.0)),
            "away_odds": float(snapshot.odds.get("away", 0.0)),
        }


class DeterministicDummyModel:
    """Injected dummy model used only to prove the model boundary."""

    def __init__(self, identity: str = "offline-dummy-model") -> None:
        self.identity = identity
        self.inputs: list[PredictionInput] = []

    def predict(self, model_input: PredictionInput) -> Mapping[str, float]:
        self.inputs.append(model_input)
        home_odds = float(model_input.features.get("home_odds", 2.0))
        away_odds = float(model_input.features.get("away_odds", 2.0))
        total = max(home_odds + away_odds, 1.0)
        return {"home": away_odds / total, "away": home_odds / total}


class NoBetShadowDecider:
    """Injected decision boundary that always produces a no-bet shadow signal."""

    def __init__(self, model_adapter_id: str) -> None:
        self.model_adapter_id = model_adapter_id

    def decide(
        self,
        fixture: Fixture,
        prediction: Mapping[str, float],
        snapshot: MarketSnapshot,
    ) -> ShadowSignalArtifact:
        if snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ProductionContractError("offline decider cannot consume closing odds")
        return ShadowSignalArtifact(
            signal_id=f"shadow:{fixture.league_code}:{fixture.fixture_key}",
            fixture_key=fixture.fixture_key,
            league_code=fixture.league_code,
            source="offline-static-shadow",
            model_adapter_id=self.model_adapter_id,
            activation_mode=ActivationMode.SHADOW,
            no_bet_flag=True,
            reason="offline_compatibility_no_bet",
            snapshot_kind=MarketSnapshotKind.SIGNAL_TIME,
        )


@dataclass
class MemoryShadowArtifactSink:
    artifacts: list[ShadowSignalArtifact] = field(default_factory=list)

    def write(self, artifact: ShadowSignalArtifact) -> None:
        artifact.validate()
        self.artifacts.append(artifact)


@dataclass(frozen=True)
class ShadowArtifactReference:
    signal_id: str
    shadow_archive_path: str
    staged_public_path: str
    publication_enabled: bool = False

    def validate(self) -> None:
        validate_artifact_ownership(self.shadow_archive_path, ArtifactOwner.SHADOW_ARCHIVE)
        validate_artifact_ownership(self.staged_public_path, ArtifactOwner.STAGED_PUBLIC)
        if self.publication_enabled:
            raise ProductionContractError("shadow artifact publication must remain disabled")


@dataclass(frozen=True)
class ShadowArtifactFlow:
    """Prediction -> signal -> archive -> staged reference, without publishing."""

    prediction_ids: tuple[str, ...]
    signal_ids: tuple[str, ...]
    signal_prediction_ids: tuple[str | None, ...]
    artifact_references: tuple[ShadowArtifactReference, ...]
    publication_enabled: bool = False

    def validate(self) -> None:
        if self.publication_enabled:
            raise ProductionContractError("shadow artifact publication must remain disabled")
        if (
            len(self.prediction_ids) != len(self.signal_ids)
            or len(self.signal_ids) != len(self.signal_prediction_ids)
            or len(self.signal_ids) != len(self.artifact_references)
        ):
            raise ProductionContractError("shadow artifact flow has incomplete stages")
        if len(set(self.prediction_ids)) != len(self.prediction_ids):
            raise ProductionContractError("shadow artifact flow has duplicate predictions")
        if len(set(self.signal_ids)) != len(self.signal_ids):
            raise ProductionContractError("shadow artifact flow has duplicate signals")
        if tuple(self.signal_prediction_ids) != self.prediction_ids:
            raise ProductionContractError("shadow artifact flow lost prediction provenance")
        for reference in self.artifact_references:
            reference.validate()


@dataclass(frozen=True)
class OfflineCompatibilityResult:
    pipeline: ShadowPipelineResult
    artifact_flow: ShadowArtifactFlow
    health: Top5ShadowHealth

    @property
    def artifact_references(self) -> tuple[ShadowArtifactReference, ...]:
        return self.artifact_flow.artifact_references

    @property
    def no_bet(self) -> bool:
        return all(signal.no_bet_flag for signal in self.pipeline.signals)

    @property
    def closing_odds_used(self) -> bool:
        return any(snapshot.kind is MarketSnapshotKind.CLOSING for snapshot in self.pipeline.snapshots)


def run_offline_compatibility(
    adapter: Top5LeagueAdapter,
    fixtures: Sequence[Fixture],
    snapshots: Sequence[MarketSnapshot],
    *,
    signal_time: SignalTimeContract,
    now: datetime,
    model_adapter: ModelAdapter | None = None,
    feature_adapter: FeatureAdapter | None = None,
    signal_decider: SignalDecider | None = None,
) -> OfflineCompatibilityResult:
    """Run the complete injected boundary with static dependencies only."""

    adapter.validate()
    signal_time.validate()
    fixture_tuple = tuple(fixtures)
    snapshot_tuple = tuple(snapshots)
    if any(fixture.league_code != adapter.league_code for fixture in fixture_tuple):
        raise ProductionContractError("offline harness received a foreign league fixture")
    if any(snapshot.fixture_key not in {fixture.fixture_key for fixture in fixture_tuple} for snapshot in snapshot_tuple):
        raise ProductionContractError("offline harness received a snapshot for an unknown fixture")
    test_model = model_adapter or DeterministicDummyModel()
    model_identity = getattr(test_model, "identity", "injected-offline-model")
    if not model_identity.strip():
        raise ProductionContractError("offline model identity is required")
    config = adapter.shadow_test_config(signal_time, test_model_adapter_id=model_identity)
    fixture_ingestor = StaticFixtureIngestor(fixture_tuple)
    odds_provider = StaticOddsProvider(snapshot_tuple)
    feature_boundary = feature_adapter or DeterministicFeatureAdapter()
    signal_boundary = signal_decider or NoBetShadowDecider(model_identity)
    sink = MemoryShadowArtifactSink()
    pipeline = run_shadow_pipeline(
        config,
        fixture_ingestor,
        odds_provider,
        feature_boundary,
        test_model,
        signal_boundary,
        now=now,
        sink=sink,
    )
    references = tuple(_artifact_reference(adapter, signal) for signal in pipeline.signals)
    artifact_flow = ShadowArtifactFlow(
        prediction_ids=tuple(prediction.prediction_id for prediction in pipeline.predictions),
        signal_ids=tuple(signal.signal_id for signal in pipeline.signals),
        signal_prediction_ids=tuple(signal.prediction_id for signal in pipeline.signals),
        artifact_references=references,
    )
    artifact_flow.validate()
    health = Top5ShadowHealth(
        league_code=adapter.league_code,
        fixture_count=pipeline.health.fixture_count,
        eligible_count=pipeline.health.prediction_count,
        prediction_count=pipeline.health.prediction_count,
        skipped_count=pipeline.health.skipped_count,
        stale_count=0,
        provider_failure_count=0,
        retry_count=0,
        duplicate_suppression_count=0,
        model_adapter_identity=model_identity,
        contract_id=signal_time_contract_id(signal_time),
        logical_fixture_evaluations=pipeline.health.fixture_count,
        bulk_provider_request_count=len(odds_provider.requests),
        fallback_request_count=0,
        health_identity=adapter.health_identity,
    )
    health.validate()
    return OfflineCompatibilityResult(pipeline, artifact_flow, health)


def _artifact_reference(
    adapter: Top5LeagueAdapter,
    signal: ShadowSignalArtifact,
) -> ShadowArtifactReference:
    suffix = f"{signal.signal_id.replace(':', '_')}.json"
    reference = ShadowArtifactReference(
        signal_id=signal.signal_id,
        shadow_archive_path=f"{TOP5_SHADOW_ARCHIVE_PREFIX}{adapter.league_code}/{suffix}",
        staged_public_path=f"{TOP5_STAGED_ARTIFACT_PREFIX}{adapter.league_code}/{suffix}",
    )
    reference.validate()
    return reference

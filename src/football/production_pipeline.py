"""Injected, shadow-only orchestration for future Top-5 football adapters.

The pipeline is deliberately not wired to a scheduler or a live provider.  A
caller must inject fixture, odds, feature, model, and decision adapters.  The
only optional sink is a caller-owned shadow sink; the module has no publisher
or ledger dependency.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from src.football.production_contracts import (
    ActivationMode,
    Fixture,
    FixtureIngestor,
    MarketSnapshot,
    MarketSnapshotKind,
    ModelAdapter,
    OddsProvider,
    OddsRequest,
    PredictionArtifact,
    PredictionInput,
    ProductionContractError,
    ShadowArtifactSink,
    ShadowSignalArtifact,
    SignalDecider,
    LeagueProductionConfig,
    FeatureAdapter,
    _utc,
)


@dataclass(frozen=True)
class ShadowRunHealth:
    """Health payload that a later adapter may hand to the existing health writer."""

    job: str
    league_code: str
    status: str
    activation_mode: ActivationMode
    provider_name: str
    provider_request_key: str
    fixture_count: int
    prediction_count: int
    artifact_count: int
    skipped_count: int
    errors: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        return {
            "job": self.job,
            "league_code": self.league_code,
            "status": self.status,
            "activation_mode": self.activation_mode.value,
            "provider": self.provider_name,
            "provider_request_key": self.provider_request_key,
            "fixture_count": self.fixture_count,
            "prediction_count": self.prediction_count,
            "artifact_count": self.artifact_count,
            "skipped_count": self.skipped_count,
            "errors": list(self.errors),
            "no_bet": True,
        }


@dataclass(frozen=True)
class ShadowPipelineResult:
    predictions: tuple[PredictionArtifact, ...]
    signals: tuple[ShadowSignalArtifact, ...]
    snapshots: tuple[MarketSnapshot, ...]
    health: ShadowRunHealth


def run_shadow_pipeline(
    config: LeagueProductionConfig,
    fixture_ingestor: FixtureIngestor,
    odds_provider: OddsProvider,
    feature_adapter: FeatureAdapter,
    model_adapter: ModelAdapter,
    signal_decider: SignalDecider,
    *,
    now: datetime,
    sink: ShadowArtifactSink | None = None,
) -> ShadowPipelineResult:
    """Run one injected league adapter in no-bet shadow mode.

    One bulk odds request is built for the whole fixture set.  Closing
    snapshots are rejected rather than silently downgraded to signal-time
    data.  Any sink is invoked only after the artifact has passed provenance
    and no-bet validation.
    """
    config.assert_shadow_ready()
    assert config.signal_time is not None
    assert config.provider_mapping is not None

    fixtures = tuple(fixture_ingestor.fetch(config))
    for fixture in fixtures:
        fixture.validate()
        if fixture.league_code != config.league_code:
            raise ProductionContractError("fixture ingestor returned a foreign league")

    request = OddsRequest.for_config(config, fixtures, now) if fixtures else None
    if request is None:
        health = ShadowRunHealth(
            job="top5_shadow",
            league_code=config.league_code,
            status="ok",
            activation_mode=ActivationMode.SHADOW,
            provider_name=odds_provider.name,
            provider_request_key="",
            fixture_count=0,
            prediction_count=0,
            artifact_count=0,
            skipped_count=0,
        )
        return ShadowPipelineResult((), (), (), health)

    if odds_provider.name != request.provider_name:
        raise ProductionContractError("injected odds provider differs from configured provider")
    snapshots = tuple(odds_provider.fetch(request))
    _validate_snapshots(snapshots)
    by_fixture = _index_signal_snapshots(snapshots)

    predictions: list[PredictionArtifact] = []
    signals: list[ShadowSignalArtifact] = []
    errors: list[str] = []
    skipped = 0
    for fixture in fixtures:
        snapshot = by_fixture.get(fixture.fixture_key)
        if snapshot is None:
            skipped += 1
            errors.append(f"no signal-time snapshot for {fixture.fixture_key}")
            continue
        features = feature_adapter.build(fixture, snapshot)
        model_input = PredictionInput.create(
            fixture,
            snapshot,
            features,
            config.signal_time,
            now,
        )
        probabilities = model_adapter.predict(model_input)
        prediction_id = _artifact_id(config.league_code, fixture, snapshot)
        prediction = PredictionArtifact(
            prediction_id=prediction_id,
            fixture_key=fixture.fixture_key,
            league_code=config.league_code,
            model_adapter_id=config.model_adapter_id,
            generated_at=_utc(now),
            snapshot_id=_snapshot_id(snapshot),
            snapshot_kind=MarketSnapshotKind.SIGNAL_TIME,
            probabilities=probabilities,
        )
        prediction.validate()
        signal = signal_decider.decide(fixture, probabilities, snapshot)
        if ActivationMode(signal.activation_mode) is not ActivationMode.SHADOW or not signal.no_bet_flag:
            raise ProductionContractError("shadow pipeline requires no-bet shadow output")
        if signal.fixture_key != fixture.fixture_key:
            raise ProductionContractError("signal decider returned a foreign fixture")
        if signal.league_code != config.league_code:
            raise ProductionContractError("signal decider returned a foreign league")
        if signal.model_adapter_id != config.model_adapter_id:
            raise ProductionContractError("signal decider returned a foreign model adapter")
        signal = replace(
            signal,
            fixture_key=fixture.fixture_key,
            league_code=config.league_code,
            model_adapter_id=config.model_adapter_id,
            prediction_id=prediction.prediction_id,
            snapshot_id=prediction.snapshot_id,
            snapshot_kind=MarketSnapshotKind.SIGNAL_TIME,
        )
        signal.validate()
        if sink is not None:
            sink.write(signal)
        predictions.append(prediction)
        signals.append(signal)

    status = "ok" if not errors else "degraded"
    health = ShadowRunHealth(
        job="top5_shadow",
        league_code=config.league_code,
        status=status,
        activation_mode=ActivationMode.SHADOW,
        provider_name=odds_provider.name,
        provider_request_key=request.request_key,
        fixture_count=len(fixtures),
        prediction_count=len(predictions),
        artifact_count=len(signals),
        skipped_count=skipped,
        errors=tuple(errors),
    )
    return ShadowPipelineResult(tuple(predictions), tuple(signals), snapshots, health)


def _validate_snapshots(snapshots: Sequence[MarketSnapshot]) -> None:
    for snapshot in snapshots:
        snapshot.validate()
        if snapshot.kind is MarketSnapshotKind.CLOSING:
            raise ProductionContractError("closing snapshot returned to prediction pipeline")


def _index_signal_snapshots(snapshots: Sequence[MarketSnapshot]) -> dict[str, MarketSnapshot]:
    selected: dict[str, MarketSnapshot] = {}
    for snapshot in snapshots:
        previous = selected.get(snapshot.fixture_key)
        if previous is None or _utc(snapshot.captured_at) > _utc(previous.captured_at):
            selected[snapshot.fixture_key] = snapshot
    return selected


def _snapshot_id(snapshot: MarketSnapshot) -> str:
    return snapshot.snapshot_id or f"{snapshot.source}:{snapshot.fixture_key}:{_utc(snapshot.captured_at).isoformat()}"


def _artifact_id(league_code: str, fixture: Fixture, snapshot: MarketSnapshot) -> str:
    return f"{league_code}:{fixture.fixture_key}:{_utc(snapshot.captured_at).isoformat()}"

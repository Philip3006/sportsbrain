"""Offline Top-5 shadow integration with immutable provenance and no side effects.

The runner uses the merged injected pipeline with static fixtures and odds.
It never imports a provider client, scheduler, publisher, Cloudflare binding,
or financial writer.  Public/staged paths are references only; no file is
written by this module.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    SignalTimeContract,
)
from src.football.production_pipeline import run_shadow_pipeline
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_dispatch import signal_time_contract_id
from src.football.top5_offline import (
    NoBetShadowDecider,
    StaticFixtureIngestor,
    StaticOddsProvider,
)
from src.football.top5_research_binding import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    TOP5_CANDIDATE_INVENTORY,
    Top5M5FeatureAdapter,
    Top5M5MarketModel,
    Top5ResearchBinding,
    build_reproducibility_manifest,
    inventory_for,
    shadow_paths,
    validate_shadow_contract,
)


def _stable_id(prefix: str, fields: Sequence[str]) -> str:
    encoded = json.dumps(tuple(fields), separators=(",", ":")).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()[:32]}"


def _snapshot_generation(snapshot: MarketSnapshot) -> str:
    snapshot.validate()
    return snapshot.snapshot_id or f"{snapshot.source}:{snapshot.captured_at.isoformat()}"


@dataclass(frozen=True)
class Top5ShadowPredictionArtifact:
    prediction_id: str
    binding: Top5ResearchBinding
    generated_at: datetime
    snapshot_id: str
    snapshot_kind: MarketSnapshotKind
    probabilities: Mapping[str, float]
    inference_latency_ms: int = 0

    def validate(self) -> None:
        self.binding.validate()
        if self.binding.fixture_key == "":
            raise ProductionContractError("prediction binding requires a fixture")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ProductionContractError("shadow prediction time must be timezone-aware")
        if not self.prediction_id.strip() or not self.snapshot_id.strip():
            raise ProductionContractError("shadow prediction requires stable identity")
        if self.snapshot_kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ProductionContractError("shadow prediction cannot use closing odds")
        if self.inference_latency_ms < 0:
            raise ProductionContractError("inference latency must be non-negative")
        expected = {"away", "draw", "home"}
        if set(self.probabilities) != expected:
            raise ProductionContractError("shadow prediction requires exactly three outcome probabilities")
        values = tuple(float(self.probabilities[name]) for name in ("away", "draw", "home"))
        if any(not isfinite(value) or not 0.0 <= value <= 1.0 for value in values):
            raise ProductionContractError("shadow prediction probability is invalid")
        if abs(sum(values) - 1.0) > 1e-9:
            raise ProductionContractError("shadow prediction probabilities must sum to one")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "prediction_id": self.prediction_id,
            "fixture_key": self.binding.fixture_key,
            "league": self.binding.league_code,
            "candidate_id": self.binding.candidate_id,
            "research_sha": self.binding.research_sha,
            "model_artifact_identity": self.binding.model_artifact_identity,
            "model_artifact_hash": self.binding.model_artifact_hash,
            "feature_schema_version": self.binding.feature_schema_version,
            "feature_schema_hash": self.binding.feature_schema_hash,
            "inference_implementation_version": self.binding.inference_implementation_version,
            "provider": self.binding.provider_mapping.provider_name,
            "sport_key": self.binding.provider_mapping.sport_key,
            "signal_time_contract_id": self.binding.signal_time_contract_id,
            "snapshot_generation": self.binding.snapshot_generation,
            "snapshot_id": self.snapshot_id,
            "snapshot_kind": self.snapshot_kind.value,
            "generated_at": self.generated_at.isoformat(),
            "probabilities": dict(self.probabilities),
            "inference_latency_ms": self.inference_latency_ms,
            "no_bet": self.binding.no_bet,
        }


@dataclass(frozen=True)
class Top5ShadowSignalArtifact:
    signal_id: str
    prediction_id: str
    binding: Top5ResearchBinding
    reason: str
    archive_path: str
    staged_public_path: str
    no_bet: bool = True
    registered: bool = False
    publication: bool = False

    def validate(self) -> None:
        self.binding.validate()
        if not self.signal_id.strip() or not self.prediction_id.strip() or not self.reason.strip():
            raise ProductionContractError("shadow signal requires provenance and reason")
        if self.prediction_id not in self.archive_path or self.prediction_id not in self.staged_public_path:
            raise ProductionContractError("shadow signal paths must identify the prediction")
        expected_archive, expected_staged = shadow_paths(self.binding, self.prediction_id)
        if self.archive_path != expected_archive or self.staged_public_path != expected_staged:
            raise ProductionContractError("shadow signal paths differ from canonical namespaces")
        if not self.no_bet:
            raise ProductionContractError("shadow signal must remain no-bet")
        if self.registered or self.publication:
            raise ProductionContractError("shadow signal cannot be registered or published")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "signal_id": self.signal_id,
            "prediction_id": self.prediction_id,
            "fixture_key": self.binding.fixture_key,
            "league": self.binding.league_code,
            "candidate_id": self.binding.candidate_id,
            "research_sha": self.binding.research_sha,
            "model_artifact_hash": self.binding.model_artifact_hash,
            "feature_schema_hash": self.binding.feature_schema_hash,
            "signal_time_contract_id": self.binding.signal_time_contract_id,
            "snapshot_generation": self.binding.snapshot_generation,
            "archive_path": self.archive_path,
            "staged_public_path": self.staged_public_path,
            "reason": self.reason,
            "no_bet": self.no_bet,
            "registered": self.registered,
            "publication": self.publication,
        }


@dataclass
class Top5ShadowArchive:
    """In-memory archive used by offline tests; it has no filesystem writer."""

    predictions: dict[str, Top5ShadowPredictionArtifact] = field(default_factory=dict)
    signals: dict[str, Top5ShadowSignalArtifact] = field(default_factory=dict)
    duplicate_suppressed: int = 0

    def append(
        self,
        prediction: Top5ShadowPredictionArtifact,
        signal: Top5ShadowSignalArtifact,
    ) -> bool:
        prediction.validate()
        signal.validate()
        if signal.prediction_id != prediction.prediction_id:
            raise ProductionContractError("shadow archive prediction and signal identities differ")
        old_prediction = self.predictions.get(prediction.prediction_id)
        old_signal = self.signals.get(signal.signal_id)
        if old_prediction is not None or old_signal is not None:
            if old_prediction == prediction and old_signal == signal:
                self.duplicate_suppressed += 1
                return False
            raise ProductionContractError("shadow archive identity collision")
        self.predictions[prediction.prediction_id] = prediction
        self.signals[signal.signal_id] = signal
        return True


@dataclass(frozen=True)
class Top5ShadowHealthRecord:
    league_code: str
    fixture_key: str
    candidate_id: str
    research_sha: str
    model_artifact_hash: str
    feature_schema_hash: str
    signal_time_contract_id: str
    snapshot_generation: str | None
    snapshot_age_seconds: float | None
    inference_status: str
    inference_latency_ms: int
    skip_reason: str | None = None
    stale_rejected: bool = False
    provider_failure: bool = False
    fallback_request_state: str = "not_used"
    duplicate_suppressed: bool = False
    no_bet: bool = True
    registered: bool = False
    publication: bool = False

    def validate(self) -> None:
        if any(not value.strip() for value in (
            self.league_code,
            self.fixture_key,
            self.candidate_id,
            self.research_sha,
            self.model_artifact_hash,
            self.feature_schema_hash,
            self.signal_time_contract_id,
            self.inference_status,
        )):
            raise ProductionContractError("shadow health is missing provenance")
        if self.research_sha != FROZEN_RESEARCH_SHA:
            raise ProductionContractError("shadow health references an unfrozen research SHA")
        inventory = inventory_for(self.league_code, self.candidate_id)
        if (
            self.model_artifact_hash != inventory.model_artifact_hash
            or self.feature_schema_hash != inventory.feature_schema_hash
        ):
            raise ProductionContractError("shadow health artifact identity differs from frozen inventory")
        if self.inference_latency_ms < 0:
            raise ProductionContractError("shadow health latency must be non-negative")
        if self.snapshot_age_seconds is not None and not isfinite(self.snapshot_age_seconds):
            raise ProductionContractError("shadow health snapshot age must be finite")
        if not self.no_bet or self.registered or self.publication:
            raise ProductionContractError("shadow health violates disabled no-bet state")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "league": self.league_code,
            "fixture_key": self.fixture_key,
            "candidate_id": self.candidate_id,
            "research_sha": self.research_sha,
            "model_artifact_hash": self.model_artifact_hash,
            "feature_schema_hash": self.feature_schema_hash,
            "signal_time_contract_id": self.signal_time_contract_id,
            "snapshot_generation": self.snapshot_generation,
            "snapshot_age_seconds": self.snapshot_age_seconds,
            "inference_status": self.inference_status,
            "inference_latency_ms": self.inference_latency_ms,
            "skip_reason": self.skip_reason,
            "stale_rejected": self.stale_rejected,
            "provider_failure": self.provider_failure,
            "fallback_request_state": self.fallback_request_state,
            "duplicate_suppressed": self.duplicate_suppressed,
            "no_bet": self.no_bet,
            "registered": self.registered,
            "publication": self.publication,
        }


@dataclass(frozen=True)
class Top5ShadowIntegrationResult:
    league_code: str
    candidate_ids: tuple[str, ...]
    predictions: tuple[Top5ShadowPredictionArtifact, ...]
    signals: tuple[Top5ShadowSignalArtifact, ...]
    health: tuple[Top5ShadowHealthRecord, ...]
    archive: Top5ShadowArchive
    manifest: Mapping[str, object]
    no_bet: bool = True
    publication: bool = False

    def validate(self) -> None:
        if self.league_code not in TOP5_LEAGUE_ADAPTERS:
            raise ProductionContractError("shadow result has an unknown league")
        if not self.candidate_ids or len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ProductionContractError("shadow result requires unique candidate identities")
        if not self.no_bet or self.publication:
            raise ProductionContractError("shadow result violates no-bet/publication boundary")
        for prediction in self.predictions:
            prediction.validate()
        for signal in self.signals:
            signal.validate()
        for record in self.health:
            record.validate()

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "league": self.league_code,
            "candidate_ids": list(self.candidate_ids),
            "predictions": [prediction.as_payload() for prediction in self.predictions],
            "signals": [signal.as_payload() for signal in self.signals],
            "health": [record.as_payload() for record in self.health],
            "manifest": dict(self.manifest),
            "no_bet": self.no_bet,
            "publication": self.publication,
        }


def _latest_snapshots(snapshots: Sequence[MarketSnapshot]) -> dict[str, MarketSnapshot]:
    selected: dict[str, MarketSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
            continue
        previous = selected.get(snapshot.fixture_key)
        if previous is None or snapshot.captured_at > previous.captured_at:
            selected[snapshot.fixture_key] = snapshot
    return selected


def run_offline_top5_shadow(
    league_code: str,
    fixtures: Sequence[Fixture],
    snapshots: Sequence[MarketSnapshot],
    *,
    signal_time: SignalTimeContract,
    now: datetime,
    integration_sha: str,
    candidate_ids: Sequence[str] = (M5_CANDIDATE_ID,),
) -> Top5ShadowIntegrationResult:
    """Run one league's reproducible candidate set on static input only."""

    if league_code not in TOP5_LEAGUE_ADAPTERS:
        raise ProductionContractError("offline shadow league is not one of the five registered leagues")
    validate_shadow_contract(signal_time, contract_id=signal_time_contract_id(signal_time))
    candidate_tuple = tuple(candidate_ids)
    if not candidate_tuple or len(set(candidate_tuple)) != len(candidate_tuple):
        raise ProductionContractError("offline shadow requires unique candidate IDs")
    inventories = []
    for candidate_id in candidate_tuple:
        inventory = next(
            (
                candidate for candidate in TOP5_CANDIDATE_INVENTORY[league_code]
                if candidate.candidate_id == candidate_id
            ),
            None,
        )
        if inventory is None:
            raise ProductionContractError("offline shadow candidate is absent from frozen inventory")
        inventory.validate()
        if inventory.readiness.value != "AVAILABLE_FOR_SHADOW":
            raise ProductionContractError("offline shadow candidate is unavailable")
        inventories.append(inventory)

    adapter = TOP5_LEAGUE_ADAPTERS[league_code]
    fixture_tuple = tuple(fixtures)
    snapshot_tuple = tuple(snapshots)
    archive = Top5ShadowArchive()
    all_predictions: list[Top5ShadowPredictionArtifact] = []
    all_signals: list[Top5ShadowSignalArtifact] = []
    all_health: list[Top5ShadowHealthRecord] = []
    all_bindings: list[Top5ResearchBinding] = []
    selected = _latest_snapshots(snapshot_tuple)
    contract_id = signal_time_contract_id(signal_time)

    for inventory in inventories:
        if inventory.candidate_id != M5_CANDIDATE_ID:
            raise ProductionContractError("no concrete shadow adapter exists for this candidate")
        config = adapter.shadow_test_config(signal_time, test_model_adapter_id=M5_CANDIDATE_ID)
        provider = StaticOddsProvider(snapshot_tuple)
        pipeline = run_shadow_pipeline(
            config,
            StaticFixtureIngestor(fixture_tuple),
            provider,
            Top5M5FeatureAdapter(),
            Top5M5MarketModel(),
            NoBetShadowDecider(M5_CANDIDATE_ID),
            now=now,
        )
        for pipeline_prediction in pipeline.predictions:
            snapshot = selected.get(pipeline_prediction.fixture_key)
            if snapshot is None:
                raise ProductionContractError("pipeline prediction lacks its signal snapshot")
            fixture = next(
                fixture for fixture in fixture_tuple
                if fixture.fixture_key == pipeline_prediction.fixture_key
            )
            binding = Top5ResearchBinding.for_snapshot(
                league_code,
                fixture,
                snapshot,
                contract_id,
            )
            prediction_id = _stable_id(
                "top5-prediction",
                (binding.identity, pipeline_prediction.snapshot_id),
            )
            prediction = Top5ShadowPredictionArtifact(
                prediction_id=prediction_id,
                binding=binding,
                generated_at=pipeline_prediction.generated_at,
                snapshot_id=pipeline_prediction.snapshot_id,
                snapshot_kind=MarketSnapshotKind.SIGNAL_TIME,
                probabilities=pipeline_prediction.probabilities,
            )
            archive_path, staged_path = shadow_paths(binding, prediction_id)
            signal = Top5ShadowSignalArtifact(
                signal_id=_stable_id("top5-signal", (prediction_id, M5_CANDIDATE_ID)),
                prediction_id=prediction_id,
                binding=binding,
                reason="offline_shadow_no_bet",
                archive_path=archive_path,
                staged_public_path=staged_path,
            )
            archive.append(prediction, signal)
            all_bindings.append(binding)
            all_predictions.append(prediction)
            all_signals.append(signal)

        for fixture in fixture_tuple:
            snapshot = selected.get(fixture.fixture_key)
            age = (now - snapshot.captured_at).total_seconds() if snapshot is not None else None
            matching = [
                prediction for prediction in all_predictions
                if prediction.binding.fixture_key == fixture.fixture_key
                and prediction.binding.candidate_id == inventory.candidate_id
            ]
            all_health.append(Top5ShadowHealthRecord(
                league_code=league_code,
                fixture_key=fixture.fixture_key,
                candidate_id=inventory.candidate_id,
                research_sha=FROZEN_RESEARCH_SHA,
                model_artifact_hash=inventory.model_artifact_hash or "",
                feature_schema_hash=inventory.feature_schema_hash,
                signal_time_contract_id=contract_id,
                snapshot_generation=_snapshot_generation(snapshot) if snapshot is not None else None,
                snapshot_age_seconds=age,
                inference_status="ok" if matching else "skipped",
                inference_latency_ms=matching[0].inference_latency_ms if matching else 0,
                skip_reason=None if matching else "no validated signal-time prediction",
                duplicate_suppressed=archive.duplicate_suppressed > 0,
            ))

    manifest = build_reproducibility_manifest(
        all_bindings,
        integration_sha=integration_sha,
        dependency_versions={"python": "3", "pipeline": "injected-static"},
    )
    result = Top5ShadowIntegrationResult(
        league_code=league_code,
        candidate_ids=candidate_tuple,
        predictions=tuple(all_predictions),
        signals=tuple(all_signals),
        health=tuple(all_health),
        archive=archive,
        manifest=manifest,
    )
    result.validate()
    return result

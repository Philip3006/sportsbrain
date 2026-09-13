"""Immutable bindings from the frozen Top-5 research package to shadow code.

This module is an integration inventory, not a model registry.  The only
candidate with a reproducible, inference-safe implementation in this branch
is the M5 market formula.  Historical outputs are recorded as evidence but
are never treated as deployable model artefacts.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from string import hexdigits
from types import MappingProxyType

from src.football.production_contracts import (
    TOP5_SHADOW_ARCHIVE_PREFIX,
    TOP5_STAGED_ARTIFACT_PREFIX,
    ActivationMode,
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    PredictionInput,
    ProductionContractError,
    ProviderMapping,
    SignalTimeContract,
    validate_artifact_ownership,
)
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS

FROZEN_RESEARCH_SHA = "6eaabbec7d0182103d815c72fae4976e261b40aa"
FROZEN_RESEARCH_NAMESPACE = "research/top5/"
INTEGRATION_CONTRACT_VERSION = "top5-shadow-binding-v1"
M5_CANDIDATE_ID = "M5_market_preclose"
M5_INFERENCE_IMPLEMENTATION = "top5-shadow-m5-market-v1"
M5_FEATURE_SCHEMA_VERSION = "top5-market-signal-v1"
M5_FEATURE_FIELDS = ("market_home", "market_draw", "market_away")


def _schema_hash(fields: Sequence[str]) -> str:
    payload = json.dumps(tuple(fields), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


M5_FEATURE_SCHEMA_HASH = _schema_hash(M5_FEATURE_FIELDS)


def _is_hex_digest(value: str, length: int) -> bool:
    return len(value) == length and all(character in hexdigits for character in value)


class CandidateReadiness(str, Enum):
    AVAILABLE_FOR_SHADOW = "AVAILABLE_FOR_SHADOW"
    NOT_REPRODUCIBLE = "NOT_REPRODUCIBLE"
    UNSUPPORTED_INPUT = "UNSUPPORTED_INPUT"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class FrozenArtifact:
    path: str
    git_blob_sha1: str | None
    role: str

    def validate(self) -> None:
        if not self.path.startswith(FROZEN_RESEARCH_NAMESPACE):
            raise ProductionContractError("frozen artefact is outside research namespace")
        if any(token in self.path.lower() for token in ("secret", "credential", "api_key", ".env")):
            raise ProductionContractError("frozen artefact path may not identify a secret")
        if not self.role.strip():
            raise ProductionContractError("frozen artefact role is required")
        if self.git_blob_sha1 is not None and not _is_hex_digest(self.git_blob_sha1, 40):
            raise ProductionContractError("frozen artefact hash must be a Git blob SHA-1")


@dataclass(frozen=True)
class CandidateInventory:
    league_code: str
    candidate_id: str
    readiness: CandidateReadiness
    model_definition: str
    required_inputs: tuple[str, ...]
    preprocessing: tuple[str, ...]
    calibration_logic: tuple[str, ...]
    market_dependencies: tuple[str, ...]
    historical_only_dependencies: tuple[str, ...]
    inference_safe_dependencies: tuple[str, ...]
    inference_entrypoint: str
    model_artifact_identity: str
    model_artifact_hash: str | None
    feature_schema_version: str
    feature_schema_hash: str
    training_provenance: str
    artefacts: tuple[FrozenArtifact, ...]
    reason: str
    production_enabled: bool = False

    def validate(self) -> None:
        if self.league_code not in TOP5_LEAGUE_ADAPTERS:
            raise ProductionContractError("candidate inventory has an unknown league")
        if not self.candidate_id.strip() or self.candidate_id not in {
            "M1_DC", "M2_Elo", "M3_LGBM_dmwd", "M4_LGBM",
            M5_CANDIDATE_ID, "M6_market_elo_blend", "M7_market_residual",
        }:
            raise ProductionContractError("candidate inventory has an unknown candidate")
        if not self.model_definition.strip() or not self.inference_entrypoint.strip():
            raise ProductionContractError("candidate inventory requires model definition and entrypoint")
        if not self.feature_schema_version.strip() or not _is_hex_digest(self.feature_schema_hash, 64):
            raise ProductionContractError("candidate inventory requires a feature schema identity")
        if not self.training_provenance.strip() or not self.reason.strip():
            raise ProductionContractError("candidate inventory requires provenance and readiness reason")
        if self.production_enabled:
            raise ProductionContractError("Top-5 candidates must remain disabled for production")
        for artefact in self.artefacts:
            artefact.validate()
        if self.readiness is CandidateReadiness.AVAILABLE_FOR_SHADOW:
            if self.model_artifact_hash is None or not _is_hex_digest(self.model_artifact_hash, 40):
                raise ProductionContractError("available candidate requires an exact artefact hash")
            if not self.inference_safe_dependencies:
                raise ProductionContractError("available candidate requires inference-safe dependencies")


def _artifact(path: str, sha1: str, role: str) -> FrozenArtifact:
    return FrozenArtifact(path, sha1, role)


_LEAGUE_ARTIFACTS: Mapping[str, Mapping[str, tuple[str, str]]] = MappingProxyType({
    "BL1": MappingProxyType({
        "dc": ("research/top5/leagues/bl1/results/dc_snapshots/dc_2425.pkl", "7b001566218584375ccb339e4a974ec75861b1d2"),
        "elo": ("research/top5/leagues/bl1/results/elo_series_dev.pkl", "1e499ebec98dcd4a07cc7026c3f4492a1c534c48"),
        "alpha": ("research/top5/leagues/bl1/results/m6_alpha_sweep.csv", "bb7d7730e3e3f3eba4736c3afc8f71e94f8beda0"),
        "m3": ("research/top5/leagues/bl1/results/oof_m3_dev_v2.csv", "4ea19351169be7d78c920ca217470ce53d3bd863"),
        "m4": ("research/top5/leagues/bl1/results/oof_m4_dev_v2.csv", "f5d7cf9294ad6cd359c18154dba68cfd717a7b20"),
        "m7": ("research/top5/leagues/bl1/results/oof_m7_dev_v3.csv", "761beae5c6b0c8dc39c10f6fe6369a69bd23936e"),
    }),
    "EPL": MappingProxyType({
        "dc": ("research/top5/leagues/epl/results/dc_snapshots/dc_2425.pkl", "d5e183fec8f9da0da4ec7b10efe3bd38aa8ca33b"),
        "elo": ("research/top5/leagues/epl/results/elo_series_dev.pkl", "c43335e7f5bacd2857751a734a7783a0baa70ce5"),
        "alpha": ("research/top5/leagues/epl/results/m6_alpha_sweep.csv", "b4003e75ca099847c7a3c2f4beb34658ccc7be2c"),
        "m3": ("research/top5/leagues/epl/results/oof_m3_dev_v2.csv", "560c84a1ce1a3e24cb1c772660d0cf820a4bc263"),
        "m4": ("research/top5/leagues/epl/results/oof_m4_dev_v2.csv", "ccd1b1d037298a156711d643265c14278922e2b0"),
        "m7": ("research/top5/leagues/epl/results/oof_m7_dev_v3.csv", "b56b8e509985c877c2e31ae3ab0eed44accc37e2"),
    }),
    "LL": MappingProxyType({
        "dc": ("research/top5/leagues/laliga/results/dc_snapshots/dc_2425.pkl", "1c9755706ab2df6371193b92f3ba299864f04384"),
        "elo": ("research/top5/leagues/laliga/results/elo_series_dev.pkl", "5106bae83c105c31df4989f62940abfa155b402d"),
        "alpha": ("research/top5/leagues/laliga/results/m6_alpha_sweep.csv", "ded877c39472d7ee2b3d7b000561edd843bf49eb"),
        "m3": ("research/top5/leagues/laliga/results/oof_m3_dev_v2.csv", "4b151af7ea9cdb1a7a578dd36fa418e4db58c58e"),
        "m4": ("research/top5/leagues/laliga/results/oof_m4_dev_v2.csv", "f312cdd64a22a8603a80e3197f686acd718958b0"),
        "m7": ("research/top5/leagues/laliga/results/oof_m7_dev_v3.csv", "a4618e12398f742b82349d539989becfb65bce29"),
    }),
    "SA": MappingProxyType({
        "dc": ("research/top5/leagues/seriea/results/dc_snapshots/dc_2425.pkl", "e98d6779a3810916fa904513fc764e7d70dd9658"),
        "elo": ("research/top5/leagues/seriea/results/elo_series_dev.pkl", "2c45a3b12dcd92b596c0caf6f92e5488fdc48bed"),
        "alpha": ("research/top5/leagues/seriea/results/m6_alpha_sweep.csv", "b20b9d8ea35a6eff778a30ebb39b55441da48641"),
        "m3": ("research/top5/leagues/seriea/results/oof_m3_dev_v2.csv", "0a73ae1de2c50aba58aacebb93f53784e9b66c6c"),
        "m4": ("research/top5/leagues/seriea/results/oof_m4_dev_v2.csv", "1cc92e44b1853e9f9459a45fe9a26be039f59e49"),
        "m7": ("research/top5/leagues/seriea/results/oof_m7_dev_v3.csv", "c78c7c7173064521407248535852d807f1a9bc51"),
    }),
    "L1": MappingProxyType({
        "dc": ("research/top5/leagues/ligue1/results/dc_snapshots/dc_2425.pkl", "eddbd601091716d41ca2353e239f8f4b129c47da"),
        "elo": ("research/top5/leagues/ligue1/results/elo_series_dev.pkl", "e8f713937787fde021f0eaead9175794708259c5"),
        "alpha": ("research/top5/leagues/ligue1/results/m6_alpha_sweep.csv", "0f1e0bf5c0b2c8e87db89a2a6245fb93f38de280"),
        "m3": ("research/top5/leagues/ligue1/results/oof_m3_dev_v2.csv", "a3c8213ada4e6b29537088d9b2308f979e35414d"),
        "m4": ("research/top5/leagues/ligue1/results/oof_m4_dev_v2.csv", "72ef740bdc229881e4a8c270c8ad4a4dd249fddc"),
        "m7": ("research/top5/leagues/ligue1/results/oof_m7_dev_v3.csv", "cc5cd9763ad234987d827698dd2d9449bd40b925"),
    }),
})

_MODEL_SOURCE_HASHES = MappingProxyType({
    "M1_DC": "cf8d50dfaeaba69a4d8954494c1bbbcbd77bf484",
    "M2_Elo": "e6cbc050816bcbf58f3b50e3a72e8af6a618d7c5",
    "M3_LGBM_dmwd": "26e33033130b65d6c35d9945e1b7f11282bfcdbb",
    "M4_LGBM": "26e33033130b65d6c35d9945e1b7f11282bfcdbb",
    M5_CANDIDATE_ID: "7d31e8857cdd48296a85484153756c718113883f",
    "M6_market_elo_blend": "ae28d1acad3b2c00ce72b2bc94754e0e4fd9b5df",
    "M7_market_residual": "26e33033130b65d6c35d9945e1b7f11282bfcdbb",
})


def _make_candidate(league_code: str, candidate_id: str) -> CandidateInventory:
    source_hash = _MODEL_SOURCE_HASHES[candidate_id]
    artifacts = _LEAGUE_ARTIFACTS[league_code]
    common = {
        "league_code": league_code,
        "candidate_id": candidate_id,
        "feature_schema_version": "none-for-shadow" if candidate_id != M5_CANDIDATE_ID else M5_FEATURE_SCHEMA_VERSION,
        "feature_schema_hash": "0" * 64 if candidate_id != M5_CANDIDATE_ID else M5_FEATURE_SCHEMA_HASH,
        "training_provenance": f"frozen research {FROZEN_RESEARCH_SHA}; DEV outer folds only",
        "production_enabled": False,
    }
    if candidate_id == "M1_DC":
        return CandidateInventory(
            **common,
            readiness=CandidateReadiness.UNSUPPORTED_INPUT,
            model_definition="Dixon-Coles walk-forward prediction from a season snapshot",
            required_inputs=("causal DC snapshot for the shadow season", "fixture teams", "kickoff"),
            preprocessing=("league-owned team identity", "chronological snapshot lookup"),
            calibration_logic=("frozen DEV phi selection",),
            market_dependencies=(),
            historical_only_dependencies=("dc_2425.pkl", "DEV research implementation"),
            inference_safe_dependencies=(),
            inference_entrypoint="research.top5.core.models_dc.run_m1",
            model_artifact_identity="dc_2425.pkl (historical snapshot; no 2627 snapshot)",
            model_artifact_hash=artifacts["dc"][1],
            artefacts=(_artifact(*artifacts["dc"], "historical DC snapshot"), _artifact("research/top5/core/models_dc.py", source_hash, "inference source")),
            reason="The frozen package has no inference-safe shadow-season DC snapshot; using 2425 would cross the sealed/future-state boundary.",
        )
    if candidate_id == "M2_Elo":
        return CandidateInventory(
            **common,
            readiness=CandidateReadiness.UNSUPPORTED_INPUT,
            model_definition="Standalone chronological Elo probability",
            required_inputs=("current causal Elo state", "fixture teams", "kickoff"),
            preprocessing=("league-owned team identity", "as-of Elo lookup"),
            calibration_logic=("frozen Elo constants",),
            market_dependencies=(),
            historical_only_dependencies=("elo_series_dev.pkl", "historical outcome rows"),
            inference_safe_dependencies=(),
            inference_entrypoint="research.top5.core.models_dc.run_m2",
            model_artifact_identity="elo_series_dev.pkl (historical series; no shadow state contract)",
            model_artifact_hash=artifacts["elo"][1],
            artefacts=(_artifact(*artifacts["elo"], "historical Elo series"), _artifact("research/top5/core/elo.py", source_hash, "inference source")),
            reason="The committed Elo series is historical and does not provide a sealed-safe current shadow state.",
        )
    if candidate_id in {"M3_LGBM_dmwd", "M4_LGBM", "M7_market_residual"}:
        output_key = {"M3_LGBM_dmwd": "m3", "M4_LGBM": "m4", "M7_market_residual": "m7"}[candidate_id]
        return CandidateInventory(
            **common,
            readiness=CandidateReadiness.NOT_REPRODUCIBLE,
            model_definition="Frozen LightGBM challenger/residual model",
            required_inputs=("trained LightGBM model", "causal feature vector", "fixture teams", "kickoff"),
            preprocessing=("frozen causal feature builder", "frozen market policy"),
            calibration_logic=("frozen outer-fold training only",),
            market_dependencies=("M7 requires canonical pre-close market probabilities",) if candidate_id == "M7_market_residual" else (),
            historical_only_dependencies=("OOF prediction CSV",),
            inference_safe_dependencies=(),
            inference_entrypoint="research.top5.core.models_lgbm.run_m3_m4/run_m7",
            model_artifact_identity="missing trained model artifact; OOF CSV is not a model",
            model_artifact_hash=None,
            artefacts=(_artifact(*artifacts[output_key], "OOF predictions only"), _artifact("research/top5/core/models_lgbm.py", source_hash, "training source")),
            reason="Only OOF predictions are committed; no trained model binary and no legitimate inference artifact is present.",
        )
    if candidate_id == "M6_market_elo_blend":
        return CandidateInventory(
            **common,
            readiness=CandidateReadiness.NOT_REPRODUCIBLE,
            model_definition="Chronological blend of M5 market probabilities and M2 Elo",
            required_inputs=("signal-time market odds", "current causal Elo state", "fixed inference alpha"),
            preprocessing=("canonical market probability formula", "as-of Elo lookup"),
            calibration_logic=("outer-fold alpha sweep is research evidence only",),
            market_dependencies=("canonical pre-close market source",),
            historical_only_dependencies=("elo_series_dev.pkl", "m6_alpha_sweep.csv"),
            inference_safe_dependencies=(),
            inference_entrypoint="research.top5.core.models.run_m6",
            model_artifact_identity="missing fixed shadow alpha; alpha sweep is not an inference configuration",
            model_artifact_hash=None,
            artefacts=(_artifact(*artifacts["elo"], "historical Elo series"), _artifact(*artifacts["alpha"], "alpha evidence, not model config"), _artifact("research/top5/core/models.py", source_hash, "inference source")),
            reason="No fixed, separately approved inference alpha and no shadow-state contract are committed.",
        )
    return CandidateInventory(
        **common,
        readiness=CandidateReadiness.AVAILABLE_FOR_SHADOW,
        model_definition="Canonical de-vigged Bookmaker average pre-close market formula",
        required_inputs=("signal-time market snapshot with home/draw/away odds", "fixture identity", "kickoff"),
        preprocessing=("validate odds > 1.0", "inverse-odds normalization"),
        calibration_logic=("none; deterministic formula",),
        market_dependencies=("explicit signal-time odds snapshot",),
        historical_only_dependencies=("research canonical market policy source",),
        inference_safe_dependencies=("signal-time snapshot only",),
        inference_entrypoint="src.football.top5_research_binding.Top5M5MarketModel.predict",
        model_artifact_identity="formula:research/top5/core/canonical_market.py:canonical_market_prob",
        model_artifact_hash=source_hash,
        artefacts=(_artifact("research/top5/core/canonical_market.py", source_hash, "formula source"),),
        reason="The formula requires no learned parameters and consumes only an explicitly supplied signal-time snapshot.",
    )


TOP5_CANDIDATE_INVENTORY: Mapping[str, tuple[CandidateInventory, ...]] = MappingProxyType({
    league: tuple(_make_candidate(league, candidate) for candidate in (
        "M1_DC", "M2_Elo", "M3_LGBM_dmwd", "M4_LGBM", M5_CANDIDATE_ID,
        "M6_market_elo_blend", "M7_market_residual",
    ))
    for league in TOP5_LEAGUE_ADAPTERS
})


def inventory_for(league_code: str, candidate_id: str) -> CandidateInventory:
    for candidate in TOP5_CANDIDATE_INVENTORY.get(league_code, ()):
        if candidate.candidate_id == candidate_id:
            candidate.validate()
            return candidate
    raise ProductionContractError("candidate is not registered in the frozen inventory")


def _mapping_for(league_code: str) -> ProviderMapping:
    config = TOP5_LEAGUE_ADAPTERS[league_code].config
    mapping = config.provider_mapping
    if mapping is None:
        raise ProductionContractError("Top-5 league has no provider mapping")
    return mapping


@dataclass(frozen=True)
class Top5ResearchBinding:
    research_sha: str
    league_code: str
    candidate_id: str
    model_artifact_identity: str
    model_artifact_hash: str
    feature_schema_version: str
    feature_schema_hash: str
    inference_implementation_version: str
    provider_mapping: ProviderMapping
    signal_time_contract_id: str
    snapshot_generation: str
    fixture_key: str
    no_bet: bool = True

    @classmethod
    def for_snapshot(
        cls,
        league_code: str,
        fixture: Fixture,
        snapshot: MarketSnapshot,
        signal_time_contract_id: str,
    ) -> Top5ResearchBinding:
        fixture.validate()
        snapshot.validate()
        if snapshot.fixture_key != fixture.fixture_key:
            raise ProductionContractError("binding fixture and snapshot identities differ")
        if snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ProductionContractError("binding cannot use closing odds")
        inventory = inventory_for(league_code, M5_CANDIDATE_ID)
        generation = snapshot.snapshot_id or f"{snapshot.source}:{snapshot.captured_at.isoformat()}"
        return cls(
            research_sha=FROZEN_RESEARCH_SHA,
            league_code=league_code,
            candidate_id=M5_CANDIDATE_ID,
            model_artifact_identity=inventory.model_artifact_identity,
            model_artifact_hash=inventory.model_artifact_hash or "",
            feature_schema_version=inventory.feature_schema_version,
            feature_schema_hash=inventory.feature_schema_hash,
            inference_implementation_version=M5_INFERENCE_IMPLEMENTATION,
            provider_mapping=_mapping_for(league_code),
            signal_time_contract_id=signal_time_contract_id,
            snapshot_generation=generation,
            fixture_key=fixture.fixture_key,
        )

    def validate(self) -> None:
        if self.research_sha != FROZEN_RESEARCH_SHA:
            raise ProductionContractError("shadow binding references an unfrozen research SHA")
        inventory = inventory_for(self.league_code, self.candidate_id)
        if inventory.readiness is not CandidateReadiness.AVAILABLE_FOR_SHADOW:
            raise ProductionContractError("shadow binding references an unavailable candidate")
        if self.model_artifact_identity != inventory.model_artifact_identity:
            raise ProductionContractError("shadow binding model identity differs from frozen inventory")
        if self.model_artifact_hash != inventory.model_artifact_hash:
            raise ProductionContractError("shadow binding model hash differs from frozen inventory")
        if self.feature_schema_version != inventory.feature_schema_version or self.feature_schema_hash != inventory.feature_schema_hash:
            raise ProductionContractError("shadow binding feature schema differs from frozen inventory")
        if not self.inference_implementation_version.strip() or not self.signal_time_contract_id.strip():
            raise ProductionContractError("shadow binding requires implementation and contract identity")
        if not self.snapshot_generation.strip() or not self.fixture_key.strip():
            raise ProductionContractError("shadow binding requires fixture and snapshot identity")
        self.provider_mapping.validate(expected_sport_key=TOP5_LEAGUE_ADAPTERS[self.league_code].config.provider_sport_key)
        if not self.no_bet:
            raise ProductionContractError("Top-5 shadow binding must remain no-bet")
        if TOP5_LEAGUE_ADAPTERS[self.league_code].config.activation_mode is not ActivationMode.DISABLED:
            raise ProductionContractError("Top-5 production registry must remain disabled")

    @property
    def identity(self) -> str:
        self.validate()
        fields = (
            self.research_sha,
            self.league_code,
            self.candidate_id,
            self.model_artifact_hash,
            self.feature_schema_hash,
            self.inference_implementation_version,
            self.signal_time_contract_id,
            self.snapshot_generation,
            self.fixture_key,
        )
        return "top5-shadow-binding:" + hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()[:32]


class Top5M5FeatureAdapter:
    """Feature adapter for M5; it reads only the current signal snapshot."""

    def build(self, fixture: Fixture, snapshot: MarketSnapshot) -> Mapping[str, float]:
        fixture.validate()
        snapshot.validate()
        if snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ProductionContractError("M5 shadow features cannot consume closing odds")
        if snapshot.fixture_key != fixture.fixture_key:
            raise ProductionContractError("M5 shadow features received a foreign fixture")
        required = {"home", "draw", "away"}
        if not required.issubset(snapshot.odds):
            raise ProductionContractError("M5 shadow features require home, draw, and away odds")
        values = {f"market_{name}": float(snapshot.odds[name]) for name in required}
        if any(not isfinite(value) or value <= 1.0 for value in values.values()):
            raise ProductionContractError("M5 shadow features require finite odds greater than 1.0")
        return {name: values[name] for name in M5_FEATURE_FIELDS}


class Top5M5MarketModel:
    """Deterministic M5 formula, explicitly limited to shadow inference."""

    identity = M5_CANDIDATE_ID

    def predict(self, model_input: PredictionInput) -> Mapping[str, float]:
        model_input.fixture.validate()
        model_input.signal_snapshot.validate()
        if model_input.signal_snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ProductionContractError("M5 shadow model cannot consume closing odds")
        if set(model_input.features) != set(M5_FEATURE_FIELDS):
            raise ProductionContractError("M5 shadow model received an incompatible feature schema")
        home = float(model_input.features["market_home"])
        draw = float(model_input.features["market_draw"])
        away = float(model_input.features["market_away"])
        if any(not isfinite(value) or value <= 1.0 for value in (home, draw, away)):
            raise ProductionContractError("M5 shadow model requires valid odds")
        inv_home, inv_draw, inv_away = 1.0 / home, 1.0 / draw, 1.0 / away
        total = inv_home + inv_draw + inv_away
        return {
            "away": inv_away / total,
            "draw": inv_draw / total,
            "home": inv_home / total,
        }


def shadow_paths(binding: Top5ResearchBinding, prediction_id: str) -> tuple[str, str]:
    binding.validate()
    if not prediction_id.strip() or "/" in prediction_id:
        raise ProductionContractError("prediction identity is not path-safe")
    archive = f"{TOP5_SHADOW_ARCHIVE_PREFIX}{binding.league_code}/{binding.candidate_id}/{prediction_id}.json"
    staged = f"{TOP5_STAGED_ARTIFACT_PREFIX}{binding.league_code}/{binding.candidate_id}/{prediction_id}.json"
    validate_artifact_ownership(archive)
    validate_artifact_ownership(staged)
    return archive, staged


def build_reproducibility_manifest(
    bindings: Sequence[Top5ResearchBinding],
    *,
    integration_sha: str,
    dependency_versions: Mapping[str, str] | None = None,
) -> dict[str, object]:
    if not _is_hex_digest(integration_sha, 40):
        raise ProductionContractError("manifest requires an exact integration SHA")
    for binding in bindings:
        binding.validate()
    ordered = tuple(sorted(bindings, key=lambda item: (item.league_code, item.candidate_id, item.fixture_key)))
    return {
        "contract_version": INTEGRATION_CONTRACT_VERSION,
        "frozen_research_sha": FROZEN_RESEARCH_SHA,
        "integration_sha": integration_sha,
        "candidates": [
            {
                "league": binding.league_code,
                "candidate_id": binding.candidate_id,
                "model_artifact_identity": binding.model_artifact_identity,
                "model_artifact_hash": binding.model_artifact_hash,
                "feature_schema_version": binding.feature_schema_version,
                "feature_schema_hash": binding.feature_schema_hash,
                "inference_implementation_version": binding.inference_implementation_version,
                "provider": binding.provider_mapping.provider_name,
                "sport_key": binding.provider_mapping.sport_key,
                "signal_time_contract_id": binding.signal_time_contract_id,
                "snapshot_generation": binding.snapshot_generation,
                "fixture_key": binding.fixture_key,
                "no_bet": binding.no_bet,
            }
            for binding in ordered
        ],
        "dependency_versions": dict(sorted((dependency_versions or {}).items())),
    }


def validate_shadow_contract(signal_time: SignalTimeContract, *, contract_id: str) -> None:
    signal_time.validate()
    if not contract_id.strip() or contract_id.lower() in {"latest", "current", "best"}:
        raise ProductionContractError("shadow signal-time contract requires an exact identity")

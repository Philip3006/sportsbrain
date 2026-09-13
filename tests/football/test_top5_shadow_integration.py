from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    PredictionInput,
    ProductionContractError,
    SignalTimeContract,
)
from src.football.top5_dispatch import signal_time_contract_id
from src.football.top5_research_binding import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    M5_FEATURE_FIELDS,
    M5_FEATURE_SCHEMA_HASH,
    TOP5_CANDIDATE_INVENTORY,
    CandidateReadiness,
    Top5M5FeatureAdapter,
    Top5M5MarketModel,
    Top5ResearchBinding,
    build_reproducibility_manifest,
    inventory_for,
    shadow_paths,
)
from src.football.top5_shadow_integration import (
    Top5ShadowArchive,
    Top5ShadowHealthRecord,
    Top5ShadowPredictionArtifact,
    Top5ShadowSignalArtifact,
    run_offline_top5_shadow,
)

BASE = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
CONTRACT = SignalTimeContract(60, 180, 120)
INTEGRATION_SHA = "0123456789abcdef" * 2 + "01234567"


def _fixture(code: str = "BL1", key: str = "fixture-1") -> Fixture:
    return Fixture(key, code, "Home FC", "Away FC", BASE + timedelta(minutes=120))


def _snapshot(
    key: str = "fixture-1",
    *,
    captured_at: datetime = BASE,
    kind: MarketSnapshotKind = MarketSnapshotKind.SIGNAL_TIME,
    odds: dict[str, float] | None = None,
    snapshot_id: str = "snapshot-1",
) -> MarketSnapshot:
    return MarketSnapshot(
        fixture_key=key,
        captured_at=captured_at,
        kind=kind,
        source="offline-static-provider",
        odds=odds or {"home": 2.0, "draw": 3.5, "away": 4.0},
        snapshot_id=snapshot_id,
    )


def test_inventory_has_all_seven_candidates_per_league_and_only_m5_is_available():
    assert set(TOP5_CANDIDATE_INVENTORY) == {"BL1", "EPL", "LL", "SA", "L1"}
    for candidates in TOP5_CANDIDATE_INVENTORY.values():
        assert len(candidates) == 7
        assert sum(candidate.readiness is CandidateReadiness.AVAILABLE_FOR_SHADOW for candidate in candidates) == 1
        assert inventory_for(candidates[0].league_code, M5_CANDIDATE_ID).readiness is CandidateReadiness.AVAILABLE_FOR_SHADOW


def test_m5_formula_uses_exact_signal_time_feature_schema():
    fixture = _fixture()
    snapshot = _snapshot()
    features = Top5M5FeatureAdapter().build(fixture, snapshot)
    assert tuple(features) == M5_FEATURE_FIELDS
    assert M5_FEATURE_SCHEMA_HASH
    result = Top5M5MarketModel().predict(
        PredictionInput.create(
            fixture, snapshot, features, CONTRACT, BASE,
        )
    )
    assert set(result) == {"away", "draw", "home"}
    assert sum(result.values()) == pytest.approx(1.0)


@pytest.mark.parametrize("league_code", ["BL1", "EPL", "LL", "SA", "L1"])
def test_offline_end_to_end_runs_each_league_with_no_bet_and_full_provenance(league_code):
    result = run_offline_top5_shadow(
        league_code,
        [_fixture(league_code, f"{league_code.lower()}-fixture")],
        [_snapshot(f"{league_code.lower()}-fixture", snapshot_id=f"{league_code.lower()}-snapshot")],
        signal_time=CONTRACT,
        now=BASE,
        integration_sha=INTEGRATION_SHA,
    )
    result.validate()
    assert result.candidate_ids == (M5_CANDIDATE_ID,)
    assert len(result.predictions) == len(result.signals) == len(result.health) == 1
    prediction = result.predictions[0]
    signal = result.signals[0]
    health = result.health[0]
    assert prediction.binding.research_sha == FROZEN_RESEARCH_SHA
    assert prediction.binding.league_code == league_code
    assert prediction.binding.model_artifact_hash
    assert prediction.binding.feature_schema_hash == M5_FEATURE_SCHEMA_HASH
    assert prediction.binding.signal_time_contract_id == signal_time_contract_id(CONTRACT)
    assert prediction.snapshot_kind is MarketSnapshotKind.SIGNAL_TIME
    assert signal.no_bet is True
    assert signal.registered is False
    assert signal.publication is False
    assert health.inference_status == "ok"
    assert health.no_bet is True
    assert health.registered is False
    assert health.publication is False
    assert result.manifest["frozen_research_sha"] == FROZEN_RESEARCH_SHA
    assert result.manifest["integration_sha"] == INTEGRATION_SHA


def test_offline_shadow_rejects_unavailable_candidate_without_fallback():
    with pytest.raises(ProductionContractError, match="unavailable"):
        run_offline_top5_shadow(
            "BL1",
            [_fixture()],
            [_snapshot()],
            signal_time=CONTRACT,
            now=BASE,
            integration_sha=INTEGRATION_SHA,
            candidate_ids=("M3_LGBM_dmwd",),
        )


def test_missing_signal_snapshot_is_a_deterministic_skip_and_not_a_fabricated_signal():
    result = run_offline_top5_shadow(
        "BL1",
        [_fixture()],
        [],
        signal_time=CONTRACT,
        now=BASE,
        integration_sha=INTEGRATION_SHA,
    )
    assert result.predictions == ()
    assert result.signals == ()
    assert result.health[0].inference_status == "skipped"
    assert result.health[0].skip_reason == "no validated signal-time prediction"


@pytest.mark.parametrize(
    "kind, match",
    [
        (MarketSnapshotKind.CLOSING, "closing"),
    ],
)
def test_closing_snapshot_is_rejected_at_the_feature_boundary(kind, match):
    with pytest.raises(ProductionContractError, match=match):
        Top5M5FeatureAdapter().build(_fixture(), _snapshot(kind=kind))


def test_m5_feature_boundary_rejects_missing_or_invalid_odds():
    with pytest.raises(ProductionContractError, match="require home, draw, and away"):
        Top5M5FeatureAdapter().build(_fixture(), _snapshot(odds={"home": 2.0, "away": 4.0}))
    with pytest.raises(ProductionContractError, match="invalid odds"):
        Top5M5FeatureAdapter().build(
            _fixture(), _snapshot(odds={"home": 2.0, "draw": 1.0, "away": 4.0})
        )


def test_binding_rejects_wrong_hashes_and_foreign_snapshot_identity():
    binding = Top5ResearchBinding.for_snapshot("BL1", _fixture(), _snapshot(), signal_time_contract_id(CONTRACT))
    with pytest.raises(ProductionContractError, match="model hash"):
        replace(binding, model_artifact_hash="a" * 40).validate()
    with pytest.raises(ProductionContractError, match="unfrozen"):
        replace(binding, research_sha="b" * 40).validate()
    with pytest.raises(ProductionContractError, match="identities differ"):
        Top5ResearchBinding.for_snapshot(
            "BL1", _fixture(), _snapshot(key="foreign"), signal_time_contract_id(CONTRACT)
        )


def test_naive_timestamps_and_stale_signal_time_fail_closed():
    with pytest.raises(ProductionContractError, match="timezone-aware"):
        Fixture("naive", "BL1", "Home FC", "Away FC", BASE.replace(tzinfo=None))
    with pytest.raises(ProductionContractError, match="timezone-aware"):
        _snapshot(captured_at=BASE.replace(tzinfo=None))
    with pytest.raises(ProductionContractError, match="timezone-aware"):
        run_offline_top5_shadow(
            "BL1", [_fixture()], [_snapshot()], signal_time=CONTRACT,
            now=BASE.replace(tzinfo=None), integration_sha=INTEGRATION_SHA,
        )
    with pytest.raises(ProductionContractError, match="signal-time contract"):
        run_offline_top5_shadow(
            "BL1", [_fixture()], [_snapshot(captured_at=BASE - timedelta(minutes=3))],
            signal_time=CONTRACT, now=BASE, integration_sha=INTEGRATION_SHA,
        )


def test_aware_offsets_have_identical_signal_time_decisions():
    offset = timezone(timedelta(hours=2))
    assert CONTRACT.accepts(BASE + timedelta(minutes=120), BASE, BASE) is True
    assert CONTRACT.accepts(
        (BASE + timedelta(minutes=120)).astimezone(offset),
        BASE.astimezone(offset),
        BASE.astimezone(offset),
    ) is True


def test_model_rejects_future_or_rolling_feature_bypass():
    fixture = _fixture()
    snapshot = _snapshot()
    features = {**Top5M5FeatureAdapter().build(fixture, snapshot), "future_result": 1.0}
    with pytest.raises(ProductionContractError, match="incompatible feature schema"):
        Top5M5MarketModel().predict(PredictionInput.create(fixture, snapshot, features, CONTRACT, BASE))


def test_prediction_artifact_rejects_nan_bad_sum_and_closing_probabilities():
    binding = Top5ResearchBinding.for_snapshot("BL1", _fixture(), _snapshot(), signal_time_contract_id(CONTRACT))
    base = {
        "prediction_id": "prediction-id",
        "binding": binding,
        "generated_at": BASE,
        "snapshot_id": "snapshot-1",
        "snapshot_kind": MarketSnapshotKind.SIGNAL_TIME,
    }
    with pytest.raises(ProductionContractError, match="invalid"):
        Top5ShadowPredictionArtifact(**base, probabilities={"away": float("nan"), "draw": 0.2, "home": 0.8}).validate()
    with pytest.raises(ProductionContractError, match="sum"):
        Top5ShadowPredictionArtifact(**base, probabilities={"away": 0.2, "draw": 0.2, "home": 0.2}).validate()
    with pytest.raises(ProductionContractError, match="closing"):
        closing_base = {**base, "snapshot_kind": MarketSnapshotKind.CLOSING}
        Top5ShadowPredictionArtifact(
            **closing_base,
            probabilities={"away": 0.2, "draw": 0.3, "home": 0.5},
        ).validate()


def test_archive_suppresses_identical_duplicate_and_rejects_identity_collision():
    fixture = _fixture()
    snapshot = _snapshot()
    binding = Top5ResearchBinding.for_snapshot("BL1", fixture, snapshot, signal_time_contract_id(CONTRACT))
    prediction = Top5ShadowPredictionArtifact(
        prediction_id="prediction-id",
        binding=binding,
        generated_at=BASE,
        snapshot_id="snapshot-1",
        snapshot_kind=MarketSnapshotKind.SIGNAL_TIME,
        probabilities={"away": 0.2, "draw": 0.3, "home": 0.5},
    )
    archive_path, staged_path = shadow_paths(binding, prediction.prediction_id)
    signal = Top5ShadowSignalArtifact(
        signal_id="signal-id",
        prediction_id=prediction.prediction_id,
        binding=binding,
        reason="offline_shadow_no_bet",
        archive_path=archive_path,
        staged_public_path=staged_path,
    )
    archive = Top5ShadowArchive()
    assert archive.append(prediction, signal) is True
    assert archive.append(prediction, signal) is False
    assert archive.duplicate_suppressed == 1
    collision = Top5ShadowSignalArtifact(
        signal_id="different-signal",
        prediction_id=prediction.prediction_id,
        binding=binding,
        reason="different",
        archive_path=archive_path,
        staged_public_path=staged_path,
    )
    with pytest.raises(ProductionContractError, match="collision"):
        archive.append(prediction, collision)

    invalid_path = replace(signal, archive_path=f"results/not-top5/{prediction.prediction_id}.json")
    with pytest.raises(ProductionContractError, match="canonical namespaces"):
        invalid_path.validate()


def test_changed_snapshot_generation_and_league_identity_produce_distinct_bindings():
    first = Top5ResearchBinding.for_snapshot("BL1", _fixture("BL1"), _snapshot(), signal_time_contract_id(CONTRACT))
    changed = Top5ResearchBinding.for_snapshot(
        "BL1", _fixture("BL1"), _snapshot(snapshot_id="snapshot-2"), signal_time_contract_id(CONTRACT)
    )
    other_league = Top5ResearchBinding.for_snapshot(
        "EPL", _fixture("EPL", "fixture-1"), _snapshot(), signal_time_contract_id(CONTRACT)
    )
    assert first.identity != changed.identity
    assert first.identity != other_league.identity


def test_manifest_is_deterministic_and_rejects_ambiguous_integration_identity():
    first = Top5ResearchBinding.for_snapshot("BL1", _fixture(), _snapshot(), signal_time_contract_id(CONTRACT))
    second = Top5ResearchBinding.for_snapshot("EPL", _fixture("EPL"), _snapshot(), signal_time_contract_id(CONTRACT))
    manifest_a = build_reproducibility_manifest([first, second], integration_sha=INTEGRATION_SHA)
    manifest_b = build_reproducibility_manifest([second, first], integration_sha=INTEGRATION_SHA)
    assert manifest_a == manifest_b
    for value in ("latest", "current", "best"):
        with pytest.raises(ProductionContractError, match="exact integration SHA"):
            build_reproducibility_manifest([first], integration_sha=value)
    for value in ("a" * 39, "g" * 40):
        with pytest.raises(ProductionContractError, match="exact integration SHA"):
            build_reproducibility_manifest([first], integration_sha=value)


def test_health_record_rejects_unfrozen_research_and_enabled_publication():
    inventory = inventory_for("BL1", M5_CANDIDATE_ID)
    record = Top5ShadowHealthRecord(
        league_code="BL1",
        fixture_key="fixture-1",
        candidate_id=M5_CANDIDATE_ID,
        research_sha=FROZEN_RESEARCH_SHA,
        model_artifact_hash=inventory.model_artifact_hash or "",
        feature_schema_hash=inventory.feature_schema_hash,
        signal_time_contract_id=signal_time_contract_id(CONTRACT),
        snapshot_generation="snapshot-1",
        snapshot_age_seconds=0.0,
        inference_status="ok",
        inference_latency_ms=0,
    )
    record.validate()
    with pytest.raises(ProductionContractError, match="unfrozen"):
        Top5ShadowHealthRecord(**{**record.__dict__, "research_sha": "b" * 40}).validate()
    with pytest.raises(ProductionContractError, match="disabled"):
        Top5ShadowHealthRecord(**{**record.__dict__, "publication": True}).validate()

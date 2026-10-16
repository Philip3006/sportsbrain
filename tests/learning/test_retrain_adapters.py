from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.learning import (
    Bundesliga2RetrainAdapter,
    CausalTrainingRowV1,
    GenericFootballRetrainAdapter,
    LifecycleError,
    NationsLeagueRetrainAdapter,
    RetrainDecision,
    TennisLGBMRetrainAdapter,
    Top5RetrainAdapter,
    canonical_digest,
    feature_schema_digest,
    training_data_digest,
)


def _row(index: int = 1, *, feature_name: str = "elo_diff") -> CausalTrainingRowV1:
    features = {feature_name: float(index)}
    result_id = canonical_digest({"result": index})
    identity = {
        "schema": "sportsbrain-causal-training-row-v1",
        "fixture_id": f"fixture-{index}",
        "sport": "football",
        "competition": "UEFA Nations League",
        "training_cutoff": "2026-10-02T00:00:00Z",
        "feature_available_at": "2026-10-01T18:00:00Z",
        "event_completed_at": "2026-10-01T20:00:00Z",
        "result_safe_available_at": "2026-10-01T22:00:00Z",
        "feature_digest": canonical_digest(features),
        "result_id": result_id,
    }
    row_id = canonical_digest(identity)
    payload = {
        **identity,
        "features": features,
        "label": {"outcome": "HOME" if index % 2 else "DRAW"},
        "row_id": row_id,
    }
    return CausalTrainingRowV1(
        row_id=row_id,
        fixture_id=f"fixture-{index}",
        sport="football",
        competition="UEFA Nations League",
        training_cutoff="2026-10-02T00:00:00Z",
        feature_available_at="2026-10-01T18:00:00Z",
        event_completed_at="2026-10-01T20:00:00Z",
        result_safe_available_at="2026-10-01T22:00:00Z",
        features=features,
        label=payload["label"],
        result_id=result_id,
        feature_digest=identity["feature_digest"],
        row_digest=canonical_digest(payload),
    )


def _plan(adapter, rows: list[CausalTrainingRowV1]):
    return adapter.plan([], rows, current_release_id="active-release-v1")


def test_new_result_safe_row_requires_retrain_and_duplicate_is_noop():
    adapter = GenericFootballRetrainAdapter()
    row = _row()
    plan = _plan(adapter, [row])
    assert plan.decision is RetrainDecision.RETRAIN_REQUIRED
    assert plan.newly_result_safe_count == 1
    assert plan.causal_training_row_count == 1
    assert plan.new_result_count == 1
    assert plan.lgbm_included is True
    duplicate = adapter.plan([row], [row], current_release_id="active-release-v1")
    assert duplicate.decision is RetrainDecision.NO_OP
    assert duplicate.newly_result_safe_count == 0


def test_training_digest_and_feature_schema_digest_are_order_independent():
    rows = [_row(1), _row(2)]
    assert training_data_digest(rows) == training_data_digest(list(reversed(rows)))
    assert feature_schema_digest(rows) == feature_schema_digest(list(reversed(rows)))


def test_feature_leakage_is_rejected():
    with pytest.raises(LifecycleError, match="result-leakage"):
        _plan(TennisLGBMRetrainAdapter(), [_row(feature_name="home_score")])


def test_family_coverage_keeps_lgbm_in_the_decision_set():
    tennis = _plan(TennisLGBMRetrainAdapter(), [_row()])
    bl2 = _plan(Bundesliga2RetrainAdapter(), [_row()])
    generic = _plan(GenericFootballRetrainAdapter(), [_row()])
    assert tennis.lgbm_included is True and tennis.lgbm_active is True
    assert bl2.lgbm_included is True and bl2.lgbm_active is False
    assert generic.lgbm_included is True and generic.lgbm_active is True
    assert bl2.component_status["bundesliga2_lgbm"] == "RETRAIN_REQUIRED"


def test_generic_stacker_cadence_is_explicit_but_lgbm_is_immediate():
    adapter = GenericFootballRetrainAdapter()
    one = _plan(adapter, [_row(1)])
    three = _plan(adapter, [_row(1), _row(2), _row(3)])
    assert "lgbm" in one.affected_components
    assert "stacker" not in one.affected_components
    assert one.component_status["stacker"] == "DEFERRED_EVIDENCE_CADENCE"
    assert "stacker" in three.affected_components
    assert three.component_status["stacker"] == "RETRAIN_REQUIRED"


def test_nations_league_tree_candidate_is_not_active():
    adapter = NationsLeagueRetrainAdapter()
    plan = _plan(adapter, [_row()])
    assert plan.decision is RetrainDecision.RETRAIN_REQUIRED
    assert plan.lgbm_included is True
    assert plan.lgbm_active is False
    assert "nations_league_tree_candidate" in plan.affected_components


def test_top5_retraining_is_governance_blocked():
    plan = _plan(Top5RetrainAdapter(), [_row()])
    assert plan.decision is RetrainDecision.RETRAIN_BLOCKED_BY_GOVERNANCE
    assert plan.candidate_artifact_identities == ()
    assert "not approved" in plan.reason


def test_failed_candidate_is_incomplete_and_active_artifact_is_untouched(
    tmp_path: Path,
):
    active = tmp_path / "active.pkl"
    active.write_bytes(b"ACTIVE-BEFORE")
    stage = tmp_path / "staging"
    adapter = TennisLGBMRetrainAdapter()
    row = _row()
    plan = _plan(adapter, [row])

    def trainer(_rows, _output):
        raise RuntimeError("synthetic LGBM failure")

    receipt = adapter.execute(
        plan,
        [row],
        trainer=trainer,
        validator=lambda *_args: {"passed": True},
        staging_root=stage,
        execute=True,
    )
    assert receipt.status == "INCOMPLETE"
    assert receipt.active_untouched is True
    assert "synthetic LGBM failure" in (receipt.failure_reason or "")
    assert active.read_bytes() == b"ACTIVE-BEFORE"


def test_injected_execution_stages_candidate_and_returns_validation_receipt(
    tmp_path: Path,
):
    active = tmp_path / "active.pkl"
    active.write_bytes(b"ACTIVE-BEFORE")
    adapter = NationsLeagueRetrainAdapter()
    row = _row()
    plan = _plan(adapter, [row])

    def trainer(rows, output_dir):
        assert tuple(rows) == (row,)
        (output_dir / "candidate.bin").write_bytes(b"candidate")

    receipt = adapter.execute(
        plan,
        [row],
        trainer=trainer,
        validator=lambda rows, _path: {
            "passed": True,
            "validation_rows": len(rows),
            "brier": 0.2,
            "log_loss": 0.6,
            "calibration_ece": 0.03,
        },
        staging_root=tmp_path / "staging",
        execute=True,
    )
    assert receipt.status == "COMPLETE"
    assert receipt.candidate_artifact_digest
    assert receipt.validation["training_rows"] == 1
    assert receipt.validation["feature_schema_digest"] == plan.feature_schema_digest
    assert receipt.active_untouched is True
    assert active.read_bytes() == b"ACTIVE-BEFORE"


def test_plan_only_never_invokes_injected_trainer(tmp_path: Path):
    calls: list[str] = []
    adapter = TennisLGBMRetrainAdapter()
    row = _row()
    plan = _plan(adapter, [row])

    def trainer(_rows, _output):
        calls.append("trainer")

    receipt = adapter.execute(
        plan,
        [row],
        trainer=trainer,
        validator=lambda *_args: {"passed": True},
        staging_root=tmp_path / "staging",
    )
    assert receipt.status == "PLAN_ONLY"
    assert calls == []
    assert not (tmp_path / "staging").exists()


def test_adapter_module_has_no_provider_or_ledger_imports():
    path = Path(__file__).resolve().parents[2] / "src/learning/retrain_adapters.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        if node.module
    )
    assert not any(name.startswith("src.data") for name in imported)
    assert not any(name.startswith("src.betting") for name in imported)

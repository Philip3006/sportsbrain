from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis.nations_league_model_lifecycle import (
    FROZEN_ALGORITHM_DIGEST,
    establish_initial_active_release,
    register_and_validate_nations_league_release,
    training_snapshot_from_input_state,
)

ROOT = Path(__file__).parents[2]
SOURCE_SHA = "e4d7c579ce347e385e08817c5083527faf6bcc87"
INPUT = ROOT / "results/research/nations_league_v1_1_input_state_20260930T200124Z.json"


def _input_state() -> dict:
    return json.loads(INPUT.read_text(encoding="utf-8"))


def test_sealed_v11_state_establishes_exact_active_release_without_retraining():
    lifecycle, release = establish_initial_active_release(
        _input_state(), source_release_sha=SOURCE_SHA
    )
    assert release.release_id == "d1da2a3b9464304c2359c62b8259eb1a39ebff716c2d6bfe18954afe80955287"
    assert release.snapshot.algorithm_digest == FROZEN_ALGORITHM_DIGEST
    assert release.snapshot.training_row_count == 566
    assert release.snapshot.training_data_digest == (
        "0775611b969cb44cce94c650ecf03da437c40e8c28114c7065a9c3dc1745b6a7"
    )
    assert release.snapshot.trained_state_digest == (
        "7ccaed2c2fc237307c9f74a9adf7755bb6182fed085dffcd11cb4b56a39d47fc"
    )
    again, receipt = register_and_validate_nations_league_release(
        lifecycle,
        _input_state(),
        source_release_sha=SOURCE_SHA,
        created_at=_input_state()["observed_at"],
    )
    assert again.release_id == release.release_id
    assert receipt.outcome == "NO_OP"


def test_tampered_input_state_fails_closed_before_release_creation():
    state = _input_state()
    state["training_records"][0]["result_safe_available_at"] = state[
        "prediction_cutoff"
    ]
    with pytest.raises(ValueError, match="strictly before|mismatch"):
        training_snapshot_from_input_state(state, source_release_sha=SOURCE_SHA)

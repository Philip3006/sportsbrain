from __future__ import annotations

import pytest

from src.learning.adapters import (
    AdapterError,
    attach_prediction,
    football_result_from_record,
    nations_league_prediction_from_record,
    tennis_prediction_from_record,
    tennis_result_from_record,
    top5_prediction_from_record,
)
from src.learning.outcome_contracts import SettlementState, canonical_digest


def _nl_record(phase: str = "INITIAL") -> dict:
    return {
        "record_type": "prediction",
        "record_id": f"nl-{phase.lower()}-1",
        "signal_id": f"nl-signal-{phase.lower()}-1",
        "fixture_id": "nl-fixture-1",
        "competition": "UEFA Nations League",
        "phase": phase,
        "prediction_timestamp": "2026-10-01T18:00:00Z",
        "probabilities": {"home": 0.55, "draw": 0.2, "away": 0.25},
        "model_version": "nations_league_v1_1",
        "model_release_id": "a" * 64,
        "model_digest": "b" * 64,
        "source_provenance": {"capture_cutoff": "2026-10-01T17:55:00Z"},
        "no_bet": True,
    }


def _football_result():
    return football_result_from_record(
        {"status": "completed", "home_score": 2, "away_score": 1},
        fixture_id="nl-fixture-1",
        competition="UEFA Nations League",
        source="official_result_feed",
        source_record_id="official-nl-fixture-1",
        completed_at="2026-10-01T20:00:00Z",
        result_safe_available_at="2026-10-01T21:00:00Z",
        provenance_digest="c" * 64,
    )


def test_nations_league_initial_and_refinement_preserve_identity_and_settle():
    initial = nations_league_prediction_from_record(_nl_record("INITIAL"))
    refinement = nations_league_prediction_from_record(_nl_record("REFINEMENT"))
    result = _football_result()
    first = attach_prediction(
        initial,
        result,
        settled_at="2026-10-01T21:01:00Z",
        provenance_digest="d" * 64,
    )
    second = attach_prediction(
        refinement,
        result,
        settled_at="2026-10-01T21:01:00Z",
        provenance_digest="e" * 64,
    )
    assert initial.source_record_id == "nl-initial-1"
    assert refinement.source_record_id == "nl-refinement-1"
    assert first.settlement_state == SettlementState.WON
    assert second.settlement_state == SettlementState.WON
    assert first.attachment_id != second.attachment_id


def test_tennis_adapter_uses_existing_void_and_winner_semantics():
    raw = {
        "source_record_id": "tennis-1",
        "fixture_id": "tennis-fixture-1",
        "sport": "tennis",
        "competition": "ATP",
        "model_family": "tennis-model",
        "model_release_id": "tennis-release",
        "prediction_timestamp": "2026-10-01T12:00:00Z",
        "feature_cutoff": "2026-10-01T11:55:00Z",
        "market": "home",
        "selection": "home",
    }
    prediction = tennis_prediction_from_record(raw)
    result = tennis_result_from_record(
        {
            "status": "completed",
            "sets": [[6, 3], [6, 4]],
            "winner": "a",
        },
        fixture_id="tennis-fixture-1",
        competition="ATP",
        source="official_tennis_feed",
        source_record_id="official-tennis-1",
        completed_at="2026-10-01T14:00:00Z",
        result_safe_available_at="2026-10-01T15:00:00Z",
        provenance_digest="f" * 64,
    )
    attachment = attach_prediction(
        prediction,
        result,
        settled_at="2026-10-01T15:01:00Z",
        provenance_digest=canonical_digest({"source": result.source}),
    )
    assert attachment.settlement_state == SettlementState.WON


def test_missing_legacy_provenance_is_not_converted():
    with pytest.raises(AdapterError, match="model_family"):
        tennis_prediction_from_record(
            {
                "fixture_id": "missing-model",
                "sport": "tennis",
                "competition": "ATP",
                "prediction_timestamp": "2026-10-01T12:00:00Z",
                "feature_cutoff": "2026-10-01T11:55:00Z",
                "market": "home",
            }
        )


def test_top5_requires_no_bet_and_rejects_authority_flags():
    record = {
        "fixture_id": "top5-1",
        "sport": "football",
        "competition": "EPL",
        "model_family": "top5-shadow",
        "model_release_id": "top5-release",
        "prediction_timestamp": "2026-10-01T12:00:00Z",
        "feature_cutoff": "2026-10-01T11:55:00Z",
        "market": "1X2",
        "selection": "HOME",
        "no_bet": True,
    }
    assert top5_prediction_from_record(record).prediction["no_bet"] is True
    with pytest.raises(AdapterError, match="no-bet"):
        top5_prediction_from_record({**record, "no_bet": False})
    with pytest.raises(AdapterError, match="authority"):
        top5_prediction_from_record(
            {**record, "production_activation_authorized": True}
        )


def test_premature_football_result_is_rejected():
    with pytest.raises(AdapterError, match="not result-safe"):
        football_result_from_record(
            {"status": "in_progress", "home_score": 1, "away_score": 0},
            fixture_id="live-1",
            competition="EPL",
            source="feed",
            source_record_id="live-1",
            completed_at="2026-10-01T14:00:00Z",
            result_safe_available_at="2026-10-01T15:00:00Z",
            provenance_digest="0" * 64,
        )


def test_cancelled_football_result_preserves_void_without_scores():
    result = football_result_from_record(
        {"status": "cancelled"},
        fixture_id="cancelled-1",
        competition="EPL",
        source="official_result_feed",
        source_record_id="cancelled-1",
        completed_at="2026-10-01T14:00:00Z",
        result_safe_available_at="2026-10-01T15:00:00Z",
        provenance_digest="1" * 64,
    )
    assert result.actual_result["status"] == "cancelled"
    assert result.actual_result["home_score"] is None

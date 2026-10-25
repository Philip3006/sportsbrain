from __future__ import annotations

import pytest

from src.learning.adapters import football_result_from_record, prediction_from_record
from src.learning.coverage import build_coverage_report
from src.learning.outcome_contracts import InMemoryOutcomeStore, LifecycleError
from src.learning.settlement import settle_predictions


def _prediction(
    *, fixture_id: str = "fixture-1", with_features: bool = False, unsafe: bool = False
):
    raw = {
        "source_record_id": f"signal-{fixture_id}",
        "fixture_id": fixture_id,
        "sport": "football",
        "competition": "EPL",
        "model_family": "generic-football",
        "model_release_id": "release-1",
        "prediction_timestamp": "2026-10-01T12:00:00Z",
        "feature_cutoff": "2026-10-01T11:55:00Z",
        "market": "1X2",
        "selection": "HOME",
    }
    if with_features:
        raw.update(
            {
                "features": {"elo_diff": 12},
                "feature_available_at": "2026-10-01T11:50:00Z",
                "training_cutoff": (
                    "2026-10-01T20:30:00Z" if unsafe else "2026-10-01T22:00:00Z"
                ),
            }
        )
    return prediction_from_record(raw, source_system="generic_football")


def _result(*, fixture_id: str = "fixture-1", score: tuple[int, int] = (2, 1)):
    return football_result_from_record(
        {"status": "completed", "home_score": score[0], "away_score": score[1]},
        fixture_id=fixture_id,
        competition="EPL",
        source="official_result_feed",
        source_record_id=f"result-{fixture_id}",
        completed_at="2026-10-01T20:00:00Z",
        result_safe_available_at="2026-10-01T21:00:00Z",
        provenance_digest="a" * 64,
    )


def test_settlement_is_read_only_by_default_and_then_idempotent_when_written():
    prediction = _prediction()
    result = _result()
    store = InMemoryOutcomeStore()
    preview = settle_predictions(
        [prediction],
        [result],
        settled_at="2026-10-01T21:01:00Z",
        store=store,
    )
    assert preview.settled == 1
    assert store.results == {}
    first = settle_predictions(
        [prediction],
        [result],
        settled_at="2026-10-01T21:01:00Z",
        store=store,
        write=True,
    )
    second = settle_predictions(
        [prediction],
        [result],
        settled_at="2026-10-01T21:01:00Z",
        store=store,
        write=True,
    )
    assert first.to_payload() == second.to_payload()
    assert len(store.results) == 1
    assert len(store.attachments) == 1


def test_missing_result_is_awaiting_and_unrelated_result_fails_closed():
    prediction = _prediction()
    report = settle_predictions(
        [prediction],
        [],
        settled_at="2026-10-01T21:01:00Z",
    )
    assert report.awaiting_result == 1
    with pytest.raises(LifecycleError, match="not represented"):
        settle_predictions(
            [prediction],
            [_result(fixture_id="other")],
            settled_at="2026-10-01T21:01:00Z",
        )


def test_conflicting_duplicate_result_is_rejected_before_write():
    with pytest.raises(LifecycleError, match="conflicting authoritative results"):
        settle_predictions(
            [_prediction()],
            [_result(), _result(score=(0, 2))],
            settled_at="2026-10-01T21:01:00Z",
            store=InMemoryOutcomeStore(),
            write=True,
        )


def test_causal_row_is_emitted_only_when_time_safe():
    report = settle_predictions(
        [_prediction(with_features=True)],
        [_result()],
        settled_at="2026-10-01T21:01:00Z",
    )
    assert report.causal_training_rows_generated == 1
    with pytest.raises(LifecycleError, match="result is not safe"):
        settle_predictions(
            [_prediction(with_features=True, unsafe=True)],
            [_result()],
            settled_at="2026-10-01T21:01:00Z",
        )


def test_coverage_report_is_deterministic_and_segmented():
    prediction = _prediction()
    result = _result()
    store = InMemoryOutcomeStore()
    settle_predictions(
        [prediction],
        [result],
        settled_at="2026-10-01T21:01:00Z",
        store=store,
        write=True,
    )
    report = build_coverage_report([prediction], store.attachments.values())
    assert report["predictions_emitted"] == 1
    assert report["predictions_represented"] == 1
    assert report["settlement_states"] == {"won": 1}
    assert report["segments"][0]["competition"] == "EPL"

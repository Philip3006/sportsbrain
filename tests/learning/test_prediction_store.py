from __future__ import annotations

import json

import pytest

from src.learning.adapters import prediction_from_record
from src.learning.outcome_contracts import LifecycleError
from src.learning.prediction_store import (
    InMemoryPredictionStore,
    JsonlPredictionEvidenceStore,
    capture_predictions,
)


def _prediction():
    return prediction_from_record(
        {
            "source_record_id": "signal-1",
            "fixture_id": "fixture-1",
            "sport": "football",
            "competition": "EPL",
            "model_family": "model",
            "model_release_id": "release",
            "prediction_timestamp": "2026-10-01T12:00:00Z",
            "feature_cutoff": "2026-10-01T11:55:00Z",
            "market": "1X2",
            "selection": "HOME",
        },
        source_system="generic_football",
    )


def test_capture_is_read_only_until_explicit_write_and_replay_is_idempotent():
    prediction = _prediction()
    store = InMemoryPredictionStore()
    capture_predictions([prediction], store=store)
    assert store.predictions == {}
    capture_predictions([prediction], store=store, write=True)
    assert capture_predictions([prediction], store=store, write=True) == (prediction,)
    assert tuple(store.predictions.values()) == (prediction,)


def test_jsonl_prediction_store_reloads_and_rejects_tampered_payload(tmp_path):
    prediction = _prediction()
    path = tmp_path / "predictions.jsonl"
    store = JsonlPredictionEvidenceStore(path)
    assert store.append_prediction(prediction) is True
    assert store.append_prediction(prediction) is False
    assert store.load_predictions() == (prediction,)
    payload = prediction.to_payload()
    payload["prediction"]["selection"] = "AWAY"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(LifecycleError, match="prediction_digest"):
        store.load_predictions()

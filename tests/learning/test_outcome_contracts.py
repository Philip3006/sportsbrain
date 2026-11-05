from __future__ import annotations

import json

import pytest

from src.learning import (
    AuthoritativeResultV1,
    CausalTrainingRowV1,
    InMemoryOutcomeStore,
    JsonlOutcomeStore,
    LifecycleError,
    PredictionSnapshotV1,
    RetrainDecision,
    append_training_rows,
    build_outcome_attachment,
    canonical_digest,
    decide_retrain,
)


def _result(
    *,
    fixture_id: str = "fixture-1",
    score: tuple[int, int] = (2, 1),
    completed_at: str = "2026-10-01T20:45:00Z",
    result_safe_available_at: str = "2026-10-01T22:00:00Z",
) -> AuthoritativeResultV1:
    actual = {"status": "completed", "home_score": score[0], "away_score": score[1]}
    result_digest = canonical_digest(actual)
    identity = {
        "schema": "sportsbrain-authoritative-result-v1",
        "fixture_id": fixture_id,
        "sport": "football",
        "competition": "UEFA Nations League",
        "source": "official_result_feed",
        "source_record_id": f"official-{fixture_id}",
        "completed_at": completed_at,
        "result_safe_available_at": result_safe_available_at,
        "result_digest": result_digest,
        "provenance_digest": "a" * 64,
    }
    return AuthoritativeResultV1(
        result_id=canonical_digest(identity),
        fixture_id=fixture_id,
        sport="football",
        competition="UEFA Nations League",
        source="official_result_feed",
        source_record_id=f"official-{fixture_id}",
        completed_at=completed_at,
        result_safe_available_at=result_safe_available_at,
        actual_result=actual,
        result_digest=result_digest,
        provenance_digest="a" * 64,
    )


def _prediction(
    *, fixture_id: str = "fixture-1", phase: str = "INITIAL"
) -> PredictionSnapshotV1:
    prediction = {"market": "1X2", "selection": "HOME", "probability": 0.61}
    prediction_digest = canonical_digest({"prediction": prediction})
    identity = {
        "schema": "sportsbrain-prediction-snapshot-v1",
        "signal_id": f"signal-{fixture_id}-{phase}",
        "fixture_id": fixture_id,
        "sport": "football",
        "competition": "UEFA Nations League",
        "model_family": "nations_league_v1_1",
        "model_release_id": "b" * 64,
        "lifecycle_version": "nations_league_v1_1",
        "phase": phase,
        "prediction_timestamp": "2026-10-01T18:45:00Z",
        "feature_cutoff": "2026-10-01T18:45:00Z",
        "prediction_digest": prediction_digest,
    }
    return PredictionSnapshotV1(
        prediction_id=canonical_digest(identity),
        signal_id=f"signal-{fixture_id}-{phase}",
        fixture_id=fixture_id,
        sport="football",
        competition="UEFA Nations League",
        model_family="nations_league_v1_1",
        model_release_id="b" * 64,
        lifecycle_version="nations_league_v1_1",
        phase=phase,
        prediction_timestamp="2026-10-01T18:45:00Z",
        feature_cutoff="2026-10-01T18:45:00Z",
        prediction=prediction,
        prediction_digest=prediction_digest,
    )


def _row(
    result: AuthoritativeResultV1, *, fixture_id: str | None = None
) -> CausalTrainingRowV1:
    features = {"elo_diff": 12.0, "feature_source": "pre_match_snapshot"}
    identity = {
        "schema": "sportsbrain-causal-training-row-v1",
        "fixture_id": fixture_id or result.fixture_id,
        "sport": "football",
        "competition": "UEFA Nations League",
        "training_cutoff": "2026-10-02T00:00:00Z",
        "feature_available_at": "2026-10-01T18:00:00Z",
        "event_completed_at": result.completed_at,
        "result_safe_available_at": result.result_safe_available_at,
        "feature_digest": canonical_digest(features),
        "result_id": result.result_id,
    }
    row_id = canonical_digest(identity)
    without_digest = {
        **identity,
        "features": features,
        "label": {"home_score": 2, "away_score": 1},
        "row_id": row_id,
    }
    return CausalTrainingRowV1(
        row_id=row_id,
        fixture_id=fixture_id or result.fixture_id,
        sport="football",
        competition="UEFA Nations League",
        training_cutoff="2026-10-02T00:00:00Z",
        feature_available_at="2026-10-01T18:00:00Z",
        event_completed_at=result.completed_at,
        result_safe_available_at=result.result_safe_available_at,
        features=features,
        label={"home_score": 2, "away_score": 1},
        result_id=result.result_id,
        feature_digest=canonical_digest(features),
        row_digest=canonical_digest(without_digest),
    )


def test_result_prediction_attachment_is_immutable_and_idempotent():
    result = _result()
    prediction = _prediction()
    attachment = build_outcome_attachment(
        prediction,
        result,
        settled_at="2026-10-01T22:01:00Z",
        provenance_digest="c" * 64,
    )
    store = InMemoryOutcomeStore()
    assert store.append_result(result) is True
    assert store.append_result(result) is False
    assert store.append_attachment(attachment) is True
    assert store.append_attachment(attachment) is False
    assert prediction.to_payload()["prediction"] == {
        "market": "1X2",
        "selection": "HOME",
        "probability": 0.61,
    }


def test_prediction_contract_without_optional_source_id_round_trips():
    prediction = _prediction()
    payload = prediction.to_payload()
    assert "source_record_id" not in payload
    assert PredictionSnapshotV1.from_payload(payload).to_payload() == payload


def test_conflicting_result_replay_fails_closed():
    store = InMemoryOutcomeStore()
    original = _result()
    store.append_result(original)
    with pytest.raises(LifecycleError, match="conflicting"):
        store.append_result(_result(score=(0, 1)))


def test_result_binding_and_time_order_fail_closed():
    result = _result()
    with pytest.raises(LifecycleError, match="fixture_id binding"):
        build_outcome_attachment(
            _prediction(fixture_id="other"),
            result,
            settled_at="2026-10-01T22:01:00Z",
            provenance_digest="c" * 64,
        )
    with pytest.raises(LifecycleError, match="result must become safe"):
        build_outcome_attachment(
            _prediction(),
            _result(
                completed_at="2026-10-01T18:00:00Z",
                result_safe_available_at="2026-10-01T18:30:00Z",
            ),
            settled_at="2026-10-01T18:45:01Z",
            provenance_digest="c" * 64,
        )


def test_tampered_digests_and_missing_authoritative_result_are_rejected():
    result = _result()
    payload = result.to_payload()
    payload["actual_result"]["home_score"] = 9
    with pytest.raises(LifecycleError, match="result_digest"):
        AuthoritativeResultV1.from_payload(payload)

    store = InMemoryOutcomeStore()
    with pytest.raises(LifecycleError, match="unknown result"):
        attachment = build_outcome_attachment(
            _prediction(),
            AuthoritativeResultV1.from_payload(result.to_payload()),
            settled_at="2026-10-01T22:01:00Z",
            provenance_digest="c" * 64,
        )
        store.append_attachment(attachment)


def test_causal_training_extension_is_deterministic_and_result_safe():
    row = _row(_result())
    decision, merged, digest = decide_retrain([], [row])
    assert decision is RetrainDecision.RETRAIN_REQUIRED
    assert len(merged) == 1
    assert digest == canonical_digest([row.to_payload()])
    decision_again, merged_again, _ = decide_retrain(merged, [row])
    assert decision_again is RetrainDecision.NO_OP
    assert [item.to_payload() for item in merged_again] == [row.to_payload()]

    with pytest.raises(LifecycleError, match="feature_available_at"):
        bad = _row(_result())
        CausalTrainingRowV1(
            row_id=bad.row_id,
            fixture_id=bad.fixture_id,
            sport=bad.sport,
            competition=bad.competition,
            training_cutoff=bad.training_cutoff,
            feature_available_at="2026-10-02T00:01:00Z",
            event_completed_at=bad.event_completed_at,
            result_safe_available_at=bad.result_safe_available_at,
            features=bad.features,
            label=bad.label,
            result_id=bad.result_id,
            feature_digest=bad.feature_digest,
            row_digest=bad.row_digest,
        )


def test_conflicting_training_rows_fail_closed():
    first = _row(_result())
    changed = _row(_result(), fixture_id="different")
    # Force the same identity while changing the supervised label.
    changed = CausalTrainingRowV1(
        row_id=first.row_id,
        fixture_id=first.fixture_id,
        sport=first.sport,
        competition=first.competition,
        training_cutoff=first.training_cutoff,
        feature_available_at=first.feature_available_at,
        event_completed_at=first.event_completed_at,
        result_safe_available_at=first.result_safe_available_at,
        features=first.features,
        label={"home_score": 0, "away_score": 9},
        result_id=first.result_id,
        feature_digest=first.feature_digest,
        row_digest=canonical_digest(
            {
                **first.to_payload_without_row_digest(),
                "label": {"home_score": 0, "away_score": 9},
            }
        ),
    )
    with pytest.raises(LifecycleError, match="conflicting"):
        append_training_rows([first], [changed])


def test_jsonl_store_survives_reload_and_rejects_corruption(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    store = JsonlOutcomeStore(path)
    result = _result()
    attachment = build_outcome_attachment(
        _prediction(),
        result,
        settled_at="2026-10-01T22:01:00Z",
        provenance_digest="c" * 64,
    )
    assert store.append_result(result) is True
    assert store.append_attachment(attachment) is True
    reloaded = JsonlOutcomeStore(path)
    results, attachments = reloaded.load()
    assert [item.result_id for item in results] == [result.result_id]
    assert [item.attachment_id for item in attachments] == [attachment.attachment_id]

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["actual_result"]["home_score"] = 8
    path.write_text(json.dumps(tampered) + "\n" + lines[1] + "\n", encoding="utf-8")
    with pytest.raises(LifecycleError, match="result_digest"):
        reloaded.load()


def test_future_sport_result_payload_is_not_reinterpreted_by_core_contract():
    result = _result()
    payload = result.to_payload()
    payload["sport"] = "future_sport"
    identity = {
        key: payload[key]
        for key in (
            "schema",
            "fixture_id",
            "sport",
            "competition",
            "source",
            "source_record_id",
            "completed_at",
            "result_safe_available_at",
            "result_digest",
            "provenance_digest",
        )
    }
    payload["result_id"] = canonical_digest(identity)
    parsed = AuthoritativeResultV1.from_payload(payload)
    assert parsed.sport == "future_sport"

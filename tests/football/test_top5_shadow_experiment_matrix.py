"""Regression coverage for the neutral Top-5 experiment evidence matrix."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from math import isclose, log

from src.football.top5_real_shadow_measurement import measure_session_payload
from src.football.top5_shadow_experiment_matrix import build_experiment_matrix
from tests.football.test_top5_real_shadow_measurement import _complete_session


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _refresh(report: dict) -> dict:
    report["measurement_digest"] = ""
    report["measurement_digest"] = _digest(
        {key: value for key, value in report.items() if key != "measurement_digest"}
    )
    return report


def _measurement(
    experiment_id: str = "shadow-experiment:one",
    *,
    prediction_id: str | None = None,
    probabilities: dict[str, float] | None = None,
    closing: bool = True,
) -> dict:
    payload, evidence = _complete_session(f"shadow-session:{experiment_id}")
    report = measure_session_payload(payload, evidence_bundle=evidence)
    record = report["eligible_predictions"][0]
    record["signal_time_experiment_id"] = experiment_id
    report["experiment_ids"] = [experiment_id]
    if prediction_id is not None:
        record["prediction_id"] = prediction_id
    if probabilities is not None:
        record["probabilities"] = probabilities
    if not closing:
        record["closing"] = None
        report["closing_benchmark"] = {
            "available": False,
            "label": "BENCHMARK / CLV MEASUREMENT ONLY",
            "reason": "no valid closing evidence is available",
            "count": 0,
        }
    report["eligible_prediction_ids"] = sorted(
        item["prediction_id"] for item in report["eligible_predictions"]
    )
    return _refresh(report)


def _add_second_fixture(report: dict, experiment_id: str) -> dict:
    second = deepcopy(report["eligible_predictions"][0])
    second["prediction_id"] = f"{second['prediction_id']}:second"
    second["fixture"] = f"{second['fixture']}:second"
    second["signal_time_experiment_id"] = experiment_id
    second["prediction_artifact_sha"] = "a" * 64
    second["result_attachment_sha"] = "b" * 64
    report["eligible_predictions"].append(second)
    report["eligible_prediction_ids"] = sorted(
        item["prediction_id"] for item in report["eligible_predictions"]
    )
    report["eligible_count"] = 2
    report["unique_fixture_count"] = 2
    report["unique_prediction_count"] = 2
    return _refresh(report)


def test_one_experiment_has_neutral_descriptive_summary() -> None:
    report = build_experiment_matrix([_measurement()])

    assert report["overall_state"] == "COMPLETE"
    assert report["experiment_ids"] == ["shadow-experiment:one"]
    assert report["eligible_count"] == 1
    assert report["experiment_summaries"]["shadow-experiment:one"]["eligible_n"] == 1
    assert report["production_activation_authorized"] is False
    assert report["signal_time_approved_for_production"] is False
    assert report["experiment_winner_selected"] is False
    assert report["provider_winner_selected"] is False


def test_same_fixture_pair_has_correct_brier_and_log_loss_deltas() -> None:
    first = _measurement("shadow-experiment:one", prediction_id="prediction-one")
    second = _measurement(
        "shadow-experiment:two",
        prediction_id="prediction-two",
        probabilities={"away": 0.2, "draw": 0.2, "home": 0.6},
    )
    comparison = build_experiment_matrix([first, second])["paired_comparisons"][0]

    assert comparison["paired_fixture_count"] == 1
    assert comparison["direction"] == "experiment_A - experiment_B"
    first = {
        "away": 0.3080724876441515,
        "draw": 0.27182866556836904,
        "home": 0.4200988467874794,
    }
    second = {"away": 0.2, "draw": 0.2, "home": 0.6}
    expected_brier = (
        sum((first[name] - float(name == "home")) ** 2 for name in first) / 3
        - sum((second[name] - float(name == "home")) ** 2 for name in second) / 3
    )
    expected_log_loss = -log(first["home"]) + log(second["home"])
    assert isclose(comparison["mean_paired_brier_delta"], expected_brier)
    assert isclose(comparison["mean_paired_log_loss_delta"], expected_log_loss)
    assert comparison["confidence_interval"]["status"] == (
        "UNAVAILABLE / INSUFFICIENT_PAIRED_SAMPLE"
    )
    assert "winner_selected" not in comparison


def test_two_shared_fixtures_get_deterministic_bootstrap_interval() -> None:
    first = _add_second_fixture(
        _measurement("shadow-experiment:one", prediction_id="prediction-one"),
        "shadow-experiment:one",
    )
    second = _add_second_fixture(
        _measurement(
            "shadow-experiment:two",
            prediction_id="prediction-two",
            probabilities={"away": 0.2, "draw": 0.2, "home": 0.6},
        ),
        "shadow-experiment:two",
    )
    first["eligible_predictions"][1]["fixture"] = second["eligible_predictions"][1][
        "fixture"
    ]
    first = _refresh(first)

    report = build_experiment_matrix([first, second])
    interval = report["paired_comparisons"][0]["confidence_interval"]
    assert interval["status"] == "AVAILABLE"
    assert interval["resamples"] == 2000
    assert report == build_experiment_matrix([second, first])


def test_measurement_digest_tampering_fails_closed() -> None:
    measurement = _measurement()
    measurement["eligible_predictions"][0]["actual_outcome"] = "away"

    report = build_experiment_matrix([measurement])

    assert report["overall_state"] == "FAILED_CLOSED"
    assert "MALFORMED_MEASUREMENT" in report["cohort_integrity"]["findings"]


def test_non_object_measurement_fails_closed() -> None:
    report = build_experiment_matrix(["not-a-measurement"])

    assert report["overall_state"] == "FAILED_CLOSED"
    assert report["cohort_integrity"]["findings"] == [
        "MALFORMED_MEASUREMENT",
        "NO_VALID_MEASUREMENT_INPUT",
    ]


def test_non_real_and_mixed_identity_measurements_fail_closed() -> None:
    non_real = _measurement()
    non_real["eligible_predictions"][0]["evidence_mode"] = "OFFLINE_REPLAY"
    mixed = _measurement("shadow-experiment:mixed")
    mixed["eligible_predictions"][0]["model_identity"] = "M4_unapproved"

    report = build_experiment_matrix([non_real, mixed])

    assert report["overall_state"] == "FAILED_CLOSED"
    assert report["cohort_integrity"]["state"] == "FAILED_CLOSED"
    assert report["eligible_count"] == 0

    mixed_research = _measurement("shadow-experiment:mixed-research")
    mixed_research["eligible_predictions"][0]["research_sha"] = "a" * 40
    research_report = build_experiment_matrix([_refresh(mixed_research)])
    assert research_report["overall_state"] == "FAILED_CLOSED"


def test_duplicate_and_divergent_prediction_identity_fail_closed() -> None:
    first = _measurement(prediction_id="same-prediction")
    duplicate = deepcopy(first)
    divergent = deepcopy(first)
    divergent["eligible_predictions"][0]["prediction_artifact_sha"] = "f" * 64
    divergent = _refresh(divergent)

    duplicate_report = build_experiment_matrix([first, duplicate])
    divergent_report = build_experiment_matrix([first, divergent])

    assert duplicate_report["overall_state"] == "FAILED_CLOSED"
    assert "DUPLICATE_PREDICTION_ID" in duplicate_report["cohort_integrity"]["findings"]
    assert (
        "DIVERGENT_PREDICTION_ARTIFACT"
        in divergent_report["cohort_integrity"]["findings"]
    )


def test_duplicate_fixture_and_conflicting_result_fail_closed() -> None:
    first = _measurement("shadow-experiment:one", prediction_id="first")
    duplicate_fixture = _measurement("shadow-experiment:one", prediction_id="second")
    duplicate_report = build_experiment_matrix([first, duplicate_fixture])
    assert (
        "DUPLICATE_FIXTURE_WITHIN_EXPERIMENT"
        in duplicate_report["cohort_integrity"]["findings"]
    )

    conflicting_result = _measurement("shadow-experiment:two", prediction_id="third")
    conflicting_result["eligible_predictions"][0]["actual_outcome"] = "away"
    conflict_report = build_experiment_matrix([first, _refresh(conflicting_result)])
    assert (
        "CONFLICTING_RESULT_IDENTITY" in conflict_report["cohort_integrity"]["findings"]
    )


def test_groupings_closing_and_missing_closing_are_descriptive_only() -> None:
    with_closing = _measurement("shadow-experiment:with-closing")
    without_closing = _measurement("shadow-experiment:without-closing", closing=False)

    report = build_experiment_matrix([with_closing, without_closing])

    assert report["breakdowns"]["league"]
    assert report["breakdowns"]["provider"]
    assert report["breakdowns"]["controlled_shadow_run_id"]
    assert report["breakdowns"]["qualification_session_id"]
    assert (
        report["experiment_summaries"]["shadow-experiment:with-closing"][
            "closing_benchmark"
        ]["available"]
        is True
    )
    assert (
        report["experiment_summaries"]["shadow-experiment:without-closing"][
            "closing_benchmark"
        ]["available"]
        is False
    )
    assert "rank" not in json.dumps(report["experiment_summaries"]).lower()
    assert "winner" not in json.dumps(report["experiment_summaries"]).lower()


def test_source_measurement_is_not_mutated() -> None:
    measurement = _measurement()
    before = deepcopy(measurement)

    build_experiment_matrix([measurement])

    assert measurement == before

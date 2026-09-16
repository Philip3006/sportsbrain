"""Regression coverage for the read-only Top-5 measurement pack."""

from __future__ import annotations

import json
from copy import deepcopy

from src.football.top5_real_shadow_measurement import (
    measure_directory_payloads,
    measure_session_payload,
)
from src.football.top5_real_shadow_session import RealShadowSession
from src.football.top5_real_shadow_session_evidence import build_shadow_evidence
from tests.football.test_top5_real_shadow_session import (
    BASE,
    INTEGRATION_SHA,
    closing_attachment,
    experiment,
    final_result,
    observation,
)


def _complete_session(
    session_key: str = "shadow-session:measurement",
) -> tuple[dict, dict]:
    session = RealShadowSession.create(
        session_key,
        experiment=experiment(),
        league_scope=("EPL",),
        integration_sha=INTEGRATION_SHA,
        created_at=BASE,
        fixture_mode=False,
    )
    session.record_observation(observation(mode="REAL_OBSERVED"))
    session.finalize_predictions()
    prediction = next(iter(session.predictions.values()))
    session.attach_result(final_result(prediction, session_key))
    session.attach_closing(closing_attachment(prediction, session_key))
    return session.as_payload(), build_shadow_evidence(session)


def _pending_closing_session() -> tuple[dict, dict]:
    session = RealShadowSession.create(
        "shadow-session:pending-closing",
        experiment=experiment(),
        league_scope=("EPL",),
        integration_sha=INTEGRATION_SHA,
        created_at=BASE,
        fixture_mode=False,
    )
    session.record_observation(observation(mode="REAL_OBSERVED"))
    session.finalize_predictions()
    prediction = next(iter(session.predictions.values()))
    session.attach_result(final_result(prediction, "pending-closing"))
    return session.as_payload(), build_shadow_evidence(session)


def test_valid_real_observation_produces_primary_metrics_and_safety() -> None:
    payload, evidence = _complete_session()
    report = measure_session_payload(payload, evidence_bundle=evidence)

    metrics = report["primary_metrics"]
    assert report["overall_state"] == "COMPLETE"
    assert report["eligible_count"] == 1
    assert report["unique_fixture_count"] == 1
    assert report["unique_prediction_count"] == 1
    assert metrics["N"] == 1
    assert metrics["brier_score"] is not None
    assert metrics["log_loss"] is not None
    assert metrics["top_probability_accuracy"] in {0.0, 1.0}
    assert sum(metrics["outcome_distribution"].values()) == 1
    assert sum(metrics["predicted_class_distribution"].values()) == 1
    assert report["safety_invariants"]["production_activation_authorized"] is False
    assert report["safety_invariants"]["publication_authorized"] is False
    assert report["safety_invariants"]["betting_authorized"] is False
    assert report["safety_invariants"]["provider_ranking_emitted"] is False


def test_metric_values_are_deterministic_and_order_independent() -> None:
    first_payload, first_evidence = _complete_session("shadow-session:one")
    second_payload, second_evidence = _complete_session("shadow-session:two")

    first = measure_directory_payloads(
        [first_payload, first_evidence, second_payload, second_evidence]
    )
    second = measure_directory_payloads(
        [second_evidence, first_payload, second_payload, first_evidence]
    )

    assert first == second
    assert first["eligible_count"] == 2
    assert first["measurement_digest"]
    assert first["cohort_digest"]


def test_pending_closing_does_not_remove_prediction_from_metrics() -> None:
    payload, evidence = _pending_closing_session()

    report = measure_session_payload(payload, evidence_bundle=evidence)

    assert report["eligible_count"] == 1
    assert report["primary_metrics"]["N"] == 1
    assert report["closing_benchmark"]["available"] is False
    assert report["closing_benchmark"]["count"] == 0


def test_valid_closing_is_benchmark_only_and_never_a_prediction_input() -> None:
    payload, evidence = _complete_session()
    report = measure_session_payload(payload, evidence_bundle=evidence)

    closing = report["closing_benchmark"]
    assert closing["available"] is True
    assert closing["label"] == "BENCHMARK / CLV MEASUREMENT ONLY"
    assert closing["count"] == 1
    assert closing["observations"][0]["used_for_prediction"] is False
    assert report["safety_invariants"]["closing_used_for_prediction"] is False
    assert "ranking" not in json.dumps(report["groups"]).lower()


def test_test_fixture_and_offline_replay_are_excluded_from_real_totals() -> None:
    fixture = RealShadowSession.create(
        "shadow-session:fixture",
        experiment=experiment(),
        league_scope=("BL1",),
        integration_sha=INTEGRATION_SHA,
        created_at=BASE,
        fixture_mode=True,
    )
    fixture.record_observation(observation())
    fixture.finalize_predictions()
    fixture_prediction = next(iter(fixture.predictions.values()))
    fixture.attach_result(final_result(fixture_prediction, "fixture"))
    fixture.attach_closing(closing_attachment(fixture_prediction, "fixture"))
    fixture_payload = fixture.as_payload()
    fixture_evidence = build_shadow_evidence(fixture)

    offline_payload, offline_evidence = _complete_session("shadow-session:offline")
    offline_payload["observations"][0]["observation_mode"] = "OFFLINE_REPLAY"

    report = measure_directory_payloads(
        [fixture_payload, fixture_evidence, offline_payload, offline_evidence]
    )

    assert report["eligible_count"] == 0
    assert report["exclusion_taxonomy"]["NON_REAL_EVIDENCE"] == 2
    assert all(
        item["reason"] == "NON_REAL_EVIDENCE" for item in report["excluded_evidence"]
    )


def test_duplicate_prediction_is_failed_closed_and_not_counted_twice() -> None:
    payload, evidence = _complete_session()
    report = measure_directory_payloads([payload, evidence, payload, evidence])

    assert report["overall_state"] == "FAILED_CLOSED"
    assert report["cohort_integrity"]["state"] == "FAILED_CLOSED"
    assert report["eligible_count"] == 0
    assert report["primary_metrics"]["N"] == 0
    assert report["exclusion_taxonomy"]["DUPLICATE_PREDICTION"] >= 2


def test_mixed_model_identity_fails_closed_for_the_whole_cohort() -> None:
    first_payload, first_evidence = _complete_session("shadow-session:first")
    second_payload, second_evidence = _complete_session("shadow-session:second")
    second_payload["predictions"][0]["model_identity"] = "M4_unapproved"

    report = measure_directory_payloads(
        [first_payload, first_evidence, second_payload, second_evidence]
    )

    assert report["overall_state"] == "FAILED_CLOSED"
    assert report["eligible_count"] == 0
    assert "MIXED_RESEARCH_OR_MODEL_IDENTITY" in report["cohort_integrity"]["findings"]


def test_conflicting_result_and_research_identity_are_excluded() -> None:
    payload, evidence = _complete_session()
    conflicting = deepcopy(payload)
    conflicting["results"].append(deepcopy(conflicting["results"][0]))
    conflicting["predictions"][0]["research_sha"] = "a" * 40

    report = measure_session_payload(conflicting, evidence_bundle=evidence)

    assert report["eligible_count"] == 0
    assert report["exclusion_taxonomy"].get("CONFLICT", 0) >= 1

    mismatched = deepcopy(payload)
    mismatched["predictions"][0]["research_sha"] = "a" * 40
    report = measure_session_payload(mismatched, evidence_bundle=evidence)
    assert report["exclusion_taxonomy"].get("PROVENANCE_MISMATCH", 0) >= 1


def test_used_for_prediction_closing_fails_closed() -> None:
    payload, evidence = _complete_session()
    payload["closings"][0]["used_for_prediction"] = True

    report = measure_session_payload(payload, evidence_bundle=evidence)

    assert report["eligible_count"] == 0
    assert report["exclusion_taxonomy"].get("PROVENANCE_MISMATCH", 0) == 1


def test_grouping_is_descriptive_and_contains_no_activation_authority() -> None:
    payload, evidence = _complete_session()
    report = measure_session_payload(payload, evidence_bundle=evidence)

    assert set(report["groups"]) == {
        "league",
        "provider",
        "signal_time_experiment_id",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "bookmaker",
    }
    assert report["groups"]["league"]["EPL"]["N"] == 1
    assert report["safety_invariants"]["signal_time_approved_for_production"] is False


def test_report_does_not_emit_secret_or_raw_provider_fields() -> None:
    payload, evidence = _complete_session()
    report = measure_session_payload(payload, evidence_bundle=evidence)
    serialized = json.dumps(report, sort_keys=True).lower()

    for forbidden in ("api_key", "authorization", "response_body", "cookie", "token"):
        assert forbidden not in serialized

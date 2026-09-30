"""Deterministic governance tests for the Nations League forward campaign."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.analysis.nations_league_forward_campaign import (
    EVIDENCE_REAL,
    EVIDENCE_SYNTHETIC,
    FORWARD_EVIDENCE_ACCUMULATING,
    INSUFFICIENT_FORWARD_EVIDENCE,
    NO_FORWARD_EVIDENCE,
    PROMOTION_REVIEW_ELIGIBLE,
    ForwardCampaignError,
    append_forward_prediction,
    append_forward_settlement,
    build_forward_evidence_summary,
    create_forward_campaign,
    render_forward_operator_view,
    serialize_forward_summary,
    update_forward_campaign,
)
from src.analysis.nations_league_v1_1 import (
    COMPETITION,
    build_forward_shadow_prediction,
    build_shadow_settlement,
    model_digest,
)

TIMELINE_DIGEST = "a" * 64
SOURCE_DIGEST = "b" * 64


def _manifest():
    return [
        {
            "fixture_id": "uefa-nl:campaign-001",
            "edition": "2024/25",
            "initial_eligible": True,
            "refinement_eligible": True,
        },
        {
            "fixture_id": "uefa-nl:campaign-002",
            "edition": "2024/25",
            "initial_eligible": True,
            "refinement_eligible": True,
        },
        {
            "fixture_id": "uefa-nl:campaign-admin",
            "edition": "2024/25",
            "initial_eligible": False,
            "refinement_eligible": False,
            "exception": "administrative",
        },
        {
            "fixture_id": "uefa-nl:campaign-cancelled",
            "edition": "2024/25",
            "initial_eligible": False,
            "refinement_eligible": False,
            "exception": "cancelled",
        },
    ]


def _campaign():
    return create_forward_campaign(
        campaign_id="nl-forward-2026-01",
        edition="2024/25",
        campaign_start="2026-09-30T10:00:00Z",
        fixture_manifest=_manifest(),
        fixture_manifest_digest=TIMELINE_DIGEST,
    )


def _prediction(fixture_id="uefa-nl:campaign-001", phase="initial"):
    kickoff = "2026-10-02T20:00:00Z"
    timestamp = "2026-10-01T20:00:00Z"
    if phase == "refinement":
        timestamp = "2026-10-02T18:30:00Z"
    fixture = {
        "fixture_id": fixture_id,
        "edition": "2024/25",
        "evaluation_block": "NL_2024_25",
        "home_team": "Austria",
        "away_team": "Belgium",
        "kickoff_utc": kickoff,
        "competition": COMPETITION,
        "source_provenance": "test-forward-fixture",
        "source_digest": SOURCE_DIGEST,
        "neutral": False,
    }
    training = [
        {
            "fixture_id": "uefa-nl:training-001",
            "edition": "2022/23",
            "evaluation_block": "NL_2022_23",
            "home_team": "Austria",
            "away_team": "Belgium",
            "kickoff_utc": "2026-08-01T20:00:00Z",
            "competition": COMPETITION,
            "source_provenance": "frozen-training",
            "source_digest": "c" * 64,
            "result_safe_available_at": "2026-08-01T23:00:00Z",
            "home_score": 2,
            "away_score": 1,
            "neutral": False,
        }
    ]
    return build_forward_shadow_prediction(
        fixture,
        phase=phase,
        prediction_timestamp=timestamp,
        training_records=training,
        input_provenance={
            "timeline_digest": TIMELINE_DIGEST,
            "fixture_source_digest": SOURCE_DIGEST,
            "training_cutoff": "2026-10-01T20:00:00Z",
        },
    )


def _settlement(prediction):
    return build_shadow_settlement(
        prediction,
        home_score=1,
        away_score=0,
        result_safe_available_at="2026-10-02T23:00:00Z",
        settled_at="2026-10-03T00:00:00Z",
        result_provenance="test-result-source",
    )


def test_zero_real_samples_is_explicit_and_does_not_hide_exceptions():
    assert _campaign().campaign.model_version == "nations_league_v1_1"
    assert _campaign().campaign.fixture_manifest_digest == TIMELINE_DIGEST
    summary = build_forward_evidence_summary(_campaign())
    assert summary["evidence_state"] == NO_FORWARD_EVIDENCE
    assert summary["completeness"] == {
        "eligible_fixtures": 2,
        "initial_eligible": 2,
        "initial_captured": 0,
        "initial_missed": 2,
        "refinement_eligible": 2,
        "refinement_captured": 0,
        "refinement_missed": 2,
        "settled": 0,
        "unsettled": 0,
        "administrative_exceptions": 1,
        "cancelled_exceptions": 1,
        "synthetic_predictions_excluded": 0,
    }


def test_one_injected_real_sample_is_accounted_without_promotion():
    artifact = append_forward_prediction(
        _campaign(), _prediction(), evidence_class=EVIDENCE_REAL
    )
    summary = build_forward_evidence_summary(artifact)
    assert summary["evidence_state"] == FORWARD_EVIDENCE_ACCUMULATING
    assert summary["completeness"]["initial_captured"] == 1
    assert summary["completeness"]["initial_missed"] == 1
    assert summary["completeness"]["unsettled"] == 1
    assert summary["metrics"]["overall"]["sample_count"] == 0
    assert summary["safety"]["automatic_promotion"] is False


def test_settlement_append_and_initial_refinement_metrics_stay_separate():
    artifact = append_forward_prediction(
        _campaign(), _prediction(), evidence_class=EVIDENCE_REAL
    )
    initial = artifact.records[0]
    artifact = append_forward_settlement(artifact, _settlement(initial))
    artifact = append_forward_prediction(
        artifact,
        _prediction("uefa-nl:campaign-002", "refinement"),
        evidence_class=EVIDENCE_REAL,
    )
    summary = build_forward_evidence_summary(artifact)
    assert summary["completeness"]["settled"] == 1
    assert summary["completeness"]["unsettled"] == 1
    assert summary["metrics"]["overall"]["sample_count"] == 1
    assert summary["metrics"]["INITIAL"]["sample_count"] == 1
    assert summary["metrics"]["REFINEMENT"]["sample_count"] == 0


def test_synthetic_evidence_is_explicitly_excluded_from_real_metrics():
    artifact = append_forward_prediction(
        _campaign(), _prediction(), evidence_class=EVIDENCE_SYNTHETIC
    )
    artifact = append_forward_settlement(artifact, _settlement(artifact.records[0]))
    summary = build_forward_evidence_summary(artifact)
    assert summary["evidence_state"] == NO_FORWARD_EVIDENCE
    assert summary["completeness"]["synthetic_predictions_excluded"] == 1
    assert summary["completeness"]["settled"] == 0
    assert summary["metrics"]["overall"]["sample_count"] == 0


def test_model_and_evaluation_contract_mutation_is_rejected_after_prediction():
    artifact = append_forward_prediction(
        _campaign(), _prediction(), evidence_class=EVIDENCE_REAL
    )
    with pytest.raises(ForwardCampaignError, match="frozen"):
        update_forward_campaign(artifact, model_digest_value="c" * 64)
    with pytest.raises(ForwardCampaignError, match="frozen"):
        update_forward_campaign(artifact)
    with pytest.raises(ForwardCampaignError, match="frozen"):
        update_forward_campaign(artifact, promotion_criteria_digest="d" * 64)
    assert artifact.campaign.model_digest_value == model_digest()


def test_prediction_and_settlement_are_append_only():
    artifact = append_forward_prediction(
        _campaign(), _prediction(), evidence_class=EVIDENCE_REAL
    )
    changed = deepcopy(artifact.records[0])
    changed["probabilities"] = {"home": 0.4, "draw": 0.2, "away": 0.4}
    with pytest.raises(ForwardCampaignError, match="append-only"):
        append_forward_prediction(artifact, changed, evidence_class=EVIDENCE_REAL)
    artifact = append_forward_settlement(artifact, _settlement(artifact.records[0]))
    with pytest.raises(ForwardCampaignError, match="append-only"):
        append_forward_settlement(artifact, _settlement(artifact.records[0]))


def test_criteria_state_is_external_frozen_review_only():
    artifact = append_forward_prediction(
        create_forward_campaign(
            campaign_id="nl-forward-criteria",
            edition="2024/25",
            campaign_start="2026-09-30T10:00:00Z",
            fixture_manifest=_manifest(),
            fixture_manifest_digest=TIMELINE_DIGEST,
            promotion_criteria_digest="d" * 64,
        ),
        _prediction(),
        evidence_class=EVIDENCE_REAL,
    )
    with pytest.raises(ForwardCampaignError, match="frozen"):
        build_forward_evidence_summary(
            artifact,
            criteria_evaluation={
                "state": PROMOTION_REVIEW_ELIGIBLE,
                "criteria_digest": "d" * 64,
            },
        )
    insufficient = build_forward_evidence_summary(
        artifact,
        criteria_evaluation={
            "state": INSUFFICIENT_FORWARD_EVIDENCE,
            "frozen": True,
            "criteria_digest": "d" * 64,
        },
    )
    assert insufficient["evidence_state"] == INSUFFICIENT_FORWARD_EVIDENCE
    eligible = build_forward_evidence_summary(
        artifact,
        criteria_evaluation={
            "state": PROMOTION_REVIEW_ELIGIBLE,
            "frozen": True,
            "human_review_required": True,
            "criteria_digest": "d" * 64,
        },
    )
    assert eligible["evidence_state"] == PROMOTION_REVIEW_ELIGIBLE


def test_summary_and_operator_view_are_deterministic():
    summary = build_forward_evidence_summary(_campaign())
    serialized = serialize_forward_summary(summary)
    assert summary["summary_digest"] in serialized
    view = render_forward_operator_view(summary)
    assert "NO_FORWARD_EVIDENCE" in view
    assert "automatic promotion disabled" in view

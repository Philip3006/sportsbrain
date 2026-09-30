"""Deterministic governance tests for the Nations League forward campaign."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

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
MANIFEST_DIGEST = json.loads(
    Path("results/audits/nations_league_forward_fixture_manifest.json").read_text()
)["manifest_digest"]


def _manifest():
    windows = {
        "initial": {
            "start_utc": "2026-10-01T18:00:00Z",
            "end_utc": "2026-10-01T22:00:00Z",
        },
        "refinement": {
            "start_utc": "2026-10-02T18:00:00Z",
            "end_utc": "2026-10-02T19:00:00Z",
        },
    }
    return [
        {
            "fixture_id": "uefa-nl:campaign-001",
            "edition": "2024/25",
            "initial_eligible": True,
            "refinement_eligible": True,
            "capture_windows": windows,
        },
        {
            "fixture_id": "uefa-nl:campaign-002",
            "edition": "2024/25",
            "initial_eligible": True,
            "refinement_eligible": True,
            "capture_windows": windows,
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
        fixture_manifest_digest=MANIFEST_DIGEST,
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


def _accounting_campaign():
    windows = {
        "initial": {
            "start_utc": "2026-09-30T18:00:00Z",
            "end_utc": "2026-09-30T20:00:00Z",
        },
        "refinement": {
            "start_utc": "2026-10-01T18:00:00Z",
            "end_utc": "2026-10-01T19:00:00Z",
        },
    }
    future_windows = {
        "initial": {
            "start_utc": "2026-10-01T18:00:00Z",
            "end_utc": "2026-10-01T20:00:00Z",
        },
        "refinement": {
            "start_utc": "2026-10-02T18:00:00Z",
            "end_utc": "2026-10-02T19:00:00Z",
        },
    }
    closed_windows = {
        "initial": {
            "start_utc": "2026-09-30T12:00:00Z",
            "end_utc": "2026-09-30T14:00:00Z",
        },
        "refinement": {
            "start_utc": "2026-09-30T14:00:00Z",
            "end_utc": "2026-09-30T15:00:00Z",
        },
    }
    before_campaign_windows = {
        "initial": {
            "start_utc": "2026-09-30T08:00:00Z",
            "end_utc": "2026-09-30T09:00:00Z",
        },
        "refinement": {
            "start_utc": "2026-09-30T08:30:00Z",
            "end_utc": "2026-09-30T09:00:00Z",
        },
    }
    return create_forward_campaign(
        campaign_id="nl-forward-accounting",
        edition="2024/25",
        campaign_start="2026-09-30T10:00:00Z",
        fixture_manifest=[
            {
                "fixture_id": "uefa-nl:campaign-001",
                "edition": "2024/25",
                "initial_eligible": True,
                "refinement_eligible": True,
                "capture_windows": windows,
            },
            {
                "fixture_id": "uefa-nl:campaign-open",
                "edition": "2024/25",
                "initial_eligible": True,
                "refinement_eligible": True,
                "capture_windows": future_windows,
            },
            {
                "fixture_id": "uefa-nl:campaign-closed",
                "edition": "2024/25",
                "initial_eligible": True,
                "refinement_eligible": True,
                "capture_windows": closed_windows,
            },
            {
                "fixture_id": "uefa-nl:campaign-before",
                "edition": "2024/25",
                "initial_eligible": False,
                "refinement_eligible": False,
                "capture_windows": before_campaign_windows,
            },
            {
                "fixture_id": "uefa-nl:campaign-exception",
                "edition": "2024/25",
                "initial_eligible": False,
                "refinement_eligible": False,
                "exception": "administrative",
            },
        ],
        fixture_manifest_digest="a" * 64,
    )


def test_zero_real_samples_is_explicit_and_does_not_hide_exceptions():
    assert _campaign().campaign.model_version == "nations_league_v1_1"
    assert _campaign().campaign.fixture_manifest_digest == MANIFEST_DIGEST
    summary = build_forward_evidence_summary(_campaign())
    assert summary["evidence_state"] == NO_FORWARD_EVIDENCE
    assert summary["completeness"] == {
        "eligible_fixtures": 2,
        "initial_eligible": 2,
        "initial_captured": 0,
        "initial_pending": 2,
        "initial_due": 0,
        "initial_missed": 0,
        "refinement_eligible": 2,
        "refinement_captured": 0,
        "refinement_pending": 2,
        "refinement_due": 0,
        "refinement_missed": 0,
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
    summary = build_forward_evidence_summary(artifact, as_of="2026-10-01T20:00:00Z")
    assert summary["evidence_state"] == FORWARD_EVIDENCE_ACCUMULATING
    assert summary["completeness"]["initial_captured"] == 1
    assert summary["completeness"]["initial_due"] == 1
    assert summary["completeness"]["initial_missed"] == 0
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
    summary = build_forward_evidence_summary(artifact, as_of="2026-10-02T20:00:00Z")
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
            fixture_manifest_digest=MANIFEST_DIGEST,
            promotion_criteria_digest="d" * 64,
        ),
        _prediction(),
        evidence_class=EVIDENCE_REAL,
    )
    with pytest.raises(ForwardCampaignError, match="frozen"):
        build_forward_evidence_summary(
            artifact,
            as_of="2026-10-01T20:00:00Z",
            criteria_evaluation={
                "state": PROMOTION_REVIEW_ELIGIBLE,
                "criteria_digest": "d" * 64,
            },
        )
    insufficient = build_forward_evidence_summary(
        artifact,
        as_of="2026-10-01T20:00:00Z",
        criteria_evaluation={
            "state": INSUFFICIENT_FORWARD_EVIDENCE,
            "frozen": True,
            "criteria_digest": "d" * 64,
        },
    )
    assert insufficient["evidence_state"] == INSUFFICIENT_FORWARD_EVIDENCE
    eligible = build_forward_evidence_summary(
        artifact,
        as_of="2026-10-01T20:00:00Z",
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


def test_lifecycle_accounting_distinguishes_due_pending_missed_and_exceptions():
    artifact = _accounting_campaign()
    summary = build_forward_evidence_summary(artifact, as_of="2026-09-30T18:30:00Z")
    states = {row["fixture_id"]: row for row in summary["lifecycle_states"]}
    assert states["uefa-nl:campaign-001"]["initial"] == "DUE"
    assert states["uefa-nl:campaign-open"]["initial"] == "PENDING"
    assert states["uefa-nl:campaign-closed"]["initial"] == "MISSED"
    assert states["uefa-nl:campaign-before"]["initial"] == "NOT_ELIGIBLE"
    assert states["uefa-nl:campaign-exception"]["initial"] == "EXCEPTION"
    assert summary["completeness"]["initial_due"] == 1
    assert summary["completeness"]["initial_pending"] == 1
    assert summary["completeness"]["initial_missed"] == 1


def test_captured_state_takes_precedence_and_real_summary_requires_as_of():
    artifact = append_forward_prediction(
        _campaign(), _prediction(), evidence_class=EVIDENCE_REAL
    )
    with pytest.raises(ForwardCampaignError, match="explicit as_of"):
        build_forward_evidence_summary(artifact)
    summary = build_forward_evidence_summary(artifact, as_of="2026-10-01T20:00:00Z")
    states = {row["fixture_id"]: row for row in summary["lifecycle_states"]}
    assert states["uefa-nl:campaign-001"]["initial"] == "CAPTURED"


@pytest.mark.parametrize(
    "campaign_start,fixture_id,match",
    [
        (
            "2026-10-01T20:30:00Z",
            "uefa-nl:campaign-001",
            "precedes campaign_start",
        ),
        (
            "2026-09-30T10:00:00Z",
            "uefa-nl:campaign-before",
            "not campaign-eligible",
        ),
    ],
)
def test_real_prediction_window_and_campaign_responsibility_are_enforced(
    campaign_start, fixture_id, match
):
    base = (
        _campaign().campaign
        if match == "precedes campaign_start"
        else _accounting_campaign().campaign
    )
    campaign = create_forward_campaign(
        campaign_id=f"nl-forward-window-{fixture_id.split(':')[-1]}",
        edition=base.edition,
        campaign_start=campaign_start,
        fixture_manifest=base.fixture_manifest,
        fixture_manifest_digest="a" * 64,
    )
    with pytest.raises(ForwardCampaignError, match=match):
        append_forward_prediction(
            campaign, _prediction(fixture_id), evidence_class=EVIDENCE_REAL
        )


def test_real_prediction_outside_capture_window_is_rejected():
    prediction = _prediction()
    prediction["prediction_timestamp"] = "2026-10-01T17:59:59Z"
    with pytest.raises(ValueError):
        append_forward_prediction(_campaign(), prediction, evidence_class=EVIDENCE_REAL)

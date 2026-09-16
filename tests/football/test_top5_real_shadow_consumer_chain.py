"""Synthetic end-to-end proof for the first real-shadow consumer seam."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.top5_real_shadow_session import main as session_cli
from src.football.top5_b2_shadow_qualification_intake import run_intake
from src.football.top5_real_shadow_audit import audit_session_payload
from src.football.top5_real_shadow_contracts import (
    M5_CANDIDATE_ID,
    NormalizedProviderObservation,
    RealShadowContractError,
)
from src.football.top5_real_shadow_measurement import measure_session_payload
from src.football.top5_real_shadow_session import RealShadowSession
from src.football.top5_real_shadow_session_evidence import build_shadow_evidence
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA
from tests.football.test_top5_b2_shadow_qualification_intake import _manifest
from tests.football.test_top5_real_shadow_session import (
    BASE,
    INTEGRATION_SHA,
    closing_attachment,
    final_result,
)


def _intake_directory(tmp_path: Path) -> Path:
    result = run_intake(_manifest(), tmp_path / "b2-evidence")
    assert result.artifact_directory is not None
    return Path(result.artifact_directory)


def test_builder2_package_reaches_complete_measurement_chain(tmp_path: Path) -> None:
    intake_dir = _intake_directory(tmp_path)
    session_path = tmp_path / "shadow" / "session.json"
    evidence_path = tmp_path / "shadow" / "evidence.json"

    assert (
        session_cli(
            [
                "--b2-intake-dir",
                str(intake_dir),
                "--session-key",
                "shadow-session:consumer-chain",
                "--integration-sha",
                INTEGRATION_SHA,
                "--experiment-id",
                "shadow-experiment:consumer-chain-v1",
                "--created-at",
                BASE.isoformat(),
                "--min-lead-minutes",
                "30",
                "--max-lead-minutes",
                "180",
                "--max-odds-age-seconds",
                "300",
                "--kickoff-tolerance-seconds",
                "0",
                "--output",
                str(session_path),
                "--evidence-output",
                str(evidence_path),
            ]
        )
        == 0
    )

    session = RealShadowSession.from_payload(json.loads(session_path.read_text()))
    prediction = next(iter(session.predictions.values()))
    session.attach_result(final_result(prediction, "consumer-chain"))
    session.attach_closing(closing_attachment(prediction, "consumer-chain"))
    session_payload = session.as_payload()
    evidence = build_shadow_evidence(session)
    evidence_path.write_text(json.dumps(evidence, sort_keys=True))

    audit = audit_session_payload(session_payload, evidence_bundle=evidence)
    measurement = measure_session_payload(session_payload, evidence_bundle=evidence)
    observation = session_payload["observations"][0]
    canonical = observation["canonical_observation"]
    receipt = observation["independent_validation"]
    audit_item = audit["predictions"][0]
    measured = measurement["eligible_predictions"][0]

    assert canonical["evidence_kind"] == "REAL_OBSERVED"
    for field in (
        "controlled_shadow_run_id",
        "qualification_session_id",
        "ceo_authorization_id",
        "fixture_key",
        "provider_identity",
        "provider_event_id",
        "provider_request_id",
        "observation_id",
        "observation_digest",
        "cascade_evidence_digest",
        "capture_attestation_digest",
        "qualification_receipt_id",
        "receipt_digest",
    ):
        assert receipt[field]
    assert receipt["fixture_key"] == canonical["fixture_key"]
    assert receipt["provider_identity"] == canonical["provider_identity"]
    assert receipt["provider_event_id"] == canonical["provider_event_id"]
    assert receipt["provider_request_id"] == canonical["provider_request_id"]
    assert receipt["observation_id"] == canonical["observation_id"]

    assert session_payload["session"]["research_sha"] == FROZEN_RESEARCH_SHA
    assert session_payload["session"]["model_identity"] == M5_CANDIDATE_ID
    assert session_payload["session"]["no_bet"] is True
    assert session_payload["session"]["publication"] is False
    assert prediction.model_identity == M5_CANDIDATE_ID
    assert prediction.research_sha == FROZEN_RESEARCH_SHA
    assert prediction.session_id == session.session_id
    assert prediction.fixture_key == canonical["fixture_key"]

    assert audit["overall_state"] == "COMPLETE"
    assert audit_item["completeness"]["AUDIT_COMPLETE"] is True
    assert audit_item["qualification_receipt_id"] == receipt["qualification_receipt_id"]
    assert audit_item["controlled_shadow_run_id"] == receipt["controlled_shadow_run_id"]
    assert audit_item["qualification_session_id"] == receipt["qualification_session_id"]
    assert audit_item["ceo_authorization_id"] == receipt["ceo_authorization_id"]
    assert audit_item["observation_digest"] == receipt["observation_digest"]
    assert audit["deterministic_audit_digest"]

    assert measurement["overall_state"] == "COMPLETE"
    assert measurement["eligible_count"] == 1
    assert measurement["measurement_id"]
    assert measurement["measurement_digest"]
    assert measured["prediction_id"] == prediction.prediction_id
    for field in (
        "qualification_receipt_id",
        "qualification_receipt_digest",
        "observation_id",
        "observation_digest",
        "normalized_record_digest",
        "cascade_evidence_digest",
        "capture_attestation_digest",
        "ceo_authorization_id",
        "provider_event_id",
        "provider_request_id",
    ):
        receipt_field = (
            "receipt_digest" if field == "qualification_receipt_digest" else field
        )
        assert measured[field] == receipt[receipt_field]
    assert measured["audit_digest"] == audit["deterministic_audit_digest"]
    assert measurement["closing_benchmark"]["label"] == (
        "BENCHMARK / CLV MEASUREMENT ONLY"
    )
    assert (
        measurement["closing_benchmark"]["observations"][0]["used_for_prediction"]
        is False
    )
    assert measurement["safety_invariants"] == {
        "production_activation_authorized": False,
        "publication_authorized": False,
        "betting_authorized": False,
        "model_approved_for_production": False,
        "signal_time_approved_for_production": False,
        "read_only": True,
        "network_accessed": False,
        "provider_ranking_emitted": False,
        "closing_used_for_prediction": False,
        "sealed_data_accessed": False,
    }


@pytest.mark.parametrize("marker", ("TEST_FIXTURE", "MOCK", "OFFLINE_REPLAY"))
def test_non_real_builder2_package_cannot_be_upgraded(
    marker: str, tmp_path: Path
) -> None:
    intake_dir = _intake_directory(tmp_path)
    manifest = json.loads((intake_dir / "manifest.json").read_text())
    receipt = json.loads((intake_dir / "receipt.json").read_text())
    observation = deepcopy(manifest["observation"])
    observation["evidence_kind"] = marker

    with pytest.raises(RealShadowContractError):
        NormalizedProviderObservation.from_builder2_package(observation, receipt)


def test_tampered_builder2_binding_fails_closed(tmp_path: Path) -> None:
    intake_dir = _intake_directory(tmp_path)
    manifest = json.loads((intake_dir / "manifest.json").read_text())
    receipt = json.loads((intake_dir / "receipt.json").read_text())
    receipt["provider_request_id"] = "request-tampered"

    with pytest.raises(RealShadowContractError):
        NormalizedProviderObservation.from_builder2_package(
            manifest["observation"], receipt
        )

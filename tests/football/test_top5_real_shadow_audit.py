"""Regression coverage for the read-only Top-5 evidence auditor."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from src.football.top5_qualification_sample_aggregator import (
    aggregate_builder2_qualification_samples,
)
from src.football.top5_real_shadow_audit import (
    AuditStatus,
    ShadowAuditError,
    audit_directory_payloads,
    audit_prediction_payload,
    audit_session_payload,
    load_json,
)
from src.football.top5_real_shadow_contracts import (
    OFFLINE_REPLAY_MARKER,
    REAL_OBSERVED_MARKER,
)
from src.football.top5_real_shadow_session_evidence import build_shadow_evidence
from tests.football.test_top5_real_shadow_session import (
    closing_attachment,
    final_result,
    make_session,
    observation,
)


def _complete_payload() -> tuple[dict, dict]:
    session = make_session(scope=("EPL",), fixture_mode=False)
    session.record_observation(observation(mode=REAL_OBSERVED_MARKER))
    session.finalize_predictions()
    prediction = next(iter(session.predictions.values()))
    session.attach_result(final_result(prediction))
    session.attach_closing(closing_attachment(prediction))
    return session.as_payload(), build_shadow_evidence(session)


def _awaiting_payload() -> dict:
    session = make_session(scope=("EPL",), fixture_mode=False)
    session.record_observation(observation(mode=REAL_OBSERVED_MARKER))
    session.finalize_predictions()
    return session.as_payload()


def test_complete_real_observed_chain_is_audit_complete() -> None:
    payload, evidence = _complete_payload()
    report = audit_session_payload(payload, evidence_bundle=evidence)
    item = report["predictions"][0]

    assert report["overall_state"] == AuditStatus.COMPLETE.value
    assert item["overall_state"] == AuditStatus.COMPLETE.value
    assert all(item["completeness"].values())
    assert item["qualification_receipt_id"].startswith("b2qr-")
    assert item["controlled_shadow_run_id"]
    assert item["ceo_authorization_id"]
    assert report["shadow_evidence_bundle"] == {"present": True, "valid": True}


def test_missing_receipt_is_qualification_missing() -> None:
    payload = _awaiting_payload()
    payload["observations"][0]["independent_validation"] = None
    report = audit_session_payload(payload)

    assert (
        report["predictions"][0]["overall_state"]
        == AuditStatus.QUALIFICATION_MISSING.value
    )
    assert report["predictions"][0]["completeness"]["QUALIFICATION_PRESENT"] is False


@pytest.mark.parametrize(
    "field",
    (
        "fixture_key",
        "provider_identity",
        "provider_event_id",
        "provider_request_id",
        "observation_id",
        "observation_digest",
        "normalized_record_digest",
        "cascade_evidence_digest",
        "capture_attestation_digest",
        "controlled_shadow_run_id",
        "qualification_session_id",
        "ceo_authorization_id",
        "adapter_version",
        "adapter_source_sha",
    ),
)
def test_receipt_binding_tampering_is_provenance_mismatch(field: str) -> None:
    payload = _awaiting_payload()
    receipt = payload["observations"][0]["independent_validation"]
    receipt[field] = "f" * 64 if field.endswith(("digest", "sha")) else "changed"

    report = audit_session_payload(payload)

    assert (
        report["predictions"][0]["overall_state"]
        == AuditStatus.PROVENANCE_MISMATCH.value
    )


def test_receipt_for_observation_a_cannot_admit_observation_b() -> None:
    payload = _awaiting_payload()
    canonical = payload["observations"][0]["canonical_observation"]
    canonical["fixture_key"] = "BL1|foreign|Home|Away"

    report = audit_session_payload(payload)

    assert (
        report["predictions"][0]["overall_state"]
        == AuditStatus.PROVENANCE_MISMATCH.value
    )


def test_test_fixture_is_non_real_and_offline_replay_is_rejected() -> None:
    fixture_session = make_session(fixture_mode=True)
    fixture_session.record_observation(observation())
    fixture_session.finalize_predictions()
    fixture_prediction = next(iter(fixture_session.predictions.values()))
    fixture_session.attach_result(final_result(fixture_prediction))
    fixture_session.attach_closing(closing_attachment(fixture_prediction))
    fixture_report = audit_session_payload(fixture_session.as_payload())
    assert (
        fixture_report["predictions"][0]["overall_state"]
        == AuditStatus.NON_REAL_EVIDENCE.value
    )

    offline = _awaiting_payload()
    offline["observations"][0]["observation_mode"] = OFFLINE_REPLAY_MARKER
    offline_report = audit_session_payload(offline)
    assert (
        offline_report["predictions"][0]["overall_state"]
        == AuditStatus.NON_REAL_EVIDENCE.value
    )


def test_missing_and_non_final_results_are_pending() -> None:
    missing = audit_session_payload(_awaiting_payload())
    assert (
        missing["predictions"][0]["overall_state"] == AuditStatus.PENDING_RESULT.value
    )

    session = make_session(scope=("EPL",), fixture_mode=False)
    session.record_observation(observation(mode=REAL_OBSERVED_MARKER))
    session.finalize_predictions()
    prediction = next(iter(session.predictions.values()))
    from src.football.top5_real_shadow_attachments import (
        RealShadowResultAttachment,
        RealShadowResultStatus,
    )

    session.attach_result(
        RealShadowResultAttachment(
            prediction_id=prediction.prediction_id,
            prediction_artifact_sha=prediction.artifact_sha,
            fixture_key=prediction.fixture_key,
            league_code=prediction.league_code,
            result_source="provider-results",
            provider_result_id=None,
            result_timestamp=prediction.captured_at,
            attached_at=prediction.captured_at,
            status=RealShadowResultStatus.POSTPONED,
            home_score=None,
            away_score=None,
            actual_outcome=None,
        )
    )
    non_final = audit_session_payload(session.as_payload())
    assert (
        non_final["predictions"][0]["overall_state"] == AuditStatus.PENDING_RESULT.value
    )


def test_missing_closing_is_pending_after_final_result() -> None:
    payload = _complete_payload()[0]
    payload["closings"] = []
    report = audit_session_payload(payload)
    assert (
        report["predictions"][0]["overall_state"] == AuditStatus.PENDING_CLOSING.value
    )


def test_conflicting_attachments_and_closing_leakage_fail_closed() -> None:
    payload, _ = _complete_payload()
    payload["results"].append(deepcopy(payload["results"][0]))
    payload["closings"][0]["used_for_prediction"] = True
    report = audit_session_payload(payload)
    item = report["predictions"][0]

    assert item["overall_state"] in {
        AuditStatus.CONFLICT.value,
        AuditStatus.PROVENANCE_MISMATCH.value,
    }
    assert any(finding["code"] == "CONFLICT" for finding in item["findings"])
    assert any("closing" in finding["message"] for finding in item["findings"])


def test_evidence_bundle_must_bind_to_session_and_exact_attachment_digests() -> None:
    payload, evidence = _complete_payload()
    evidence["predictions"][0]["provenance"]["artifact_sha"] = "f" * 64
    report = audit_session_payload(payload, evidence_bundle=evidence)
    assert report["overall_state"] == AuditStatus.INCOMPLETE.value
    assert any(
        finding["code"] == "DIGEST_MISMATCH"
        for finding in report["predictions"][0]["findings"]
    )

    payload, evidence = _complete_payload()
    evidence["real_shadow_session"]["integration_sha"] = "a" * 40
    report = audit_session_payload(payload, evidence_bundle=evidence)
    assert report["overall_state"] == AuditStatus.INCOMPLETE.value
    assert any(
        finding["code"] == "IDENTITY_MISMATCH"
        for finding in report["predictions"][0]["findings"]
    )


def test_prediction_digest_and_frozen_binding_mismatches_are_distinguished() -> None:
    digest_payload, _ = _complete_payload()
    digest_payload["predictions"][0]["artifact_sha"] = "f" * 64
    digest_report = audit_session_payload(digest_payload)
    assert (
        digest_report["predictions"][0]["overall_state"]
        == AuditStatus.DIGEST_MISMATCH.value
    )

    research_payload, _ = _complete_payload()
    research_payload["predictions"][0]["research_sha"] = "a" * 40
    research_report = audit_session_payload(research_payload)
    assert (
        research_report["predictions"][0]["overall_state"]
        == AuditStatus.PROVENANCE_MISMATCH.value
    )


def test_optional_sample_report_is_validated_without_issuing_authority() -> None:
    payload, _ = _complete_payload()
    receipt = payload["observations"][0]["independent_validation"]
    sample = aggregate_builder2_qualification_samples([receipt]).as_payload()
    report = audit_session_payload(payload, sample_report=sample)

    assert report["qualification_sample_report"] == {"present": True, "valid": True}


def test_directory_and_digest_are_deterministic_and_counts_are_evidence_only() -> None:
    payload, evidence = _complete_payload()
    first = audit_directory_payloads([payload, evidence])
    second = audit_directory_payloads(
        [
            json.loads(json.dumps(payload)),
            json.loads(json.dumps(evidence, default=dict)),
        ]
    )

    assert first == second
    assert first["summary"]["real_observed"] == 1
    assert first["summary"]["complete"] == 1
    assert first["summary"]["per_league"] == {"EPL": 1}


def test_standalone_prediction_never_infers_missing_lifecycle_evidence() -> None:
    payload, _ = _complete_payload()
    report = audit_prediction_payload(payload["predictions"][0])

    assert report["overall_state"] == AuditStatus.INCOMPLETE.value
    assert report["predictions"][0]["completeness"]["OBSERVATION_PRESENT"] is False
    assert report["predictions"][0]["completeness"]["AUDIT_COMPLETE"] is False


def test_auditor_has_no_authority_issuer_or_network_or_ledger_path() -> None:
    import src.football.top5_real_shadow_audit as audit_module

    assert not hasattr(audit_module, "issue_builder2_qualification_receipt")
    assert "requests" not in audit_module.__dict__
    with pytest.raises(ShadowAuditError, match="ledger"):
        load_json(__import__("pathlib").Path("/tmp/ledger/shadow.json"))

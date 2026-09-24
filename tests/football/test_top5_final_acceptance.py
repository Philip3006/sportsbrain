"""Offline tests for the read-only Top-5 final acceptance composition gate."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.odds.therundown import THERUNDOWN_BASE_URL, THERUNDOWN_PROVIDER_NAME
from src.football.top5_final_acceptance import (
    ACTIVE_PROVIDER,
    CANDIDATE_PROVIDER,
    FINAL_ACCEPTANCE_SCHEMA_VERSION,
    STATUS_VERIFIED,
    Top5FinalAcceptanceError,
    canonical_digest,
    verify_final_acceptance,
)
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA, M5_CANDIDATE_ID
from src.football.top5_therundown_event_discovery import (
    B4_QUOTA_PROOF_AFFILIATE_IDS,
    B4_QUOTA_PROOF_EXECUTION_PHASE,
    B4_QUOTA_PROOF_MAX_DATAPOINTS,
    B4_QUOTA_PROOF_PACKAGE_SCHEMA_VERSION,
    B4_QUOTA_PROOF_SCHEMA_VERSION,
    B4_QUOTA_PROOF_SPORT_ID,
    TheRundownEventDiscoveryEvidenceV1,
)
from src.football.top5_therundown_network_shadow import (
    TheRundownQuotaHeadroomEvidenceV1,
)
from tests.football.test_top5_candidate_provider_eligibility import (
    _candidate_network_run,
)
from tests.football.test_top5_public_delivery import _controlled_public_product
from tests.football.test_top5_therundown_network_shadow import (
    NOW,
    _quota_headroom,
)
from tests.football.test_top5_therundown_network_shadow import (
    _authorization as network_authorization,
)

SOURCE_SHA = "1" * 40
MODEL_ARTIFACT_HASH = "2" * 64
NOW_ACCEPTANCE = NOW


def _b4_package() -> dict[str, object]:
    finished = NOW - timedelta(minutes=1)
    started = finished - timedelta(seconds=1)
    snapshot_date = NOW.date().isoformat()
    request_shape = {
        "method": "GET",
        "endpoint": f"{THERUNDOWN_BASE_URL}/sports/{B4_QUOTA_PROOF_SPORT_ID}/events/{snapshot_date}",
        "query": {
            "affiliate_ids": ",".join(B4_QUOTA_PROOF_AFFILIATE_IDS),
            "hide_closed": "true",
            "main_line": "true",
            "market_ids": "1",
        },
    }
    request_shape_digest = canonical_digest(request_shape)
    proof: dict[str, object] = {
        "schema_version": B4_QUOTA_PROOF_SCHEMA_VERSION,
        "execution_phase": B4_QUOTA_PROOF_EXECUTION_PHASE,
        "proof_id": "b4-proof-final-acceptance",
        "provider": THERUNDOWN_PROVIDER_NAME,
        "sport_id": B4_QUOTA_PROOF_SPORT_ID,
        "snapshot_date": snapshot_date,
        "account_scope": "offline-test-account",
        "authorization_package_digest": "a" * 64,
        "configuration_digest": "b" * 64,
        "authorization_id": "b4-proof-authorization",
        "controlled_shadow_run_id": "quota-proof-only",
        "qualification_session_id": "quota-proof-only",
        "ceo_authorization_identity": "ceo:offline-test",
        "request_shape_digest": request_shape_digest,
        "credential_binding_digest": "c" * 64,
        "request_started_at": started.isoformat(),
        "response_finished_at": finished.isoformat(),
        "billed_datapoints": B4_QUOTA_PROOF_MAX_DATAPOINTS,
        "remaining_datapoints": 550,
        "quota_used_datapoints": 450,
        "quota_limit_datapoints": 1000,
        "quota_period": "daily",
        "quota_reset_at": (NOW + timedelta(hours=1)).isoformat(),
        "raw_header_evidence": {
            "x-datapoints": str(B4_QUOTA_PROOF_MAX_DATAPOINTS),
            "x-datapoints-used": "450",
            "x-datapoints-remaining": "550",
            "x-datapoints-limit": "1000",
            "x-datapoints-period": "daily",
            "x-datapoints-reset": (NOW + timedelta(hours=1)).isoformat(),
            "x-tier": "free",
            "x-rate-limit": "1",
            "x-data-delay-seconds": "0",
        },
        "response_digest": "d" * 64,
        "status_code": 200,
        "request_count": 1,
        "retry_count": 0,
        "no_retry": True,
    }
    proof["evidence_digest"] = canonical_digest(proof)
    return {
        "schema_version": B4_QUOTA_PROOF_PACKAGE_SCHEMA_VERSION,
        "execution_phase": B4_QUOTA_PROOF_EXECUTION_PHASE,
        "proof": proof,
        "request": {
            "proof_id": proof["proof_id"],
            "provider": THERUNDOWN_PROVIDER_NAME,
            "sport_id": B4_QUOTA_PROOF_SPORT_ID,
            "snapshot_date": snapshot_date,
            "authorization_package_digest": proof["authorization_package_digest"],
            "configuration_digest": proof["configuration_digest"],
            "authorization_id": proof["authorization_id"],
            "controlled_shadow_run_id": proof["controlled_shadow_run_id"],
            "qualification_session_id": proof["qualification_session_id"],
            "ceo_authorization_identity": proof["ceo_authorization_identity"],
            "adapter_version": "therundown-v2-experimental:2",
            "adapter_source_sha": "e" * 40,
            "endpoint": request_shape["endpoint"],
            "query": request_shape["query"],
            "request_shape_digest": request_shape_digest,
            "maximum_datapoints": B4_QUOTA_PROOF_MAX_DATAPOINTS,
            "request_count": 1,
            "retry_count": 0,
        },
        "spend_control": {
            "provider": THERUNDOWN_PROVIDER_NAME,
            "evidence_kind": "provider_response_headers",
            "account_tier": "free",
            "overage_exposure": "none",
            "observed_at": finished.isoformat(),
            "digest": "f" * 64,
        },
        "safety": {
            "five_league_requests": 0,
            "receipt_issued": False,
            "authority_changed": False,
            "activation": False,
            "publication": False,
            "betting": False,
            "monetary_spend_authorized": False,
        },
    }


def _headroom(run_id: str, session_id: str, authorization_id: str) -> dict[str, object]:
    authorization, _ = _quota_headroom(
        network_authorization(
            _configuration_for_headroom(run_id, session_id, authorization_id),
            provider=CANDIDATE_PROVIDER,
        )
    )
    evidence = TheRundownQuotaHeadroomEvidenceV1(
        provider=CANDIDATE_PROVIDER,
        account_scope="offline-test-account",
        observed_remaining_datapoints=550,
        observed_at=NOW,
        provenance_source="offline-test",
        provenance_digest="a" * 64,
        authorization_package_digest="b" * 64,
        authorization_id=authorization.authorization_id,
        controlled_shadow_run_id=run_id,
        qualification_session_id=session_id,
        ceo_authorization_identity=authorization.ceo_authorization_identity,
        evidence_digest="0" * 64,
    )
    evidence = replace(evidence, evidence_digest=evidence.computed_evidence_digest)
    return evidence.as_payload()


def _configuration_for_headroom(run_id: str, session_id: str, authorization_id: str):
    del run_id, session_id, authorization_id
    from tests.football.test_top5_therundown_network_shadow import (
        _configuration,
        _targets,
    )

    targets = tuple(
        replace(target, provider=CANDIDATE_PROVIDER) for target in _targets()
    )
    return _configuration(targets=targets, enabled=True)


def _discovery(run) -> list[dict[str, object]]:
    records = []
    for capture in run.captures:
        target = capture.target
        request = capture.request
        item = TheRundownEventDiscoveryEvidenceV1(
            discovery_authorization_id="discovery-auth-final",
            discovery_authorization_digest="a" * 64,
            provider=CANDIDATE_PROVIDER,
            league=target.league,
            fixture_key=target.fixture_key,
            home_team=target.home_team,
            away_team=target.away_team,
            home_participant_id=request.home_participant_id,
            away_participant_id=request.away_participant_id,
            kickoff=target.kickoff,
            provider_event_id=target.provider_event_id,
            request_identity=request.request_identity,
            request_shape_digest="b" * 64,
            request_started_at=capture.response.request_started_at,
            response_completed_at=capture.response.request_finished_at,
            raw_response_digest="c" * 64,
            provider_event_evidence_digest="0" * 64,
            datapoints=55,
            remaining_datapoints=550,
            retry_count=0,
            network_execution=True,
        )
        records.append(
            replace(
                item,
                provider_event_evidence_digest=item.computed_provider_event_evidence_digest,
            ).as_payload()
        )
    return records


def _public(run_id: str, session_id: str, source_sha: str) -> dict[str, object]:
    worker = deepcopy(_controlled_public_product())
    release = worker["top5_release"]
    release.update(
        {
            "provider_authority": ACTIVE_PROVIDER,
            "controlled_shadow_run_id": run_id,
            "qualification_session_id": session_id,
            "model_identity": M5_CANDIDATE_ID,
            "league_codes": ["BL1", "EPL", "L1", "LL", "SA"],
            "generated_at": NOW.isoformat(),
            "published_at": NOW.isoformat(),
        }
    )
    for record in worker["football"]:
        record.update(
            {
                "provider": ACTIVE_PROVIDER,
                "source": ACTIVE_PROVIDER,
                "run_id": run_id,
                "session_id": session_id,
                "model_identity": M5_CANDIDATE_ID,
                "research_sha": FROZEN_RESEARCH_SHA,
                "source_sha": source_sha,
                "no_bet": True,
                "closing_used_for_prediction": False,
            }
        )
        record["provenance"].update(
            {
                "provider": ACTIVE_PROVIDER,
                "source": ACTIVE_PROVIDER,
                "source_sha": source_sha,
                "research_sha": FROZEN_RESEARCH_SHA,
                "controlled_shadow_run_id": run_id,
                "qualification_session_id": session_id,
            }
        )
    attestation = {
        "schema_version": "top5-delivery-attestation-v1",
        "attestation_digest": "a" * 64,
        "artifact_digest": "b" * 64,
        "generation_id": release["generation_id"],
        "activation_id": release["activation_id"],
        "provider_authority": ACTIVE_PROVIDER,
        "controlled_shadow_run_id": run_id,
        "qualification_session_id": session_id,
        "publication_authorization_id": release["publication_authorization_id"],
        "league_codes": release["league_codes"],
        "issued_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "publication_authorized": True,
        "no_bet": True,
        "capability_id": "capability-final-test",
        "capability_nonce_digest": "c" * 64,
    }
    capability = {
        "capability_id": "capability-final-test",
        "capability_nonce": "n" * 32,
    }
    manifest = {
        "public_product_digest": canonical_digest(worker),
        "static_payload_digest": canonical_digest(worker),
        "worker_payload_digest": canonical_digest(worker),
        "generation_id": release["generation_id"],
        "activation_id": release["activation_id"],
    }
    return {
        "worker_payload": worker,
        "static_payload": deepcopy(worker),
        "publication_attestation": attestation,
        "capability": capability,
        "delivery_manifest": manifest,
        "worker_status": 200,
        "pwa_status": 200,
    }


def _bundle() -> dict[str, object]:
    run, _configuration = _candidate_network_run()
    run_payload = run.as_payload()
    run_id = run.controlled_shadow_run_id
    session_id = run.qualification_session_id
    authorization_id = run.authorization_id
    return {
        "schema_version": FINAL_ACCEPTANCE_SCHEMA_VERSION,
        "source_main_sha": SOURCE_SHA,
        "b4_quota_proof_package": _b4_package(),
        "b4_quota_headroom": _headroom(run_id, session_id, authorization_id),
        "discovery_evidence": _discovery(run),
        "controlled_shadow": run_payload,
        "model_runtime": {
            "candidate_id": M5_CANDIDATE_ID,
            "model_identity": M5_CANDIDATE_ID,
            "research_sha": FROZEN_RESEARCH_SHA,
            "source_sha": SOURCE_SHA,
            "model_artifact_hash": MODEL_ARTIFACT_HASH,
            "signal_time_contract_id": "signal-time:shadow-v1",
            "prediction_input_kind": "signal_time",
            "closing_used_for_prediction": False,
            "production_model_approved": False,
            "signal_time_approved_for_production": False,
            "publication_authorized": False,
            "no_bet": True,
            "prediction_input_allowed": True,
        },
        "public": _public(run_id, session_id, SOURCE_SHA),
        "runtime_evidence": {
            "runtime_root": "/private/tmp/governed-runtime",
            "runtime_root_role": "governed-runtime",
            "checkout_clean": True,
            "publisher_clean": True,
            "health_authority": "governed",
            "health_status": "ok",
            "active_provider_order": [ACTIVE_PROVIDER],
            "source_release_sha": SOURCE_SHA,
            "runtime_data_sha": "3" * 64,
            "captured_at": NOW.isoformat(),
        },
    }


def _remove_public_record(bundle: dict[str, object]) -> None:
    worker = bundle["public"]["worker_payload"]
    static = bundle["public"]["static_payload"]
    worker["football"].pop()
    static["football"].pop()
    digest = canonical_digest(worker)
    bundle["public"]["delivery_manifest"].update(
        {
            "public_product_digest": digest,
            "static_payload_digest": digest,
            "worker_payload_digest": digest,
        }
    )


def test_complete_b4_to_public_acceptance_bundle_is_verified_without_side_effects():
    result = verify_final_acceptance(_bundle(), now=NOW_ACCEPTANCE)
    assert result["status"] == STATUS_VERIFIED
    assert result["manifest"]["provider_authority"] == ACTIVE_PROVIDER
    assert result["manifest"]["candidate_provider"] == CANDIDATE_PROVIDER
    assert result["manifest"]["checks"]["candidate_not_authority"] is True
    assert len(result["manifest"]["discovery_event_ids"]) == 5


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda bundle: bundle["public"]["worker_payload"]["top5_release"].update(
                {"league_codes": ["EPL"]}
            ),
            "Top-5",
        ),
        (_remove_public_record, "15 Top-5"),
        (
            lambda bundle: bundle["public"]["worker_payload"]["top5_release"].update(
                {"provider_authority": CANDIDATE_PROVIDER}
            ),
            "unexpected provider authority",
        ),
        (
            lambda bundle: bundle["controlled_shadow"]["captures"][0].update(
                {"evidence_kind": "TEST_FIXTURE"}
            ),
            "REAL_OBSERVED",
        ),
        (
            lambda bundle: bundle["runtime_evidence"].update(
                {"active_provider_order": [CANDIDATE_PROVIDER]}
            ),
            "provider authority",
        ),
        (
            lambda bundle: bundle["model_runtime"].update(
                {"closing_used_for_prediction": True}
            ),
            "closing",
        ),
    ],
)
def test_final_acceptance_fails_closed_for_unsafe_or_incomplete_inputs(mutate, message):
    bundle = _bundle()
    mutate(bundle)
    with pytest.raises(Top5FinalAcceptanceError, match=message):
        verify_final_acceptance(bundle, now=NOW_ACCEPTANCE)


def test_current_main_binding_is_required():
    with pytest.raises(Top5FinalAcceptanceError, match="current main"):
        verify_final_acceptance(
            _bundle(), now=NOW_ACCEPTANCE, expected_source_main_sha="4" * 40
        )


def test_cli_input_is_digestable_without_network_or_writes():
    bundle = _bundle()
    assert len(canonical_digest(bundle)) == 64

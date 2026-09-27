"""Regression tests for the side-effect-free Top-5 public-read boundary."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from scripts.top5_public_acceptance import main as acceptance_cli
from scripts.top5_public_production_verification import verify_public_captures
from src.football.top5_public_acceptance import (
    TOP5_PUBLIC_DELIVERY_BLOCKED,
    TOP5_PUBLIC_DELIVERY_READY,
    TOP5_PUBLICATION_PRECHECK_BLOCKED,
    TOP5_PUBLICATION_PRECHECK_READY,
    publication_precheck,
    validate_public_bundle,
)
from tests.football.test_top5_public_delivery import (
    BASE,
    LEAGUES,
    _controlled_public_product,
)


def _bundle() -> dict[str, object]:
    product = _controlled_public_product()
    release = product["top5_release"]
    release["source_release_sha"] = "1" * 40
    release["runtime_data_sha"] = "2" * 40
    release["source_runtime_consistent"] = True
    return product


def test_exact_five_league_bundle_and_provenance_pass() -> None:
    bundle = _bundle()
    result = validate_public_bundle(
        bundle,
        now=BASE + timedelta(minutes=1),
        expected_source_release_sha="1" * 40,
        expected_runtime_data_sha="2" * 40,
    )
    assert result["status"] == TOP5_PUBLIC_DELIVERY_READY
    assert result["record_count"] == 15
    assert result["leagues"] == list(LEAGUES)


def test_stale_container_and_stale_signal_fail_closed() -> None:
    result = validate_public_bundle(_bundle(), now=BASE + timedelta(hours=3))
    assert result["status"] == TOP5_PUBLIC_DELIVERY_BLOCKED
    codes = {reason["code"] for reason in result["reasons"]}
    assert "PUBLIC_ARTIFACT_STALE" in codes


def test_future_release_timestamp_fails_closed() -> None:
    bundle = _bundle()
    bundle["top5_release"]["published_at"] = (BASE + timedelta(minutes=1)).isoformat()
    result = validate_public_bundle(bundle, now=BASE)
    assert result["status"] == TOP5_PUBLIC_DELIVERY_BLOCKED
    assert any(
        reason["code"] == "PUBLIC_ARTIFACT_FUTURE" for reason in result["reasons"]
    )


def test_missing_or_malformed_release_timestamp_fails_closed() -> None:
    missing = _bundle()
    del missing["top5_release"]["generated_at"]
    result = validate_public_bundle(missing, now=BASE)
    assert result["status"] == TOP5_PUBLIC_DELIVERY_BLOCKED
    assert any(
        reason["code"] == "PUBLIC_SCHEMA_INVALID" for reason in result["reasons"]
    )

    malformed = _bundle()
    malformed["top5_release"]["published_at"] = "not-a-timestamp"
    result = validate_public_bundle(malformed, now=BASE)
    assert result["status"] == TOP5_PUBLIC_DELIVERY_BLOCKED
    assert any(
        reason["code"] == "PUBLIC_SCHEMA_INVALID" for reason in result["reasons"]
    )


def test_fresh_container_with_stale_signal_or_mismatched_provenance_fails_closed() -> (
    None
):
    stale_signal = _bundle()
    stale_signal["football"][0]["signal_timestamp"] = (
        BASE - timedelta(hours=3)
    ).isoformat()
    result = validate_public_bundle(stale_signal, now=BASE + timedelta(minutes=1))
    assert result["status"] == TOP5_PUBLIC_DELIVERY_BLOCKED
    assert any(
        reason["code"] == "PUBLIC_ARTIFACT_STALE" for reason in result["reasons"]
    )

    mismatch = _bundle()
    result = validate_public_bundle(
        mismatch,
        now=BASE + timedelta(minutes=1),
        expected_source_release_sha="f" * 40,
        expected_runtime_data_sha="2" * 40,
    )
    assert result["status"] == TOP5_PUBLIC_DELIVERY_BLOCKED
    assert any(
        reason["code"] == "PUBLIC_PROVENANCE_INVALID" for reason in result["reasons"]
    )


def test_candidate_provider_and_synthetic_records_never_become_public_ready() -> None:
    candidate = _bundle()
    candidate["top5_release"]["provider_authority"] = "therundown_experimental"
    result = validate_public_bundle(candidate, now=BASE + timedelta(minutes=1))
    assert result["status"] == TOP5_PUBLIC_DELIVERY_BLOCKED
    assert any(
        reason["code"] == "PUBLIC_CANDIDATE_AUTHORITY_LEAK"
        for reason in result["reasons"]
    )

    synthetic = _bundle()
    synthetic["football"][0]["synthetic"] = True
    synthetic["football"][0]["evidence_kind"] = "TEST_FIXTURE"
    result = validate_public_bundle(synthetic, now=BASE + timedelta(minutes=1))
    assert result["status"] == TOP5_PUBLIC_DELIVERY_BLOCKED
    assert any(
        reason["code"] == "PUBLIC_TEST_DATA_REJECTED"
        or (
            reason["code"] == "PUBLIC_SCHEMA_INVALID"
            and "synthetic football evidence" in reason["message"]
        )
        for reason in result["reasons"]
    )


def test_offline_fixture_is_contract_ready_but_not_production_eligible() -> None:
    fixture = {
        "fixture_mode": "TEST/OFFLINE",
        "football": [
            {
                "league": league,
                "evidence_kind": "TEST_FIXTURE",
                "synthetic": True,
                "publication_enabled": False,
                "signal_status": "SHADOW",
            }
            for league in LEAGUES
        ],
    }
    result = validate_public_bundle(fixture, now=BASE, offline_fixture=True)
    assert result["status"] == TOP5_PUBLIC_DELIVERY_READY
    assert result["production_eligible"] is False
    assert result["evidence_classification"] == "TEST_FIXTURE"


def test_publication_precheck_requires_builder1_acceptance_and_keeps_publish_disabled(
    monkeypatch,
) -> None:
    bundle, b1_result, now, dry_run = _verified_b1_cli_evidence(monkeypatch)
    before = deepcopy(bundle)
    result = publication_precheck(
        bundle,
        b1_result,
        now=now,
        delivery_manifest=dry_run,
    )
    assert result["status"] == TOP5_PUBLICATION_PRECHECK_READY
    assert result["publication_enabled"] is False
    assert result["publication_authorized"] is False
    assert result["production_mutation"] is False
    assert result["provider_requests"] == 0
    assert bundle == before


def test_publication_precheck_requires_verified_b1_and_rejects_network_dry_run(
    monkeypatch,
) -> None:
    bundle, _b1_result, now, dry_run = _verified_b1_cli_evidence(monkeypatch)
    invalid = deepcopy(dry_run)
    invalid["network_requests"] = 1
    result = publication_precheck(bundle, {}, now=now, delivery_manifest=invalid)
    assert result["status"] == TOP5_PUBLICATION_PRECHECK_BLOCKED
    assert result["publication_enabled"] is False
    assert result["production_mutation"] is False
    assert result["provider_requests"] == 0
    assert any(
        reason["code"] == "PUBLIC_ACCEPTANCE_EVIDENCE_INVALID"
        for reason in result["reasons"]
    )
    assert any(reason["code"] == "PUBLIC_DRY_RUN_INVALID" for reason in result["reasons"])


def test_publication_precheck_requires_dry_run_and_rollback_manifest(monkeypatch) -> None:
    bundle, b1_result, now, _dry_run = _verified_b1_cli_evidence(monkeypatch)
    result = publication_precheck(bundle, b1_result, now=now)
    assert result["status"] == "TOP5_PUBLICATION_PRECHECK_BLOCKED"
    assert any(
        reason["code"] == "PUBLIC_DELIVERY_MANIFEST_MISSING"
        for reason in result["reasons"]
    )


def _verified_b1_cli_evidence(monkeypatch=None):
    from src.football.top5_final_acceptance import verify_final_acceptance
    from tests.football import (
        test_top5_candidate_provider_eligibility as candidate_tests,
    )
    from tests.football import test_top5_final_acceptance as builder1_tests
    from tests.football import test_top5_therundown_network_shadow as shadow_tests
    from tests.football.test_top5_public_delivery import BASE as PUBLIC_BASE

    now = PUBLIC_BASE + timedelta(minutes=1)
    if monkeypatch is not None:
        monkeypatch.setattr(candidate_tests, "NOW", PUBLIC_BASE)
        monkeypatch.setattr(shadow_tests, "NOW", PUBLIC_BASE)
        monkeypatch.setattr(builder1_tests, "NOW", PUBLIC_BASE)
        monkeypatch.setattr(builder1_tests, "NOW_ACCEPTANCE", now)
    bundle = builder1_tests._bundle()
    result = verify_final_acceptance(bundle, now=now)
    from src.football.top5_public_acceptance import Top5PrepublicationArtifactV1

    artifact = Top5PrepublicationArtifactV1.from_mapping(bundle["public"])
    delivery_manifest = artifact.delivery_dry_run_manifest(rollback_ready=True)
    return bundle["public"], result, now, delivery_manifest


def test_publication_precheck_accepts_actual_verified_b1_cli_shape_read_only(
    monkeypatch,
):
    public_payload, b1_result, now, delivery_manifest = _verified_b1_cli_evidence(
        monkeypatch
    )

    result = publication_precheck(
        public_payload,
        b1_result,
        now=now,
        delivery_manifest=delivery_manifest,
    )

    assert b1_result["status"] == "TOP5_FINAL_ACCEPTANCE_VERIFIED"
    assert result["status"] == TOP5_PUBLICATION_PRECHECK_READY
    assert result["publication_enabled"] is False
    assert result["production_mutation"] is False
    assert result["provider_requests"] == 0


@pytest.mark.parametrize(
    "mutation",
    (
        "manifest_digest",
        "candidate_authority",
        "product_binding",
        "public_payload",
        "outer_shape",
    ),
)
def test_publication_precheck_rejects_tampered_b1_cli_output(monkeypatch, mutation):
    public_payload, b1_result, now, delivery_manifest = _verified_b1_cli_evidence(
        monkeypatch
    )
    manifest = b1_result["manifest"]
    if mutation == "manifest_digest":
        manifest["manifest_digest"] = "0" * 64
    elif mutation == "candidate_authority":
        manifest["checks"]["candidate_not_authority"] = False
        manifest["manifest_digest"] = builder1_manifest_digest(manifest)
    elif mutation == "product_binding":
        manifest["public_product_digest"] = "0" * 64
        manifest["manifest_digest"] = builder1_manifest_digest(manifest)
    elif mutation == "public_payload":
        public_payload["worker_candidate_payload"]["top5_release"]["runtime_data_sha"] = "f" * 64
    else:
        b1_result["publication_authorized"] = True

    result = publication_precheck(
        public_payload,
        b1_result,
        now=now,
        delivery_manifest=delivery_manifest,
    )
    assert result["status"] == TOP5_PUBLICATION_PRECHECK_BLOCKED
    assert result["publication_enabled"] is False
    assert result["provider_requests"] == 0


def builder1_manifest_digest(manifest):
    from src.football.top5_final_acceptance import canonical_digest

    return canonical_digest(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    )


def test_cli_is_read_only_and_emits_machine_status(tmp_path, capsys) -> None:
    path = tmp_path / "offline.json"
    path.write_text(
        '{"fixture_mode":"TEST/OFFLINE","football":['
        + ",".join(
            f'{{"league":"{league}","evidence_kind":"TEST_FIXTURE","synthetic":true,"publication_enabled":false,"signal_status":"SHADOW"}}'
            for league in LEAGUES
        )
        + "]}"
    )
    assert acceptance_cli(["accept", "--bundle", str(path), "--offline-fixture"]) == 0
    assert '"status": "TOP5_PUBLIC_DELIVERY_READY"' in capsys.readouterr().out


def test_captured_worker_and_static_surfaces_require_exact_same_generation() -> None:
    worker = _bundle()
    static = _bundle()
    result = verify_public_captures(
        worker,
        static,
        now=BASE + timedelta(minutes=1),
    )
    assert result["status"] == "TOP5_PUBLIC_PRODUCTION_VERIFIED"

    static = _bundle()
    static["top5_release"]["generation_id"] = "top5-generation-v1:other"
    blocked = verify_public_captures(
        worker,
        static,
        now=BASE + timedelta(minutes=1),
    )
    assert blocked["status"] == "TOP5_PUBLIC_PRODUCTION_BLOCKED"
    assert any(
        reason["code"] == "PUBLIC_PROVENANCE_INVALID" for reason in blocked["reasons"]
    )

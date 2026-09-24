"""Regression tests for the side-effect-free Top-5 public-read boundary."""

from __future__ import annotations

from datetime import timedelta

from scripts.top5_public_acceptance import main as acceptance_cli
from scripts.top5_public_production_verification import verify_public_captures
from src.football.top5_public_acceptance import (
    TOP5_PUBLIC_DELIVERY_BLOCKED,
    TOP5_PUBLIC_DELIVERY_READY,
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
        reason["code"] == "PUBLIC_TEST_DATA_REJECTED" for reason in result["reasons"]
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


def test_publication_precheck_requires_builder1_acceptance_and_keeps_publish_disabled() -> (
    None
):
    bundle = _bundle()
    release = bundle["top5_release"]
    accepted = {
        "schema_version": "top5-final-acceptance-v1",
        "status": "ACCEPTED",
        "publication_ready": True,
        "provider_authority": "the_odds_api",
        "generation_id": release["generation_id"],
        "activation_id": release["activation_id"],
        "source_release_sha": "1" * 40,
        "runtime_data_sha": "2" * 40,
        "evidence_digest": "evidence-accepted",
    }
    bundle_digest = validate_public_bundle(bundle, now=BASE + timedelta(minutes=1))[
        "bundle_digest"
    ]
    result = publication_precheck(
        bundle,
        accepted,
        now=BASE + timedelta(minutes=1),
        delivery_manifest={
            "public_product_digest": bundle_digest,
            "generation_id": release["generation_id"],
            "activation_id": release["activation_id"],
            "static_payload_digest": bundle_digest,
            "worker_payload_digest": bundle_digest,
            "dry_run_status": "TOP5_DELIVERY_DRY_RUN",
            "rollback_ready": True,
        },
    )
    assert result["status"] == TOP5_PUBLICATION_PRECHECK_READY
    assert result["publication_enabled"] is False
    assert result["provider_requests"] == 0


def test_publication_precheck_requires_dry_run_and_rollback_manifest() -> None:
    bundle = _bundle()
    release = bundle["top5_release"]
    accepted = {
        "schema_version": "top5-final-acceptance-v1",
        "status": "ACCEPTED",
        "publication_ready": True,
        "provider_authority": "the_odds_api",
        "generation_id": release["generation_id"],
        "activation_id": release["activation_id"],
        "source_release_sha": "1" * 40,
        "runtime_data_sha": "2" * 40,
        "evidence_digest": "evidence-accepted",
    }
    result = publication_precheck(bundle, accepted, now=BASE + timedelta(minutes=1))
    assert result["status"] == "TOP5_PUBLICATION_PRECHECK_BLOCKED"
    assert any(
        reason["code"] == "PUBLIC_DELIVERY_MANIFEST_MISSING"
        for reason in result["reasons"]
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

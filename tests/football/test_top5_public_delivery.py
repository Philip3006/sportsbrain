"""Offline acceptance tests for the controlled Top-5 public delivery path."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.top5_publication_delivery_acceptance import (
    DeliveryAcceptanceError,
    validate_delivery,
)
from src.football.top5_public_delivery import (
    InMemoryTop5DeliveryTransaction,
    Top5CanonicalDeliveryAdapter,
    Top5DeliveryError,
)
from src.football.top5_publisher import (
    ControlledTop5PublicationBatch,
    ControlledTop5PublicationPayload,
    InMemoryTop5PublicationStore,
    Top5PublicationAuthorization,
    Top5PublisherContract,
    Top5PublisherPayload,
)
from src.notifications.public_serializer import (
    PublicFootballCompatibilityError,
    serialize_public_product,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "top5" / "publication_delivery_offline.json"
BASE = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
LEAGUES = ("EPL", "BL1", "LL", "SA", "L1")
HEX = "a" * 64


def _controlled_payload(league: str) -> ControlledTop5PublicationPayload:
    evidence = ("b" if league == "EPL" else "c" if league == "BL1" else "d" if league == "LL" else "e" if league == "SA" else "f") * 64
    fixture = f"{league}:controlled-001"
    return ControlledTop5PublicationPayload(
        artifact_path=f"docs/data/top5/published/{league}/signals.json",
        activation_id="activation:top5-delivery-test",
        league_code=league,
        candidate_id="candidate:top5-delivery-test",
        model_identity="model:top5-delivery-test",
        signal_time_experiment_id="signal-time:top5-delivery-test",
        source_sha=HEX,
        research_sha=HEX,
        model_artifact_hash=HEX,
        provider_authority="the_odds_api",
        result_authority="result-source",
        evidence_digest=evidence,
        controlled_shadow_run_id="shadow:top5-delivery-test",
        qualification_session_id="qualification:top5-delivery-test",
        generated_at=BASE,
        football_records=(
            {
                "fixture": {
                    "fixture_key": fixture,
                    "home_team": f"{league} Home",
                    "away_team": f"{league} Away",
                    "kickoff": "2026-09-21T18:00:00Z",
                },
                "league": league,
                "model_identity": "model:top5-delivery-test",
                "research_sha": HEX,
                "source_sha": HEX,
                "signal_time_experiment_id": "signal-time:top5-delivery-test",
                "activation_id": "activation:top5-delivery-test",
                "evidence_digest": evidence,
                "controlled_shadow_run_id": "shadow:top5-delivery-test",
                "qualification_session_id": "qualification:top5-delivery-test",
                "prediction_id": f"prediction:{league}:controlled-001",
                "probabilities": {"home": 0.5, "draw": 0.25, "away": 0.25},
                "odds": {"home": 2.2, "draw": 3.5, "away": 4.2},
                "no_bet": True,
                "closing_used_for_prediction": False,
            },
        ),
        health={
            "status": "ok",
            "activation_state": "controlled",
            "no_bet": True,
            "provider_health": "ok",
            "result_source_health": "ok",
        },
    )


def _offline_public_product() -> dict[str, object]:
    raw = json.loads(FIXTURE.read_text())
    publisher = Top5PublisherContract()
    envelopes = []
    for item in raw["records"]:
        payload = Top5PublisherPayload(
            artifact_path=f"docs/data/top5/shadow/{item['league']}/offline.json",
            league_code=item["league"],
            fixture_key=item["fixture"],
            candidate_id="candidate:test-offline",
            model_identity="model:test-offline",
            signal_id=item["signal_id"],
            signal_generated_at=BASE,
            source_sha=HEX,
            research_sha=HEX,
            model_artifact_hash=HEX,
            probabilities={"home": 0.5, "draw": 0.25, "away": 0.25},
            snapshot_age_seconds=0,
            provenance={
                "source_sha": HEX,
                "research_sha": HEX,
                "model_artifact_hash": HEX,
            },
        )
        staged = publisher.stage_shadow(payload)
        published = staged.payload.as_payload()
        envelopes.append(
            {
                "record_type": "prediction_artifact",
                "prediction_artifact": {
                    "league_code": published["league"],
                    "fixture_key": published["fixture"],
                    "prediction_id": published["signal_id"],
                    "model_identity": published["model"],
                    "prediction_timestamp": published["signal_generated_at"],
                    "signal_timestamp": published["signal_generated_at"],
                    "snapshot_id": "offline-snapshot",
                    "snapshot_kind": "SIGNAL_TIME",
                    "probabilities": published["probabilities"],
                },
                "fixture": {
                    "fixture_key": published["fixture"],
                    "home_team": item["home"],
                    "away_team": item["away"],
                    "kickoff": "2026-09-21T18:00:00Z",
                },
                "provenance": {
                    **published["provenance"],
                    "evidence_kind": raw["evidence_kind"],
                    "synthetic": True,
                },
                "activation_state": "shadow",
                "publication_status": "UNPUBLISHED",
                "publication_enabled": False,
                "no_bet": True,
            }
        )
    return serialize_public_product(
        {"updated": BASE.isoformat(), "football": envelopes}
    )


def _published_batch_artifact():
    payloads = tuple(_controlled_payload(league) for league in LEAGUES)
    authorizations = {
        payload.league_code: Top5PublicationAuthorization(
            publication_authorization_id=f"publication-auth:{payload.league_code}",
            activation_id=payload.activation_id,
            league_code=payload.league_code,
            candidate_id=payload.candidate_id,
            model_identity=payload.model_identity,
            source_sha=payload.source_sha,
            research_sha=payload.research_sha,
            model_artifact_hash=payload.model_artifact_hash,
            signal_time_experiment_id=payload.signal_time_experiment_id,
            publication_token=f"token:{payload.league_code}",
            issued_at=BASE - timedelta(minutes=1),
            expires_at=BASE + timedelta(hours=1),
        )
        for payload in payloads
    }
    binding_names = (
        "activation_id",
        "league_code",
        "candidate_id",
        "model_identity",
        "source_sha",
        "research_sha",
        "model_artifact_hash",
        "signal_time_experiment_id",
        "provider_authority",
        "result_authority",
        "evidence_digest",
        "controlled_shadow_run_id",
        "qualification_session_id",
    )
    bindings = {
        payload.league_code: {
            "active": True,
            **{
                name: (
                    payload.activation_id
                    if name == "activation_id"
                    else getattr(payload, name)
                )
                for name in binding_names
            },
        }
        for payload in payloads
    }
    store = InMemoryTop5PublicationStore()
    return store.publish_batch(
        payloads,
        authorizations,
        activation_bindings=bindings,
        now=BASE,
    )


def test_offline_fixture_is_publisher_to_pwa_compatible_and_never_real():
    product = _offline_public_product()
    assert len(product["football"]) == 15
    assert {record["league"] for record in product["football"]} == set(LEAGUES)
    assert {record["fixture_key"] for record in product["football"]} == {
        f"{league}:offline-001" for league in LEAGUES
    }
    assert all(record["signal_status"] == "SHADOW" for record in product["football"])
    assert all(record["publication_enabled"] is False for record in product["football"])
    assert all(record["no_bet"] is True for record in product["football"])
    assert all(record["evidence_kind"] == "TEST_FIXTURE" for record in product["football"])
    assert all(record["real_observed"] is False for record in product["football"])
    assert "top5_release" not in product


def test_controlled_batch_preserves_all_release_bindings_and_current_odds():
    payloads = tuple(_controlled_payload(league) for league in LEAGUES)
    product = ControlledTop5PublicationBatch(payloads).as_public_product(
        published_at=BASE,
        publication_authorization_id="publication-auth:top5-delivery-test",
    )
    release = product["top5_release"]
    assert release["league_codes"] == sorted(LEAGUES)
    assert release["publication_authorization_id"] == "publication-auth:top5-delivery-test"
    assert release["publication_enabled"] is True
    assert release["no_bet"] is True
    assert len({record["fixture_key"] for record in product["football"]}) == 5
    assert {record["league"] for record in product["football"]} == set(LEAGUES)
    for record in product["football"]:
        assert record["activation_id"] == "activation:top5-delivery-test"
        assert record["controlled_shadow_run_id"] == "shadow:top5-delivery-test"
        assert record["qualification_session_id"] == "qualification:top5-delivery-test"
        assert record["current_odds"] == record["odds"]
        assert record["current_ev_pct"] == record["ev_pct"]
        assert record["signal_status"] == "CONTROLLED"
        assert record["no_bet"] is True
        assert record["provenance"]["evidence_digest"] == record["evidence_digest"]


def test_public_serialization_is_idempotent_for_controlled_release():
    product = ControlledTop5PublicationBatch(
        tuple(_controlled_payload(league) for league in LEAGUES)
    ).as_public_product(
        published_at=BASE,
        publication_authorization_id="publication-auth:top5-delivery-test",
    )
    assert serialize_public_product(product) == product


def test_unbound_non_fixture_top5_records_are_rejected_at_serializer_boundary():
    with pytest.raises(PublicFootballCompatibilityError, match="controlled release"):
        serialize_public_product(
            {
                "football": [
                    {
                        "sport": "football",
                        "league": "EPL",
                        "signal_status": "CONTROLLED",
                        "publication_status": "PUBLISHED",
                        "publication_enabled": True,
                        "no_bet": True,
                    }
                ]
            }
        )


def test_read_only_post_publication_acceptance_binds_worker_static_and_auth():
    product = ControlledTop5PublicationBatch(
        tuple(_controlled_payload(league) for league in LEAGUES)
    ).as_public_product(
        published_at=BASE,
        publication_authorization_id="publication-auth:top5-delivery-test",
    )
    result = validate_delivery(
        product,
        json.loads(json.dumps(product)),
        {
            "publication_authorized": True,
            "generation_id": product["top5_release"]["generation_id"],
            "activation_id": product["top5_release"]["activation_id"],
            "provider_authority": "the_odds_api",
        },
        now=BASE.replace(hour=13),
    )
    assert result["status"] == "TOP5_DELIVERY_VERIFIED"
    assert result["worker_status"] == 200
    assert result["pwa_status"] == 200


def test_read_only_acceptance_rejects_mixed_generation():
    product = ControlledTop5PublicationBatch(
        tuple(_controlled_payload(league) for league in LEAGUES)
    ).as_public_product(
        published_at=BASE,
        publication_authorization_id="publication-auth:top5-delivery-test",
    )
    static = json.loads(json.dumps(product))
    static["top5_release"]["generation_id"] = "top5-generation-v1:stale"
    with pytest.raises(DeliveryAcceptanceError, match="generation_id mismatch"):
        validate_delivery(
            product,
            static,
            {
                "publication_authorized": True,
                "generation_id": product["top5_release"]["generation_id"],
                "activation_id": product["top5_release"]["activation_id"],
                "provider_authority": "the_odds_api",
            },
            now=BASE.replace(hour=13),
        )


def test_store_swaps_five_league_generation_atomically():
    payloads = tuple(_controlled_payload(league) for league in LEAGUES)
    authorizations = {
        payload.league_code: Top5PublicationAuthorization(
            publication_authorization_id=f"publication-auth:{payload.league_code}",
            activation_id=payload.activation_id,
            league_code=payload.league_code,
            candidate_id=payload.candidate_id,
            model_identity=payload.model_identity,
            source_sha=payload.source_sha,
            research_sha=payload.research_sha,
            model_artifact_hash=payload.model_artifact_hash,
            signal_time_experiment_id=payload.signal_time_experiment_id,
            publication_token=f"token:{payload.league_code}",
            issued_at=BASE - timedelta(minutes=1),
            expires_at=BASE + timedelta(hours=1),
        )
        for payload in payloads
    }
    bindings = {
        payload.league_code: {
            "active": True,
            **{
                name: (
                    payload.activation_id
                    if name == "activation_id"
                    else getattr(payload, name)
                )
                for name in (
                    "activation_id",
                    "league_code",
                    "candidate_id",
                    "model_identity",
                    "source_sha",
                    "research_sha",
                    "model_artifact_hash",
                    "signal_time_experiment_id",
                    "provider_authority",
                    "result_authority",
                    "evidence_digest",
                    "controlled_shadow_run_id",
                    "qualification_session_id",
                )
            },
        }
        for payload in payloads
    }
    store = InMemoryTop5PublicationStore()
    artifact = store.publish_batch(
        payloads,
        authorizations,
        activation_bindings=bindings,
        now=BASE,
    )
    assert store.current is None
    assert store.current_batch is artifact
    assert artifact.public_product["top5_release"]["publication_authorization_id"].startswith(
        "top5-batch-publication-v1:"
    )
    rollback = store.rollback()
    assert rollback.restored_unpublished is True
    assert store.current_batch is None


def test_canonical_delivery_replaces_only_top5_and_preserves_public_product():
    artifact = _published_batch_artifact()
    current = {
        "updated": "2026-09-20T11:00:00Z",
        "tennis": [{"match_id": "tennis-keep"}],
        "schedule": [{"league": "BL2", "fixture": "bl2-keep"}],
        "health": {"overall": "ok", "writer_state": "active"},
        "football": [
            {"league": "EPL", "fixture_key": "legacy-top5"},
            {"league": "UCL", "fixture_key": "ucl-keep"},
        ],
    }
    plan = Top5CanonicalDeliveryAdapter().build_plan(current, artifact)
    output = plan.public_product
    assert list(output["tennis"]) == current["tennis"]
    assert list(output["schedule"]) == current["schedule"]
    assert output["health"]["writer_state"] == "active"
    assert {record["league"] for record in output["football"] if record["league"] in LEAGUES} == set(LEAGUES)
    assert not any(record.get("fixture_key") == "legacy-top5" for record in output["football"])
    assert any(record.get("fixture_key") == "ucl-keep" for record in output["football"])
    assert plan.static_payload == plan.worker_payload
    assert plan.manifest()["static_payload_digest"] == plan.manifest()["worker_payload_digest"]


def test_canonical_delivery_rejects_conflicting_current_generation_and_mixed_artifact():
    artifact = _published_batch_artifact()
    adapter = Top5CanonicalDeliveryAdapter()
    with pytest.raises(Top5DeliveryError, match="conflicting Top-5 generation"):
        adapter.build_plan(
            {
                "football": [],
                "top5_release": {
                    **artifact.public_product["top5_release"],
                    "generation_id": "top5-generation-v1:stale",
                },
            },
            artifact,
        )

    mixed_release = {
        **artifact.public_product["top5_release"],
        "activation_id": "activation:mixed",
    }
    mixed_product = {**artifact.public_product, "top5_release": mixed_release}
    digest = hashlib.sha256(
        json.dumps(mixed_product, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(Top5DeliveryError, match="record/release binding"):
        adapter.build_plan(
            {}, replace(artifact, public_product=mixed_product, artifact_digest=digest)
        )


def test_canonical_delivery_transaction_rolls_back_on_either_target_failure_and_is_idempotent():
    artifact = _published_batch_artifact()
    plan = Top5CanonicalDeliveryAdapter().build_plan({"football": []}, artifact)
    transaction = InMemoryTop5DeliveryTransaction(initial_payload=b"safe")
    with pytest.raises(Top5DeliveryError, match="static staging failed"):
        transaction.stage(plan, fail_static=True)
    assert transaction.static_payload == b"safe"
    assert transaction.worker_payload == b"safe"
    with pytest.raises(Top5DeliveryError, match="Worker staging failed"):
        transaction.stage(plan, fail_worker=True)
    assert transaction.static_payload == b"safe"
    assert transaction.worker_payload == b"safe"
    assert transaction.committed_digest is None
    result = transaction.stage(plan)
    assert result.status == "TOP5_DELIVERY_STAGED"
    assert transaction.static_payload == plan.serialized_payload
    assert transaction.worker_payload == plan.serialized_payload
    repeat = transaction.stage(plan)
    assert repeat.status == "TOP5_DELIVERY_IDEMPOTENT"


def test_acceptance_binds_delivery_manifest_and_digest():
    artifact = _published_batch_artifact()
    plan = Top5CanonicalDeliveryAdapter().build_plan({"football": []}, artifact)
    result = validate_delivery(
        json.loads(plan.serialized_payload),
        json.loads(plan.serialized_payload),
        {
            "publication_authorized": True,
            "generation_id": plan.generation_id,
            "activation_id": plan.activation_id,
            "provider_authority": "the_odds_api",
        },
        now=BASE + timedelta(minutes=1),
        expected_public_product_digest=plan.public_product_digest,
        delivery_manifest=plan.manifest(),
    )
    assert result["status"] == "TOP5_DELIVERY_VERIFIED"
    assert result["public_product_digest"] == plan.public_product_digest

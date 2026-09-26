"""Focused tests for the additive, fail-closed Top-5 public lifecycle read model."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.production_contracts import ActivationMode
from src.football.top5_lifecycle_public import TOP5_LIFECYCLE_SCHEMA_VERSION
from src.football.top5_publisher import ControlledTop5PublicationBatch
from src.football.top5_pwa import Top5PwaData
from src.notifications.public_serializer import (
    PublicFootballCompatibilityError,
    map_prediction_to_public_football_signals,
    serialize_public_product,
)
from tests.football.test_top5_public_delivery import (
    BASE,
    LEAGUES,
    _controlled_payload,
    _controlled_public_product,
)


def _lifecycle(
    record: dict[str, object],
    *,
    stage: str = "INITIAL",
    version: int = 1,
    initial_at: str | None = None,
    current_at: str | None = None,
    initial_probability: float | None = None,
    current_probability: float | None = None,
    probability_delta: float | None = None,
    classification: str | None = None,
) -> dict[str, object]:
    provenance = record["provenance"]
    assert isinstance(provenance, dict)
    current_probability = (
        float(record["model_prob"]) / 100
        if current_probability is None
        else current_probability
    )
    current_at = (
        str(record["prediction_timestamp"]) if current_at is None else current_at
    )
    initial_at = current_at if initial_at is None else initial_at
    current_market_probability = float(record["fair_prob"]) / 100
    initial_probability = (
        current_probability if initial_probability is None else initial_probability
    )
    result: dict[str, object] = {
        "schema_version": TOP5_LIFECYCLE_SCHEMA_VERSION,
        "lifecycle_id": f"life:{record['fixture_key']}:{record['market']}",
        "initial_record_id": f"initial:{record['fixture_key']}:{record['market']}",
        "lifecycle_version": version,
        "lifecycle_stage": stage,
        "initial_generated_at": initial_at,
        "current_generated_at": current_at,
        "initial_probability": initial_probability,
        "current_probability": current_probability,
        "initial_market_probability": current_market_probability,
        "current_market_probability": current_market_probability,
        "initial_edge_pp": (float(initial_probability) - current_market_probability)
        * 100,
        "current_edge_pp": (current_probability - current_market_probability) * 100,
        "provenance_binding": {
            key: provenance[key]
            for key in (
                "source_sha",
                "research_sha",
                "model_artifact_hash",
                "evidence_digest",
                "snapshot_id",
            )
        },
        "model_identity": record["model_identity"],
        "fixture_identity": record["fixture_key"],
        "owner": "must-not-be-public",
        "ledger": {"private": "must-not-be-public"},
    }
    if probability_delta is not None:
        result["probability_delta"] = probability_delta
    if classification is not None:
        result["refinement_classification"] = classification
    if stage == "WITHDRAWN":
        result["refinement_classification"] = "WITHDRAWN"
    if stage == "REFINED":
        result["edge_delta_pp"] = float(result["current_edge_pp"]) - float(
            result["initial_edge_pp"]
        )
        result["probability_delta"] = (
            current_probability - initial_probability
            if probability_delta is None
            else probability_delta
        )
        result["refinement_classification"] = classification or "STRENGTHENED"
    return result


def _controlled_product_with_lifecycle(stage: str = "INITIAL") -> dict[str, object]:
    product = _controlled_public_product()
    for record in product["football"]:
        record["lifecycle"] = _lifecycle(
            record,
            stage=stage,
            version=1 if stage == "INITIAL" else 2,
            initial_at=(BASE - timedelta(minutes=30)).isoformat()
            if stage != "INITIAL"
            else None,
            classification="STRENGTHENED" if stage == "REFINED" else None,
        )
    return product


def test_legacy_controlled_records_remain_valid_and_unchanged():
    product = _controlled_public_product()
    assert serialize_public_product(product) == product
    assert all("lifecycle" not in record for record in product["football"])


@pytest.mark.parametrize("stage", ("INITIAL", "REFINED", "WITHDRAWN"))
def test_controlled_lifecycle_stages_round_trip_with_public_allowlist(stage):
    product = _controlled_product_with_lifecycle(stage)
    serialized = serialize_public_product(product)
    assert len(serialized["football"]) == 15
    assert serialized["top5_release"]["provider_authority"] == "the_odds_api"
    assert serialized["top5_release"]["publication_enabled"] is True
    assert serialized["top5_release"]["no_bet"] is True
    lifecycle = serialized["football"][0]["lifecycle"]
    assert lifecycle["lifecycle_stage"] == stage
    assert lifecycle["schema_version"] == TOP5_LIFECYCLE_SCHEMA_VERSION
    assert "owner" not in lifecycle and "ledger" not in lifecycle
    assert lifecycle["initial_market_probability"] == pytest.approx(0.464602)
    assert "updated_at" not in lifecycle


def test_refined_lifecycle_requires_history_and_consistent_delta():
    product = _controlled_product_with_lifecycle("REFINED")
    del product["football"][0]["lifecycle"]["initial_record_id"]
    with pytest.raises(PublicFootballCompatibilityError, match="initial_record_id"):
        serialize_public_product(product)

    product = _controlled_product_with_lifecycle("REFINED")
    product["football"][0]["lifecycle"]["probability_delta"] = 0.9
    with pytest.raises(PublicFootballCompatibilityError, match="probability_delta"):
        serialize_public_product(product)


def test_lifecycle_rejects_backwards_time_identity_and_provider_authority():
    product = _controlled_product_with_lifecycle("REFINED")
    product["football"][0]["lifecycle"]["current_generated_at"] = (
        BASE - timedelta(hours=1)
    ).isoformat()
    with pytest.raises(
        PublicFootballCompatibilityError, match="timestamps move backwards"
    ):
        serialize_public_product(product)

    product = _controlled_product_with_lifecycle("INITIAL")
    product["football"][0]["lifecycle"]["model_identity"] = "other-model"
    with pytest.raises(PublicFootballCompatibilityError, match="identity binding"):
        serialize_public_product(product)

    product = _controlled_product_with_lifecycle("INITIAL")
    product["football"][0]["lifecycle"]["provider_authority"] = "candidate-provider"
    with pytest.raises(PublicFootballCompatibilityError, match="provider authority"):
        serialize_public_product(product)


def test_refinement_chain_collapses_to_one_latest_signal_and_rejects_forks():
    product = _controlled_product_with_lifecycle("INITIAL")
    initial = product["football"][0]
    initial_at = (BASE - timedelta(minutes=30)).isoformat()
    initial["prediction_timestamp"] = initial_at
    initial["signal_timestamp"] = initial_at
    initial["odds_ts"] = initial_at
    initial["lifecycle"] = _lifecycle(initial, initial_at=initial_at)
    refined = deepcopy(initial)
    refined["prediction_timestamp"] = BASE.isoformat()
    refined["signal_timestamp"] = BASE.isoformat()
    refined["odds_ts"] = BASE.isoformat()
    refined["model_prob"] = 52.0
    refined["lifecycle"] = _lifecycle(
        refined,
        stage="REFINED",
        version=2,
        initial_at=initial_at,
        current_at=BASE.isoformat(),
        initial_probability=0.5,
        current_probability=0.52,
        probability_delta=0.02,
        classification="STRENGTHENED",
    )
    product["football"].append(refined)
    serialized = serialize_public_product(product)
    assert len(serialized["football"]) == 15
    assert (
        sum(
            record["lifecycle"]["lifecycle_id"] == refined["lifecycle"]["lifecycle_id"]
            for record in serialized["football"]
        )
        == 1
    )
    assert (
        next(
            record
            for record in serialized["football"]
            if record["lifecycle"]["lifecycle_id"]
            == refined["lifecycle"]["lifecycle_id"]
        )["lifecycle"]["lifecycle_version"]
        == 2
    )

    fork = deepcopy(refined)
    fork["lifecycle"]["lifecycle_id"] = "different-lifecycle"
    product["football"].append(fork)
    with pytest.raises(PublicFootballCompatibilityError, match="ID changed"):
        serialize_public_product(product)


def test_lifecycle_metadata_cannot_authorize_or_bypass_synthetic_publication():
    product = _controlled_product_with_lifecycle("INITIAL")
    del product["top5_release"]["publication_authorization_id"]
    product["football"][0]["lifecycle"]["publication_authorization_id"] = "invented"
    serialized = serialize_public_product(product)
    assert "publication_authorization_id" not in serialized["football"][0]["lifecycle"]
    assert serialized["top5_release"].get("publication_authorization_id") is None

    unauthorized = _controlled_product_with_lifecycle("INITIAL")
    unauthorized.pop("top5_release")
    with pytest.raises(
        PublicFootballCompatibilityError, match="controlled release envelope"
    ):
        serialize_public_product(unauthorized)

    synthetic = _controlled_product_with_lifecycle("INITIAL")
    synthetic["football"][0]["synthetic"] = True
    synthetic["football"][0]["evidence_kind"] = "TEST_FIXTURE"
    with pytest.raises(
        PublicFootballCompatibilityError, match="synthetic football evidence"
    ):
        serialize_public_product(synthetic)


def test_prediction_mapper_projects_each_outcome_and_never_fabricates_history():
    generated = BASE.isoformat()
    provenance = {
        "source": "the_odds_api",
        "source_sha": "a" * 64,
        "research_sha": "b" * 64,
        "model_artifact_hash": "c" * 64,
        "evidence_digest": "d" * 64,
        "snapshot_id": "snapshot:test",
    }
    envelope = {
        "record_type": "prediction_artifact",
        "prediction_artifact": {
            "league_code": "EPL",
            "fixture_key": "EPL:fixture:one",
            "prediction_id": "prediction:one",
            "model_identity": "model:one",
            "prediction_timestamp": generated,
            "probabilities": {"home": 0.6, "draw": 0.2, "away": 0.2},
        },
        "fixture": {"fixture_key": "EPL:fixture:one"},
        "provenance": provenance,
        "activation_state": "shadow",
        "publication_enabled": False,
        "no_bet": True,
        "lifecycle_by_market": {
            market: {
                "schema_version": TOP5_LIFECYCLE_SCHEMA_VERSION,
                "lifecycle_id": f"life:{market}",
                "initial_record_id": f"initial:{market}",
                "lifecycle_version": 1,
                "lifecycle_stage": "INITIAL",
                "initial_generated_at": generated,
                "current_generated_at": generated,
                "current_probability": probability,
                "fixture_identity": "EPL:fixture:one",
                "model_identity": "model:one",
                "provenance_binding": {
                    key: provenance[key]
                    for key in (
                        "source_sha",
                        "research_sha",
                        "model_artifact_hash",
                        "evidence_digest",
                        "snapshot_id",
                    )
                },
                "owner": "private-marker",
                "ledger": ["private-marker"],
            }
            for market, probability in (("home", 0.6), ("draw", 0.2), ("away", 0.2))
        },
    }
    output = map_prediction_to_public_football_signals(envelope)
    assert len(output) == 3
    assert {record["lifecycle"]["lifecycle_stage"] for record in output} == {"INITIAL"}
    assert all("owner" not in record["lifecycle"] for record in output)
    assert all("initial_probability" not in record["lifecycle"] for record in output)
    assert all("ledger" not in str(record["lifecycle"]) for record in output)


def test_controlled_publisher_has_a_narrow_per_market_lifecycle_adapter():
    payloads = [_controlled_payload(league) for league in LEAGUES]
    first = payloads[0]
    source_record = dict(first.football_records[0])
    fixture = source_record["fixture"]
    assert isinstance(fixture, dict)
    fixture_key = str(fixture["fixture_key"])
    provenance = {
        "source_sha": first.source_sha,
        "research_sha": first.research_sha,
        "model_artifact_hash": first.model_artifact_hash,
        "evidence_digest": first.evidence_digest,
        "snapshot_id": first.signal_time_experiment_id,
    }
    source_record["lifecycle_by_market"] = {
        market: {
            "schema_version": TOP5_LIFECYCLE_SCHEMA_VERSION,
            "lifecycle_id": f"life:{fixture_key}:{market}",
            "initial_record_id": f"initial:{fixture_key}:{market}",
            "lifecycle_version": 1,
            "lifecycle_stage": "INITIAL",
            "initial_generated_at": BASE.isoformat(),
            "current_generated_at": BASE.isoformat(),
            "initial_probability": probability,
            "current_probability": probability,
            "provenance_binding": provenance,
            "fixture_identity": fixture_key,
            "model_identity": first.model_identity,
        }
        for market, probability in source_record["probabilities"].items()
    }
    payloads[0] = replace(first, football_records=(source_record,))

    product = ControlledTop5PublicationBatch(tuple(payloads)).as_public_product(
        published_at=BASE,
        publication_authorization_id="publication-auth:lifecycle-adapter-test",
    )
    epl = [record for record in product["football"] if record["league"] == "EPL"]
    assert len(epl) == 3
    assert {record["lifecycle"]["lifecycle_stage"] for record in epl} == {"INITIAL"}
    assert all(record["signal_status"] == "CONTROLLED" for record in epl)
    assert all(record["publication_enabled"] is True for record in epl)
    assert all(record["no_bet"] is True for record in epl)


def test_pwa_readiness_can_carry_lifecycle_without_enabling_publication():
    provenance = {
        "source_sha": "a" * 64,
        "research_sha": "b" * 64,
        "model_artifact_hash": "c" * 64,
    }
    at = BASE.isoformat()
    lifecycle = {
        "schema_version": TOP5_LIFECYCLE_SCHEMA_VERSION,
        "lifecycle_id": "life:pwa:home",
        "initial_record_id": "initial:pwa:home",
        "lifecycle_version": 2,
        "lifecycle_stage": "REFINED",
        "initial_generated_at": (BASE - timedelta(minutes=30)).isoformat(),
        "current_generated_at": at,
        "initial_probability": 0.55,
        "current_probability": 0.6,
        "probability_delta": 0.05,
        "refinement_classification": "STRENGTHENED",
        "fixture_identity": "fixture-pwa",
        "model_identity": "model-pwa",
        "provenance_binding": provenance,
    }
    pwa = Top5PwaData(
        league="EPL",
        fixture="fixture-pwa",
        kickoff=BASE + timedelta(hours=2),
        probabilities={"home": 0.6},
        model_identity="model-pwa",
        signal_timestamp=BASE,
        provenance=provenance,
        lifecycle_by_market={"home": lifecycle},
    )
    payload = pwa.as_payload()
    assert payload["lifecycle_by_market"]["home"]["lifecycle_version"] == 2
    assert payload["activation_mode"] == ActivationMode.DISABLED.value
    assert payload["no_bet"] is True
    assert payload["publication_enabled"] is False

    with pytest.raises(ValueError, match="current_probability disagrees"):
        Top5PwaData(
            league="EPL",
            fixture="fixture-pwa",
            kickoff=BASE + timedelta(hours=2),
            probabilities={"home": 0.4},
            model_identity="model-pwa",
            signal_timestamp=BASE,
            provenance=provenance,
            lifecycle_by_market={"home": lifecycle},
        ).validate()

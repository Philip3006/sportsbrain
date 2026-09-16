"""League-neutral football publication compatibility regressions."""

from __future__ import annotations

import pytest

from src.notifications.public_serializer import (
    PublicFootballCompatibilityError,
    build_public_football_release_health,
    map_prediction_to_public_football_signals,
    serialize_public_product,
)


def _synthetic_cl_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "record_type": "prediction_artifact",
        "synthetic": True,
        "prediction_artifact": {
            "prediction_id": "ucl-prediction-001",
            "fixture_key": "ucl:2026:fixture-001",
            "league_code": "ucl",
            "model_adapter_id": "cl-model-v1",
            "model_version": "cl-model-v1.0.0",
            "generated_at": "2026-09-16T12:00:00Z",
            "snapshot_id": "ucl-signal-snapshot-001",
            "snapshot_kind": "signal_time",
            "probabilities": {"home": 0.50, "draw": 0.25, "away": 0.25},
        },
        "fixture": {
            "home_team": "Synthetic Home FC",
            "away_team": "Synthetic Away FC",
            "kickoff": "2026-09-16T14:00:00Z",
        },
        "provenance": {
            "source": "synthetic-fixture",
            "source_sha": "a" * 64,
            "research_sha": "b" * 64,
            "model_artifact_hash": "c" * 64,
            "evidence_kind": "SYNTHETIC",
        },
        "health": {
            "source_age_seconds": 42,
            "stale": False,
            "run_id": "synthetic-run-001",
            "session_id": "synthetic-session-001",
        },
        "activation_state": "shadow",
        "no_bet": True,
        "publication_enabled": False,
    }
    record.update(overrides)
    return record


def test_synthetic_cl_maps_to_existing_public_football_shape() -> None:
    records = map_prediction_to_public_football_signals(_synthetic_cl_record())

    assert len(records) == 3
    assert {record["league"] for record in records} == {"ucl"}
    assert {record["market"] for record in records} == {"home", "draw", "away"}
    for record in records:
        assert record["sport"] == "football"
        assert record["fixture_key"] == "ucl:2026:fixture-001"
        assert record["model_identity"] == "cl-model-v1"
        assert record["model_version"] == "cl-model-v1.0.0"
        assert record["prediction_timestamp"] == "2026-09-16T12:00:00Z"
        assert record["signal_snapshot_id"] == "ucl-signal-snapshot-001"
        assert record["activation_state"] == "SHADOW"
        assert record["signal_status"] == "SHADOW"
        assert record["publication_enabled"] is False
        assert record["no_bet"] is True
        assert record["real_observed"] is False
        assert record["model_approved"] is False
        assert record["signal_time_approved"] is False
        assert record["production_activation"] is False
        assert record["stale_state"] == "FRESH"


def test_serializer_normalizes_marked_prediction_and_health_without_new_api() -> None:
    public = serialize_public_product(
        {
            "football": [_synthetic_cl_record()],
            "health": {
                "overall": "ok",
                "football_releases": [
                    {
                        "league": "ucl",
                        "model_identity": "cl-model-v1",
                        "model_version": "cl-model-v1.0.0",
                        "run_id": "synthetic-run-001",
                        "prediction_count": 3,
                        "publication_status": "UNPUBLISHED",
                        "stale_artifact": False,
                        "missing_result_count": 3,
                        "settlement_status": "PENDING",
                        "source_age_seconds": 42,
                        "source": "synthetic-fixture",
                        "rollback_state": "DISABLED",
                        "activation_state": "SHADOW",
                        "no_bet": True,
                        "publication_enabled": False,
                    }
                ],
            },
        }
    )

    assert len(public["football"]) == 3
    health = public["health"]["football_releases"][0]
    assert health["league"] == "ucl"
    assert health["model_identity"] == "cl-model-v1"
    assert health["prediction_count"] == 3
    assert health["publication_status"] == "UNPUBLISHED"
    assert health["missing_result_count"] == 3
    assert health["rollback_state"] == "DISABLED"
    assert health["activation_state"] == "SHADOW"


def test_mapping_is_deterministic() -> None:
    record = _synthetic_cl_record()
    assert map_prediction_to_public_football_signals(
        record
    ) == map_prediction_to_public_football_signals(record)


@pytest.mark.parametrize(
    "field",
    (
        "real_observed",
        "provider_approved",
        "model_approved",
        "signal_time_approved",
        "production_activation",
        "live_activation",
    ),
)
def test_synthetic_cl_cannot_claim_real_or_approved_state(field: str) -> None:
    with pytest.raises(
        PublicFootballCompatibilityError, match="synthetic football evidence"
    ):
        map_prediction_to_public_football_signals(_synthetic_cl_record(**{field: True}))


def test_synthetic_cl_cannot_be_published_or_live() -> None:
    with pytest.raises(
        PublicFootballCompatibilityError, match="synthetic football evidence"
    ):
        map_prediction_to_public_football_signals(
            _synthetic_cl_record(activation_state="live", publication_enabled=True)
        )


@pytest.mark.parametrize("publication_status", ("published", "Published", "PUBLISHED"))
def test_synthetic_publication_status_is_case_insensitive(
    publication_status: str,
) -> None:
    with pytest.raises(
        PublicFootballCompatibilityError, match="synthetic football evidence"
    ):
        map_prediction_to_public_football_signals(
            _synthetic_cl_record(publication_status=publication_status)
        )


@pytest.mark.parametrize("container", ("prediction_artifact", "provenance", "health"))
def test_synthetic_nested_publication_status_cannot_be_published(
    container: str,
) -> None:
    record = _synthetic_cl_record()
    record[container] = {**record[container], "publication_status": "published"}
    with pytest.raises(
        PublicFootballCompatibilityError, match="synthetic football evidence"
    ):
        map_prediction_to_public_football_signals(record)


def test_synthetic_release_health_cannot_be_published() -> None:
    with pytest.raises(
        PublicFootballCompatibilityError, match="synthetic football evidence"
    ):
        build_public_football_release_health(
            {
                "synthetic": True,
                "league": "ucl",
                "model_identity": "cl-model-v1",
                "publication_status": "PUBLISHED",
                "activation_state": "shadow",
            }
        )


def test_release_health_exposes_required_observability_fields() -> None:
    health = build_public_football_release_health(
        {
            "league_code": "ucl",
            "model_identity": "cl-model-v1",
            "run_id": "run-001",
            "prediction_count": 3,
            "publication_status": "FAILED",
            "publication_failure": True,
            "stale_artifact": True,
            "stale_artifact_age_seconds": 5400,
            "missing_result_count": 2,
            "settlement_status": "PENDING",
            "source_age_seconds": 120,
            "provider_name": "synthetic-fixture",
            "rollback_state": "DISABLED",
            "activation_state": "shadow",
            "no_bet": True,
            "publication_enabled": False,
            "observed_at": "2026-09-16T12:00:00Z",
        }
    )

    assert health == {
        "schema_version": "football-release-health-v1",
        "league": "ucl",
        "model_identity": "cl-model-v1",
        "model_version": "",
        "run_id": "run-001",
        "session_id": "",
        "prediction_count": 3,
        "publication_status": "FAILED",
        "publication_success": None,
        "publication_failure": True,
        "stale_artifact": True,
        "stale_artifact_age_seconds": 5400.0,
        "missing_result_count": 2,
        "settlement_status": "PENDING",
        "source_age_seconds": 120.0,
        "source": "synthetic-fixture",
        "provider": "synthetic-fixture",
        "source_sha": "",
        "rollback_state": "DISABLED",
        "activation_state": "SHADOW",
        "no_bet": True,
        "publication_enabled": False,
        "observed_at": "2026-09-16T12:00:00Z",
    }

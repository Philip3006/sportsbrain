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


def test_champions_league_context_and_lineage_are_additive_and_bounded() -> None:
    record = _synthetic_cl_record()
    record["competition_context"] = {
        "competition_id": "uefa_champions_league",
        "competition_name": "UEFA Champions League",
        "season": "2026/27",
        "historical_format_era": "league_phase_2024_onward",
        "stage": "knockout",
        "round": "round_of_16",
        "leg": 1,
        "aggregate_context": {"home_score": 1, "away_score": 0},
        "neutral_site": True,
    }
    record["prediction_artifact"] = {
        **record["prediction_artifact"],
        "prediction_time_provenance": {
            "source": "signal_time_snapshot",
            "captured_at": "2026-09-16T11:59:30Z",
            "snapshot_id": "ucl-signal-snapshot-001",
        },
        "model_artifact_hash": "d" * 64,
    }
    record["result"] = {
        "result_id": "ucl-result-001",
        "provider_result_id": "uefa-result-001",
        "result_source": "shadow-result-feed",
        "result_timestamp": "2026-09-16T16:00:00Z",
        "result_status": "PENDING",
    }
    record["freshness"] = {"state": "fresh", "source_age_seconds": 18}

    signal = map_prediction_to_public_football_signals(record)[0]

    assert signal["competition_id"] == "uefa_champions_league"
    assert signal["competition_identity"] == "uefa_champions_league"
    assert signal["season"] == "2026/27"
    assert signal["historical_format_era"] == "league_phase_2024_onward"
    assert signal["stage"] == "knockout"
    assert signal["round"] == "round_of_16"
    assert signal["leg"] == 1
    assert signal["aggregate_context"] == {"home_score": 1, "away_score": 0}
    assert signal["neutral_site"] is True
    assert signal["home_advantage_applicable"] is False
    assert signal["model_artifact_hash"] == "d" * 64
    assert signal["prediction_time_provenance"]["source"] == "signal_time_snapshot"
    assert signal["fixture_identity"]["fixture_key"] == "ucl:2026:fixture-001"
    assert signal["result_identity"]["result_id"] == "ucl-result-001"
    assert signal["result_lineage"]["prediction_id"] == "ucl-prediction-001"
    assert signal["freshness_state"] == "FRESH"
    assert signal["freshness_age_seconds"] == 18.0
    assert signal["shadow_state"] == "SHADOW"
    assert signal["externally_visible"] is False


def test_injected_and_real_observed_evidence_remain_distinguishable() -> None:
    injected = _synthetic_cl_record(synthetic=False, injected=True)
    injected["provenance"] = {
        **injected["provenance"],
        "evidence_kind": "INJECTED",
    }
    injected_signal = map_prediction_to_public_football_signals(injected)[0]
    assert injected_signal["evidence_kind"] == "INJECTED"
    assert injected_signal["injected"] is True
    assert injected_signal["synthetic"] is False
    assert injected_signal["real_observed"] is False

    observed = _synthetic_cl_record(synthetic=False)
    observed["provenance"] = {
        **observed["provenance"],
        "evidence_kind": "REAL_OBSERVED",
    }
    observed_signal = map_prediction_to_public_football_signals(observed)[0]
    assert observed_signal["evidence_kind"] == "REAL_OBSERVED"
    assert observed_signal["injected"] is False
    assert observed_signal["synthetic"] is False
    assert observed_signal["real_observed"] is True


def test_champions_league_active_or_public_state_fails_closed() -> None:
    with pytest.raises(
        PublicFootballCompatibilityError,
        match="Champions League compatibility output",
    ):
        map_prediction_to_public_football_signals(
            _synthetic_cl_record(
                synthetic=False,
                activation_state="live",
                no_bet=True,
                publication_enabled=False,
            )
        )


def test_rich_champions_league_release_health_retains_shadow_context() -> None:
    health = build_public_football_release_health(
        {
            "league": "ucl",
            "model_identity": "cl-model-v2",
            "model_artifact_hash": "e" * 64,
            "competition_context": {
                "competition_id": "uefa_champions_league",
                "season": "2026/27",
                "historical_format_era": "league_phase_2024_onward",
                "stage": "league_phase",
                "round": 1,
                "neutral_site": False,
            },
            "fixture_key": "ucl:fixture-002",
            "provider_fixture_id": "provider-fixture-002",
            "prediction_timestamp": "2026-09-17T10:00:00Z",
            "prediction_time_provenance": {"source": "signal_time"},
            "freshness": {"state": "fresh", "source_age_seconds": 25},
            "result_lineage": {
                "prediction_id": "ucl-prediction-002",
                "result_id": None,
            },
            "prediction_count": 1,
            "activation_state": "shadow",
            "publication_status": "UNPUBLISHED",
            "publication_enabled": False,
            "no_bet": True,
        }
    )
    assert health["competition_id"] == "uefa_champions_league"
    assert health["season"] == "2026/27"
    assert health["round"] == 1
    assert health["neutral_site"] is False
    assert health["model_artifact_hash"] == "e" * 64
    assert health["freshness_state"] == "FRESH"
    assert health["source_age_seconds"] == 25.0
    assert health["shadow_state"] == "SHADOW"
    assert health["externally_visible"] is False

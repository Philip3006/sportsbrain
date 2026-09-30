"""Live-release adapter for the frozen, causal Nations League v1.1 algorithm.

It consumes an already validated #229 input snapshot.  It neither reaches a
provider nor changes the immutable shadow records that predate this adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.analysis.nations_league_forward_input import build_input_state
from src.analysis.nations_league_v1_1 import MODEL_VERSION, model_digest, sha256_json
from src.models.live_model_lifecycle import (
    ModelLifecycleError,
    model_release,
    retrain_receipt,
    training_snapshot,
    validate_technical,
)

MODEL_FAMILY = "nations_league_v1_1"


def build_nations_league_training_snapshot(
    input_state: Mapping[str, Any],
    *,
    created_at: str,
    parent_release_id: str | None = None,
) -> dict[str, Any]:
    """Validate exactly the current causal input state and seal its Elo state."""
    state = dict(input_state)
    try:
        rebuilt = build_input_state(
            state["fixtures"],
            state["training_records"],
            prediction_cutoff=state["prediction_cutoff"],
            provenance=state["provenance"],
            base_timeline=state.get("base_timeline"),
            result_extension=state.get("result_extension"),
            completeness_artifact=state.get("completeness"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelLifecycleError(
            "Nations League input state is not result-safe"
        ) from exc
    if rebuilt != state:
        raise ModelLifecycleError("Nations League input state is not canonical")
    if (
        state.get("model_version") != MODEL_VERSION
        or state.get("model_digest") != model_digest()
    ):
        raise ModelLifecycleError("Nations League frozen algorithm binding is invalid")
    if state.get("completeness", {}).get("status") != "READY":
        raise ModelLifecycleError(
            "Nations League results require refresh before release"
        )
    cutoff = state["prediction_cutoff"]
    return training_snapshot(
        model_family=MODEL_FAMILY,
        algorithm_version=MODEL_VERSION,
        algorithm_digest=model_digest(),
        training_digest=sha256_json(state["training_records"]),
        training_cutoff=cutoff,
        training_rows=len(state["training_records"]),
        result_watermark=state["results_verified_through"],
        feature_schema_digest=sha256_json(
            {
                "base_timeline_digest": state["base_timeline_digest"],
                "result_extension_digest": state["result_extension_digest"],
                "completeness_digest": state["completeness_digest"],
                "identity_binding_digest": state["identity_binding_digest"],
            }
        ),
        created_at=created_at,
        parent_release_id=parent_release_id,
    )


def build_nations_league_release(
    input_state: Mapping[str, Any],
    *,
    created_at: str,
    parent_release_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    snapshot = build_nations_league_training_snapshot(
        input_state, created_at=created_at, parent_release_id=parent_release_id
    )
    state_digest = sha256_json(
        {
            "elo_state": input_state["elo_state"],
            "training_snapshot_id": snapshot["training_snapshot_id"],
        }
    )
    training_release = model_release(
        snapshot,
        state_digest=state_digest,
        created_at=created_at,
        parent_release_id=parent_release_id,
    )
    validation = validate_technical(training_release, checked_at=created_at)
    release = validation["validated_release"]
    receipt = retrain_receipt(
        snapshot,
        release,
        triggered_by="RESULT_WATERMARK_ADVANCED",
        created_at=created_at,
    )
    return snapshot, release, {"validation": validation, "retrain": receipt}


def historical_live_projection(
    source_record: Mapping[str, Any], release: Mapping[str, Any]
) -> dict[str, Any]:
    """Reference a prior immutable INITIAL capture without rewriting it."""
    if (
        source_record.get("model_version") != MODEL_VERSION
        or source_record.get("model_digest") != model_digest()
    ):
        raise ModelLifecycleError("historical capture has wrong frozen algorithm")
    if source_record.get("no_bet") is not True:
        raise ModelLifecycleError("historical capture lost no-bet binding")
    if (
        release.get("model_family") != MODEL_FAMILY
        or release.get("algorithm_digest") != model_digest()
    ):
        raise ModelLifecycleError(
            "live release does not bind historical model algorithm"
        )
    return {
        "schema": "nations-league-live-prediction-reference-v1",
        "source_record_id": source_record["record_id"],
        "source_record_digest": sha256_json(dict(source_record)),
        "source_lifecycle": "INITIAL",
        "active_release_id": release["release_id"],
        "algorithm_digest": model_digest(),
        "state_digest": release["state_digest"],
        "no_bet": True,
        "publication_eligible": True,
    }

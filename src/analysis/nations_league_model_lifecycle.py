"""Nations League v1.1 adapter for the generic continuous model lifecycle.

It consumes only sealed, local input-state artifacts produced by the frozen
forward-shadow contract.  It neither changes the frozen model nor emits a
prediction, network request, publication, or operational side effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from src.analysis.nations_league_forward_input import build_input_state
from src.analysis.nations_league_v1_1 import (
    MODEL_SPEC,
    MODEL_VERSION,
    model_digest,
    predict_1x2,
    sha256_json,
)
from src.models.lifecycle import (
    ACTIVE,
    ModelLifecycle,
    ModelRelease,
    RetrainReceipt,
    TrainingSnapshot,
    canonical_digest,
)

MODEL_FAMILY = "nations_league_v1_1"
SPORT = "football"
SCOPE = "UEFA Nations League"
FROZEN_ALGORITHM_DIGEST = (
    "50fc0f9120009b86eda1ebf016c14e93c9bd72c256b849577d5f55217d77f626"
)
FEATURE_SCHEMA = (
    "fixture_id",
    "edition",
    "evaluation_block",
    "home_team",
    "away_team",
    "kickoff_utc",
    "result_safe_available_at",
    "home_score",
    "away_score",
    "competition",
    "source_provenance",
    "source_digest",
)


class NationsLeagueLifecycleError(ValueError):
    """A sealed v1.1 input-state cannot form a safe lifecycle release."""


def parameter_payload() -> dict[str, Any]:
    """The immutable algorithm material, distinct from trained Elo state."""

    return {
        "model_version": MODEL_VERSION,
        "algorithm_digest": model_digest(),
        "algorithm": deepcopy(MODEL_SPEC["algorithm"]),
        "excluded_inputs": list(MODEL_SPEC["excluded_inputs"]),
    }


def _require_input_state(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("schema") != "nations-league-forward-input-v1_1":
        raise NationsLeagueLifecycleError("unsupported Nations League input-state")
    if (
        snapshot.get("model_version") != MODEL_VERSION
        or snapshot.get("model_digest") != FROZEN_ALGORITHM_DIGEST
        or model_digest() != FROZEN_ALGORITHM_DIGEST
    ):
        raise NationsLeagueLifecycleError("frozen Nations League algorithm changed")
    if snapshot.get("shadow") is not True or snapshot.get("no_bet") is not True:
        raise NationsLeagueLifecycleError("input-state violates frozen shadow safety")
    for field in (
        "base_timeline_digest",
        "result_extension_digest",
        "completeness_digest",
        "input_snapshot_digest",
    ):
        value = snapshot.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise NationsLeagueLifecycleError(f"input-state {field} is missing")
    rebuilt = build_input_state(
        snapshot.get("fixtures", []),
        snapshot.get("training_records", []),
        prediction_cutoff=snapshot.get("prediction_cutoff"),
        provenance=snapshot.get("provenance", {}),
        base_timeline=snapshot.get("base_timeline"),
        result_extension=snapshot.get("result_extension"),
        completeness_artifact=snapshot.get("completeness"),
        include_identity_bindings="identity_bindings" in snapshot,
    )
    if rebuilt != snapshot:
        raise NationsLeagueLifecycleError("sealed input-state digest/provenance mismatch")
    if len(snapshot["training_records"]) < 1 or not isinstance(
        snapshot.get("elo_state"), Mapping
    ):
        raise NationsLeagueLifecycleError("input-state has no causal Elo material")


def training_snapshot_from_input_state(
    snapshot: Mapping[str, Any], *, source_release_sha: str
) -> TrainingSnapshot:
    """Translate a verified v1.1 input state into generic immutable evidence."""

    _require_input_state(snapshot)
    rows = snapshot["training_records"]
    watermark = max(row["result_safe_available_at"] for row in rows)
    return TrainingSnapshot(
        model_family=MODEL_FAMILY,
        sport=SPORT,
        scope=SCOPE,
        algorithm_version=MODEL_VERSION,
        algorithm_digest=FROZEN_ALGORITHM_DIGEST,
        training_data_digest=canonical_digest(rows),
        training_cutoff=snapshot["prediction_cutoff"],
        training_row_count=len(rows),
        result_safe_watermark=watermark,
        feature_schema_digest=canonical_digest(list(FEATURE_SCHEMA)),
        trained_state_digest=canonical_digest(snapshot["elo_state"]),
        source_release_sha=source_release_sha,
        captured_at=snapshot["observed_at"],
    )


def _smoke_prediction(snapshot: Mapping[str, Any]) -> dict[str, float]:
    fixture = snapshot["fixtures"][0]
    bindings = snapshot.get("identity_bindings", {})
    home = bindings.get(fixture["home_team"], {}).get("canonical_team", fixture["home_team"])
    away = bindings.get(fixture["away_team"], {}).get("canonical_team", fixture["away_team"])
    return predict_1x2(
        snapshot["elo_state"], home, away, neutral=bool(fixture.get("neutral", False))
    )


def register_and_validate_nations_league_release(
    lifecycle: ModelLifecycle,
    input_state: Mapping[str, Any],
    *,
    source_release_sha: str,
    created_at: str,
) -> tuple[ModelRelease, RetrainReceipt]:
    """Create or idempotently reuse a technical-only v1.1 release candidate."""

    snapshot = training_snapshot_from_input_state(
        input_state, source_release_sha=source_release_sha
    )
    release, receipt = lifecycle.create_or_noop(
        snapshot,
        parameter_digest=canonical_digest(parameter_payload()),
        created_at=created_at,
    )
    if receipt.outcome == "NO_OP":
        return release, receipt
    validated = lifecycle.validate(
        release.release_id,
        training_rows=input_state["training_records"],
        trained_state=input_state["elo_state"],
        parameter_payload=parameter_payload(),
        prediction_smoke=_smoke_prediction(input_state),
    )
    if validated.status != "VALIDATED":
        raise NationsLeagueLifecycleError(
            f"Nations League technical validation rejected: {validated.rejection_reason}"
        )
    return validated, receipt


def establish_initial_active_release(
    input_state: Mapping[str, Any],
    *,
    source_release_sha: str,
) -> tuple[ModelLifecycle, ModelRelease]:
    """Create the first ACTIVE v1.1 release from sealed, already-real evidence."""

    lifecycle = ModelLifecycle()
    created_at = str(input_state["observed_at"])
    release, _ = register_and_validate_nations_league_release(
        lifecycle,
        input_state,
        source_release_sha=source_release_sha,
        created_at=created_at,
    )
    lifecycle.activate(release.release_id, activated_at=created_at)
    active = lifecycle.releases[release.release_id]
    if active.status != ACTIVE:
        raise NationsLeagueLifecycleError("initial release did not become active")
    return lifecycle, active


def prepare_result_driven_retrain(
    lifecycle: ModelLifecycle,
    *,
    fixtures: list[Mapping[str, Any]],
    training_records: list[Mapping[str, Any]],
    prediction_cutoff: str,
    provenance: Mapping[str, Any],
    base_timeline: Mapping[str, Any],
    result_extension: Mapping[str, Any],
    completeness_artifact: Mapping[str, Any],
    source_release_sha: str,
) -> tuple[ModelRelease, RetrainReceipt]:
    """Build the next causal release from base timeline plus sealed results.

    Equal immutable inputs return ``NO_OP``.  A new release is merely validated
    and registered; callers must choose an explicit later activation operation.
    """

    input_state = build_input_state(
        fixtures,
        training_records,
        prediction_cutoff=prediction_cutoff,
        provenance=provenance,
        base_timeline=base_timeline,
        result_extension=result_extension,
        completeness_artifact=completeness_artifact,
    )
    return register_and_validate_nations_league_release(
        lifecycle,
        input_state,
        source_release_sha=source_release_sha,
        created_at=input_state["observed_at"],
    )


def release_binding_payload(release: ModelRelease) -> dict[str, Any]:
    """The minimal public-safe binding for immutable source predictions."""

    if release.snapshot.model_family != MODEL_FAMILY or release.status != ACTIVE:
        raise NationsLeagueLifecycleError("only the active v1.1 release is publishable")
    return {
        "model_family": MODEL_FAMILY,
        "model_version": MODEL_VERSION,
        "algorithm_digest": FROZEN_ALGORITHM_DIGEST,
        "release_id": release.release_id,
        "training_data_digest": release.snapshot.training_data_digest,
        "trained_state_digest": release.snapshot.trained_state_digest,
        "training_cutoff": release.snapshot.training_cutoff,
        "binding_digest": sha256_json(
            {
                "release_id": release.release_id,
                "algorithm_digest": FROZEN_ALGORITHM_DIGEST,
                "training_data_digest": release.snapshot.training_data_digest,
                "trained_state_digest": release.snapshot.trained_state_digest,
            }
        ),
    }

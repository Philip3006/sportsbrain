"""Provider-free continuous LIVE lifecycle for Nations League v1.1.

The live lifecycle is deliberately an input-materialization seam: official
result refresh and the sealed #229 input-state are supplied by callers, while
this module enforces ordering, release binding, idempotency, and the financial
safety boundary.  It never calls a provider and it never publishes, bets, or
mutates a ledger.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.nations_league_forward_input import predict_from_input_state
from src.analysis.nations_league_model_lifecycle import (
    NationsLeagueLifecycleError,
    active_release_from_registry,
)
from src.analysis.nations_league_v1_1 import MODEL_VERSION
from src.models.lifecycle import ACTIVE, ModelRelease
from src.utils.atomic_io import atomic_write_json

COMPETITION = "UEFA Nations League"
INITIAL = "initial"
REFINEMENT = "refinement"
LIVE = "LIVE"
INITIAL_WINDOW = (timedelta(hours=22), timedelta(hours=26))
REFINEMENT_WINDOW = (timedelta(minutes=60), timedelta(minutes=120))


class NationsLeagueLiveRuntimeError(ValueError):
    """A live lifecycle input or transition is unsafe."""


def _utc(value: str, field: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise NationsLeagueLiveRuntimeError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NationsLeagueLiveRuntimeError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise NationsLeagueLiveRuntimeError(f"{field} must be explicit UTC")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def due_state(kickoff_utc: str, as_of: str) -> str:
    """Classify the two approved live capture windows, including boundaries."""

    kickoff = _utc(kickoff_utc, "kickoff_utc")
    clock = _utc(as_of, "as_of")
    lead = kickoff - clock
    if lead <= timedelta(0):
        return "STARTED"
    if lead > timedelta(hours=26):
        return "TOO_EARLY"
    if lead >= timedelta(hours=22):
        return "INITIAL_DUE"
    if lead > timedelta(minutes=120):
        return "BETWEEN_WINDOWS"
    if lead >= timedelta(minutes=60):
        return "REFINEMENT_DUE"
    return "TOO_LATE"


def phase_window(kickoff_utc: str, phase: str) -> tuple[datetime, datetime]:
    kickoff = _utc(kickoff_utc, "kickoff_utc")
    if phase == INITIAL:
        return kickoff - INITIAL_WINDOW[1], kickoff - INITIAL_WINDOW[0]
    if phase == REFINEMENT:
        return kickoff - REFINEMENT_WINDOW[1], kickoff - REFINEMENT_WINDOW[0]
    raise NationsLeagueLiveRuntimeError("unsupported live phase")


def load_active_release(registry_path: Path) -> ModelRelease:
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NationsLeagueLiveRuntimeError("active lifecycle registry unavailable") from exc
    try:
        release = active_release_from_registry(registry)
    except (KeyError, TypeError, NationsLeagueLifecycleError) as exc:
        raise NationsLeagueLiveRuntimeError(str(exc)) from exc
    if release.status != ACTIVE:
        raise NationsLeagueLiveRuntimeError("live prediction requires an ACTIVE release")
    return release


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _probabilities(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != {"home", "draw", "away"}:
        raise NationsLeagueLiveRuntimeError("live probabilities are incomplete")
    result = {key: float(value[key]) for key in ("home", "draw", "away")}
    if any(not 0 <= item <= 1 for item in result.values()) or abs(sum(result.values()) - 1) > 1e-9:
        raise NationsLeagueLiveRuntimeError("live probabilities are invalid")
    return result


def build_live_prediction(
    input_state: Mapping[str, Any],
    fixture_id: str,
    *,
    phase: str,
    active_release: ModelRelease,
    captured_at: str,
) -> dict[str, Any]:
    """Create one provider-free LIVE record from a READY #229 input-state.

    ``predict_from_input_state`` remains the sole frozen-model prediction
    implementation.  This adapter only binds the result to the active release
    and the LIVE safety/provenance contract.
    """

    captured = _utc(captured_at, "captured_at")
    input_cutoff = _utc(str(input_state.get("prediction_cutoff", "")), "input prediction_cutoff")
    if input_cutoff != captured:
        raise NationsLeagueLiveRuntimeError(
            "live prediction cutoff must equal the sealed input-state cutoff"
        )
    if active_release.status != ACTIVE:
        raise NationsLeagueLiveRuntimeError("active release is required")
    if active_release.snapshot.algorithm_version != MODEL_VERSION:
        raise NationsLeagueLiveRuntimeError("wrong live model version")
    shadow = predict_from_input_state(input_state, fixture_id, phase=phase)
    probabilities = _probabilities(shadow["probabilities"])
    fixture = next(row for row in input_state["fixtures"] if row["fixture_id"] == fixture_id)
    return {
        "record_id": _digest(
            {
                "fixture_id": fixture_id,
                "phase": phase,
                "release_id": active_release.release_id,
                "input_snapshot_digest": input_state["input_snapshot_digest"],
            }
        ),
        "record_type": "prediction",
        "status": LIVE,
        "competition": COMPETITION,
        "edition": fixture.get("edition"),
        "fixture_id": fixture_id,
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
        "kickoff_utc": fixture["kickoff_utc"],
        "phase": phase,
        "prediction_timestamp": _stamp(captured),
        "updated_at": _stamp(captured),
        "probabilities": probabilities,
        "model_release_id": active_release.release_id,
        "model_version": MODEL_VERSION,
        "algorithm_digest": active_release.snapshot.algorithm_digest,
        "training_data_digest": active_release.snapshot.training_data_digest,
        "trained_state_digest": active_release.snapshot.trained_state_digest,
        "training_cutoff": active_release.snapshot.training_cutoff,
        "input_snapshot_digest": input_state["input_snapshot_digest"],
        "fixture_source_digest": fixture["source_digest"],
        "source_provenance": input_state.get("provenance", {}),
        "no_bet": True,
        "betting_enabled": False,
        "publication_enabled": True,
        "ledger_mutation": False,
    }


def refresh_result_state(
    previous: Mapping[str, Any] | None, current_input_state: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a deterministic NO_OP or retrain-required refresh decision."""

    current_digest = current_input_state.get("result_extension_digest")
    if not isinstance(current_digest, str) or len(current_digest) != 64:
        raise NationsLeagueLiveRuntimeError("result refresh requires a sealed extension digest")
    previous_digest = previous.get("result_extension_digest") if isinstance(previous, Mapping) else None
    return {
        "status": "NO_OP" if previous_digest == current_digest else "RESULTS_CHANGED",
        "result_extension_digest": current_digest,
        "retrain_required": previous_digest != current_digest,
        "no_bet": True,
        "ledger_mutation": False,
    }


def run_live_cycle(
    manifest: Mapping[str, Any],
    input_state: Mapping[str, Any],
    active_release: ModelRelease,
    *,
    as_of: str,
    existing_records: Iterable[Mapping[str, Any]] = (),
    prediction_builder: Callable[..., dict[str, Any]] = build_live_prediction,
    execute: bool = False,
) -> dict[str, Any]:
    """Plan or materialize due LIVE predictions without network side effects."""

    clock = _utc(as_of, "as_of")
    if manifest.get("competition") != COMPETITION:
        raise NationsLeagueLiveRuntimeError("wrong future fixture manifest")
    if active_release.snapshot.algorithm_version != MODEL_VERSION:
        raise NationsLeagueLiveRuntimeError("wrong active release")
    existing = {}
    for record in existing_records:
        if not isinstance(record, Mapping):
            raise NationsLeagueLiveRuntimeError("live store record is malformed")
        key = (record.get("fixture_id"), record.get("phase"), record.get("model_release_id"))
        if key in existing:
            raise NationsLeagueLiveRuntimeError("conflicting duplicate live record")
        existing[key] = dict(record)
    plan: list[dict[str, Any]] = []
    appended: list[dict[str, Any]] = []
    for fixture in manifest.get("fixtures", []):
        if not isinstance(fixture, Mapping) or fixture.get("status") != "VERIFIED":
            continue
        state = due_state(str(fixture["kickoff_utc"]), _stamp(clock))
        phase = {"INITIAL_DUE": INITIAL, "REFINEMENT_DUE": REFINEMENT}.get(state)
        row = {
            "fixture_id": fixture["fixture_id"],
            "phase": phase,
            "due_state": state,
            "kickoff_utc": fixture["kickoff_utc"],
            "model_release_id": active_release.release_id,
            "status": "NOT_DUE" if phase is None else "DUE",
        }
        if phase is not None:
            key = (fixture["fixture_id"], phase, active_release.release_id)
            if key in existing:
                row["status"] = "ALREADY_CAPTURED"
            elif execute:
                appended_record = prediction_builder(
                    input_state,
                    fixture["fixture_id"],
                    phase=phase,
                    active_release=active_release,
                    captured_at=_stamp(clock),
                )
                if appended_record.get("status") != LIVE or appended_record.get("no_bet") is not True:
                    raise NationsLeagueLiveRuntimeError("live prediction safety contract failed")
                appended.append(appended_record)
                row["status"] = "MATERIALIZED"
        plan.append(row)
    return {
        "schema": "nations-league-live-cycle-v1",
        "as_of": _stamp(clock),
        "status": "READY" if plan else "NO_OP",
        "predictions": plan,
        "appended_records": appended,
        "appended_count": len(appended),
        "model_release_id": active_release.release_id,
        "no_bet": True,
        "publication_enabled": True,
        "betting_enabled": False,
        "ledger_mutation": False,
    }


def commit_active_registry(path: Path, registry: Mapping[str, Any]) -> None:
    """Atomically persist a validated registry supplied by a retrain caller."""

    active_release_from_registry(registry)
    atomic_write_json(path, dict(registry), indent=2, ensure_ascii=False, sort_keys=True)

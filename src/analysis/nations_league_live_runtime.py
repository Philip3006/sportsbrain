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
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.nations_league_forward_input import (
    build_input_state,
    predict_from_input_state,
)
from src.analysis.nations_league_live_edge import (
    build_edge_analysis,
    validate_edge_analysis,
)
from src.analysis.nations_league_model_lifecycle import (
    FROZEN_ALGORITHM_DIGEST,
    NationsLeagueLifecycleError,
    active_release_from_registry,
    register_and_validate_nations_league_release,
)
from src.analysis.nations_league_result_extension import completeness
from src.analysis.nations_league_v1_1 import (
    MODEL_VERSION,
    training_records_from_timeline,
)
from src.models.lifecycle import ACTIVE, ModelLifecycle, ModelRelease, canonical_digest
from src.utils.atomic_io import atomic_write_json, atomic_write_text

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


def build_fresh_input_state(
    manifest: Mapping[str, Any],
    base_timeline: Mapping[str, Any],
    result_extension: Mapping[str, Any],
    *,
    prediction_cutoff: str,
) -> dict[str, Any]:
    """Build a sealed #229 input-state for one real capture cutoff.

    The result extension is the reviewed official-UEFA continuation input.  A
    completeness proof is recomputed for the exact cutoff and only READY
    evidence can reach the frozen predictor.  In particular, a stale
    committed extension cannot be relabeled as current by the scheduler.
    """

    cutoff = _utc(prediction_cutoff, "prediction_cutoff")
    if manifest.get("competition") != COMPETITION:
        raise NationsLeagueLiveRuntimeError("wrong future fixture manifest")
    expected_manifest_digest = manifest.get("manifest_digest")
    if expected_manifest_digest != _digest(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    ):
        raise NationsLeagueLiveRuntimeError("future fixture manifest digest mismatch")
    try:
        proof = completeness(
            result_extension,
            _stamp(cutoff),
            schedule_manifest=manifest,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise NationsLeagueLiveRuntimeError("official result completeness is invalid") from exc
    if proof.get("status") != "READY":
        raise NationsLeagueLiveRuntimeError(
            f"official result completeness is {proof.get('status', 'UNKNOWN')}"
        )
    targets = []
    for raw in manifest.get("fixtures", []):
        if not isinstance(raw, Mapping) or raw.get("status") != "VERIFIED":
            continue
        if _utc(str(raw.get("kickoff_utc", "")), "kickoff_utc") <= cutoff:
            continue
        targets.append(deepcopy(dict(raw)))
    if not targets:
        raise NationsLeagueLiveRuntimeError("future manifest has no eligible targets")
    base_rows = training_records_from_timeline(base_timeline)
    training_rows = base_rows + deepcopy(list(result_extension.get("result_rows", [])))
    source_digest = _digest(
        {
            "base_timeline_digest": base_timeline.get("dataset_digest"),
            "result_extension_digest": result_extension.get("extension_digest"),
            "completeness_digest": proof.get("completeness_digest"),
        }
    )
    provenance = {
        "base_timeline_digest": base_timeline.get("dataset_digest"),
        "timeline_digest": base_timeline.get("dataset_digest"),
        "result_extension_digest": result_extension.get("extension_digest"),
        "completeness_digest": proof.get("completeness_digest"),
        "source_digest": source_digest,
        "source_provenance": "official UEFA result continuation with sealed completeness proof",
        "capture_cutoff": _stamp(cutoff),
    }
    try:
        return build_input_state(
            targets,
            training_rows,
            prediction_cutoff=_stamp(cutoff),
            provenance=provenance,
            base_timeline=base_timeline,
            result_extension=result_extension,
            completeness_artifact=proof,
            future_manifest=manifest,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise NationsLeagueLiveRuntimeError("fresh causal input-state is invalid") from exc


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
    market_snapshots: Iterable[Mapping[str, Any]] = (),
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
    if (
        active_release.snapshot.algorithm_version != MODEL_VERSION
        or active_release.snapshot.algorithm_digest != FROZEN_ALGORITHM_DIGEST
    ):
        raise NationsLeagueLiveRuntimeError("wrong live model binding")
    shadow = predict_from_input_state(input_state, fixture_id, phase=phase)
    probabilities = _probabilities(shadow["probabilities"])
    fixture = next(row for row in input_state["fixtures"] if row["fixture_id"] == fixture_id)
    bindings = input_state.get("identity_bindings")
    if not isinstance(bindings, Mapping):
        raise NationsLeagueLiveRuntimeError("sealed identity bindings are required")
    home_binding = bindings.get(fixture["home_team"])
    away_binding = bindings.get(fixture["away_team"])
    if (
        not isinstance(home_binding, Mapping)
        or not isinstance(away_binding, Mapping)
        or home_binding.get("resolution") not in {"IDENTITY", "EXPLICIT_EXISTING_ALIAS"}
        or away_binding.get("resolution") not in {"IDENTITY", "EXPLICIT_EXISTING_ALIAS"}
    ):
        raise NationsLeagueLiveRuntimeError("sealed identity bindings are not READY")
    canonical_identity = {
        "home_team": home_binding["canonical_team"],
        "away_team": away_binding["canonical_team"],
    }
    record_id = _digest(
        {
            "fixture_id": fixture_id,
            "phase": phase,
            "release_id": active_release.release_id,
            "input_snapshot_digest": input_state["input_snapshot_digest"],
        }
    )
    prediction_timestamp = _stamp(captured)
    return {
        "record_id": record_id,
        "record_type": "prediction",
        "status": LIVE,
        "competition": COMPETITION,
        "edition": fixture.get("edition"),
        "fixture_id": fixture_id,
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
        "source_identity": {
            "home_team": fixture["home_team"],
            "away_team": fixture["away_team"],
        },
        "canonical_identity": canonical_identity,
        "kickoff_utc": fixture["kickoff_utc"],
        "phase": phase,
        "prediction_timestamp": prediction_timestamp,
        "updated_at": prediction_timestamp,
        "probabilities": probabilities,
        "model_release_id": active_release.release_id,
        "model_version": MODEL_VERSION,
        "model_digest": active_release.snapshot.algorithm_digest,
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
        "edge_analysis": build_edge_analysis(
            probabilities,
            fixture_id=fixture_id,
            phase=phase,
            model_release_id=active_release.release_id,
            prediction_record_id=record_id,
            prediction_timestamp=prediction_timestamp,
            market_snapshots=market_snapshots,
        ),
    }


def refresh_result_state(
    previous: Mapping[str, Any] | None, current_input_state: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a deterministic NO_OP or retrain-required refresh decision."""

    current_digest = current_input_state.get("result_extension_digest")
    if not isinstance(current_digest, str) or len(current_digest) != 64:
        raise NationsLeagueLiveRuntimeError("result refresh requires a sealed extension digest")
    current_training_digest = canonical_digest(current_input_state.get("training_records", []))
    current_state_digest = canonical_digest(current_input_state.get("elo_state", {}))
    previous_digest = previous.get("result_extension_digest") if isinstance(previous, Mapping) else None
    previous_training_digest = previous.get("training_data_digest") if isinstance(previous, Mapping) else None
    previous_state_digest = previous.get("trained_state_digest") if isinstance(previous, Mapping) else None
    if previous_training_digest is not None or previous_state_digest is not None:
        changed = (
            previous_training_digest != current_training_digest
            or previous_state_digest != current_state_digest
        )
    else:
        changed = previous_digest != current_digest
    return {
        "status": "NO_OP" if not changed else "RESULTS_CHANGED",
        "result_extension_digest": current_digest,
        "training_data_digest": current_training_digest,
        "trained_state_digest": current_state_digest,
        "retrain_required": changed,
        "no_bet": True,
        "ledger_mutation": False,
    }


def refresh_and_activate(
    registry: Mapping[str, Any],
    input_state: Mapping[str, Any],
    *,
    source_release_sha: str,
    activated_at: str,
) -> tuple[dict[str, Any], ModelRelease, dict[str, Any]]:
    """Run the existing #240 lifecycle against newly sealed result evidence."""

    try:
        lifecycle_payload = registry["lifecycle"]
        lifecycle = ModelLifecycle.from_payload(lifecycle_payload)
        current = active_release_from_registry(registry)
    except (KeyError, TypeError, ValueError, NationsLeagueLifecycleError) as exc:
        raise NationsLeagueLiveRuntimeError("durable lifecycle registry is invalid") from exc
    previous = {
        "training_data_digest": current.snapshot.training_data_digest,
        "trained_state_digest": current.snapshot.trained_state_digest,
    }
    decision = refresh_result_state(previous, input_state)
    if decision["status"] == "NO_OP":
        return dict(registry), current, decision
    try:
        release, receipt = register_and_validate_nations_league_release(
            lifecycle,
            input_state,
            source_release_sha=source_release_sha,
            created_at=str(input_state["observed_at"]),
        )
        if receipt.outcome != "NO_OP":
            lifecycle.activate(release.release_id, activated_at=activated_at)
        active = lifecycle.releases[lifecycle.pointers["nations_league_v1_1"].release_id]
        updated = deepcopy(dict(registry))
        updated["lifecycle"] = lifecycle.to_payload()
        health = lifecycle.health("nations_league_v1_1", updated_at=activated_at)
        updated["health"] = [health.to_payload()]
        updated.pop("registry_digest", None)
        updated["registry_digest"] = _digest(updated)
    except (KeyError, TypeError, ValueError, NationsLeagueLifecycleError) as exc:
        raise NationsLeagueLiveRuntimeError("result-driven model activation blocked") from exc
    return updated, active, {**decision, "status": "RETRAINED", "receipt": receipt.to_payload()}


def run_live_cycle(
    manifest: Mapping[str, Any],
    input_state: Mapping[str, Any],
    active_release: ModelRelease,
    *,
    as_of: str,
    existing_records: Iterable[Mapping[str, Any]] = (),
    prediction_builder: Callable[..., dict[str, Any]] = build_live_prediction,
    execute: bool = False,
    market_snapshots: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Plan or materialize due LIVE predictions without network side effects."""

    clock = _utc(as_of, "as_of")
    if manifest.get("competition") != COMPETITION:
        raise NationsLeagueLiveRuntimeError("wrong future fixture manifest")
    if active_release.snapshot.algorithm_version != MODEL_VERSION:
        raise NationsLeagueLiveRuntimeError("wrong active release")
    existing = {}
    existing_phases: set[tuple[Any, Any]] = set()
    for record in existing_records:
        if not isinstance(record, Mapping):
            raise NationsLeagueLiveRuntimeError("live store record is malformed")
        key = (
            record.get("fixture_id"),
            record.get("phase"),
            record.get("model_release_id"),
            record.get("input_snapshot_digest") or input_state.get("input_snapshot_digest"),
        )
        if key in existing:
            raise NationsLeagueLiveRuntimeError("conflicting duplicate live record")
        existing[key] = dict(record)
        if record.get("fixture_id") and record.get("phase"):
            existing_phases.add((record.get("fixture_id"), record.get("phase")))
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
            key = (
                fixture["fixture_id"],
                phase,
                active_release.release_id,
                input_state.get("input_snapshot_digest"),
            )
            if key in existing or (fixture["fixture_id"], phase) in existing_phases:
                row["status"] = "ALREADY_CAPTURED"
            elif execute:
                builder_kwargs: dict[str, Any] = {
                    "phase": phase,
                    "active_release": active_release,
                    "captured_at": _stamp(clock),
                }
                if market_snapshots is not None:
                    builder_kwargs["market_snapshots"] = market_snapshots.get(
                        fixture["fixture_id"], ()
                    )
                appended_record = prediction_builder(
                    input_state,
                    fixture["fixture_id"],
                    **builder_kwargs,
                )
                if appended_record.get("status") != LIVE or appended_record.get("no_bet") is not True:
                    raise NationsLeagueLiveRuntimeError("live prediction safety contract failed")
                appended.append(appended_record)
                row["status"] = "MATERIALIZED"
        plan.append(row)
    return {
        "schema": "nations-league-live-cycle-v1",
        "as_of": _stamp(clock),
        "status": (
            "MATERIALIZED"
            if appended
            else "ALREADY_CAPTURED"
            if any(row["status"] == "ALREADY_CAPTURED" for row in plan)
            else "NOT_DUE"
            if plan
            else "NO_OP"
        ),
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


def load_live_store(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise NationsLeagueLiveRuntimeError("LIVE prediction store is invalid") from exc
    if any(not isinstance(row, dict) for row in rows):
        raise NationsLeagueLiveRuntimeError("LIVE prediction store row is malformed")
    for row in rows:
        _validate_live_store_record(row)
    return rows


def _validate_live_store_record(row: Mapping[str, Any]) -> None:
    required = {
        "record_id",
        "fixture_id",
        "phase",
        "model_release_id",
        "input_snapshot_digest",
        "prediction_timestamp",
        "kickoff_utc",
        "probabilities",
        "algorithm_digest",
        "training_data_digest",
        "trained_state_digest",
        "training_cutoff",
        "fixture_source_digest",
        "source_identity",
        "canonical_identity",
    }
    if not required.issubset(row) or row.get("phase") not in {INITIAL, REFINEMENT}:
        raise NationsLeagueLiveRuntimeError("LIVE prediction provenance is incomplete")
    if (
        row.get("status") != LIVE
        or row.get("no_bet") is not True
        or row.get("publication_enabled") is not True
        or row.get("betting_enabled") is not False
        or row.get("ledger_mutation") is not False
    ):
        raise NationsLeagueLiveRuntimeError("LIVE prediction safety contract failed")
    for field in (
        "record_id",
        "model_release_id",
        "input_snapshot_digest",
        "algorithm_digest",
        "training_data_digest",
        "trained_state_digest",
        "fixture_source_digest",
    ):
        value = row.get(field)
        if not isinstance(value, str) or len(value) != 64 or any(
            char not in "0123456789abcdef" for char in value
        ):
            raise NationsLeagueLiveRuntimeError(f"LIVE prediction {field} is invalid")
    if row["algorithm_digest"] != FROZEN_ALGORITHM_DIGEST:
        raise NationsLeagueLiveRuntimeError("LIVE prediction algorithm is invalid")
    for field in ("prediction_timestamp", "kickoff_utc", "training_cutoff"):
        _utc(str(row.get(field, "")), field)
    _probabilities(row.get("probabilities"))
    if "edge_analysis" in row:
        try:
            validate_edge_analysis(
                row["edge_analysis"],
                fixture_id=row["fixture_id"],
                prediction_record_id=row["record_id"],
            )
        except ValueError as exc:
            raise NationsLeagueLiveRuntimeError(str(exc)) from exc
    for field in ("source_identity", "canonical_identity"):
        identity = row.get(field)
        if (
            not isinstance(identity, Mapping)
            or not isinstance(identity.get("home_team"), str)
            or not isinstance(identity.get("away_team"), str)
            or not identity["home_team"].strip()
            or not identity["away_team"].strip()
        ):
            raise NationsLeagueLiveRuntimeError(f"LIVE prediction {field} is invalid")
    expected_record_id = _digest(
        {
            "fixture_id": row["fixture_id"],
            "phase": row["phase"],
            "release_id": row["model_release_id"],
            "input_snapshot_digest": row["input_snapshot_digest"],
        }
    )
    if row["record_id"] != expected_record_id:
        raise NationsLeagueLiveRuntimeError("LIVE prediction record identity is invalid")


def append_live_store(path: Path, existing: Iterable[Mapping[str, Any]], new_records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Atomically append immutable records and reject identity conflicts."""

    rows = [dict(row) for row in existing]
    by_id = {row.get("record_id"): row for row in rows}
    if len(by_id) != len(rows) or None in by_id:
        raise NationsLeagueLiveRuntimeError("LIVE prediction store has duplicate record IDs")
    by_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        _validate_live_store_record(row)
        identity = (row.get("fixture_id"), row.get("phase"), row.get("model_release_id"), row.get("input_snapshot_digest"))
        if identity in by_identity and by_identity[identity] != row:
            raise NationsLeagueLiveRuntimeError("LIVE prediction store has conflicting identity")
        by_identity[identity] = row
    for raw in new_records:
        row = dict(raw)
        record_id = row.get("record_id")
        identity = (row.get("fixture_id"), row.get("phase"), row.get("model_release_id"), row.get("input_snapshot_digest"))
        if record_id in by_id:
            if by_id[record_id] != row:
                raise NationsLeagueLiveRuntimeError("LIVE prediction record substitution")
            continue
        if identity in by_identity:
            raise NationsLeagueLiveRuntimeError("LIVE prediction identity already exists")
        _validate_live_store_record(row)
        by_id[record_id] = row
        by_identity[identity] = row
        rows.append(row)
    if new_records:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))
    return rows

"""Manual, offline lifecycle queue for Nations League forward shadow capture.

The queue consumes a verified future-fixture manifest and local JSONL evidence
only.  It never contacts a provider, starts a scheduler, or mutates production
state.  Planning is the default; local execution is opt-in and delegates the
actual prediction to the frozen ``nations_league_v1`` runner.
"""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.nations_league_forward_input import (
    build_input_state,
    predict_from_input_state,
)
from src.analysis.nations_league_v1 import (
    deterministic_record_id,
    model_digest,
    sha256_json,
)

QUEUE_SCHEMA_VERSION = "nations-league-forward-lifecycle-queue-v1"
DEFAULT_SHADOW_STORE = "results/audits/nations_league_forward_shadow.jsonl"
SHADOW_ONLY = "SHADOW_ONLY"
NO_BET = True
REQUIRED_INPUT_PROVENANCE = (
    "timeline_digest",
    "fixture_source_digest",
    "input_snapshot_digest",
)
UTC = timezone.utc
INITIAL_MINIMUM = timedelta(hours=22)
INITIAL_MAXIMUM = timedelta(hours=26)
REFINEMENT_MINIMUM = timedelta(minutes=60)
REFINEMENT_MAXIMUM = timedelta(minutes=120)


class QueueError(ValueError):
    """Raised when a queue input or idempotency contract fails closed."""


@dataclass(frozen=True)
class FixtureState:
    fixture_id: str
    status: str
    kickoff_utc: str
    lead_seconds: int
    initial_window: tuple[str, str]
    refinement_window: tuple[str, str]


def _parse_utc(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise QueueError(f"{field} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise QueueError(f"{field} must carry UTC timezone")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    # Match #229's canonical ``datetime.isoformat()`` UTC representation.
    return value.astimezone(UTC).isoformat()


def _digest(value: Any, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise QueueError(f"{field} must be a lowercase SHA-256 digest")


def _fixture_rows(manifest: Any) -> list[dict[str, Any]]:
    if isinstance(manifest, dict):
        rows = manifest.get("fixtures")
    else:
        rows = manifest
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise QueueError("manifest must contain a list of fixture objects")
    return [deepcopy(row) for row in rows]


def validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    """Validate the stable subset required by the frozen forward target API."""
    rows = _fixture_rows(manifest)
    seen: set[str] = set()
    for row in rows:
        fixture_id = row.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            raise QueueError("fixture manifest requires fixture_id")
        if fixture_id in seen:
            raise QueueError(f"duplicate fixture_id: {fixture_id}")
        seen.add(fixture_id)
        if row.get("status") != "VERIFIED":
            raise QueueError(f"fixture is not VERIFIED: {fixture_id}")
        for field in (
            "edition",
            "evaluation_block",
            "home_team",
            "away_team",
            "competition",
            "source_provenance",
            "kickoff_utc",
        ):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise QueueError(f"{fixture_id}: {field} is required")
        if row["home_team"] == row["away_team"]:
            raise QueueError(f"{fixture_id}: identical teams")
        if row["competition"] != "UEFA Nations League":
            raise QueueError(f"{fixture_id}: unsupported competition")
        _parse_utc(row["kickoff_utc"], f"{fixture_id}.kickoff_utc")
        _digest(row.get("source_digest"), f"{fixture_id}.source_digest")
        if "neutral" in row and not isinstance(row["neutral"], bool):
            raise QueueError(f"{fixture_id}: neutral must be boolean")
        if "predictions" in row and row["predictions"] != []:
            raise QueueError(f"{fixture_id}: manifest contains predictions")
    return rows


def _window(
    kickoff: datetime, minimum: timedelta, maximum: timedelta
) -> tuple[str, str]:
    return _iso(kickoff - maximum), _iso(kickoff - minimum)


def classify_fixture(fixture: dict[str, Any], as_of: str) -> FixtureState:
    """Classify one verified fixture using inclusive frozen boundaries."""
    now = _parse_utc(as_of, "as_of")
    kickoff = _parse_utc(fixture["kickoff_utc"], "kickoff_utc")
    lead = kickoff - now
    if lead <= timedelta(0):
        status = "STARTED"
    elif INITIAL_MINIMUM <= lead <= INITIAL_MAXIMUM:
        status = "INITIAL_DUE"
    elif REFINEMENT_MINIMUM <= lead <= REFINEMENT_MAXIMUM:
        status = "REFINEMENT_DUE"
    elif lead > INITIAL_MAXIMUM:
        status = "TOO_EARLY"
    else:
        status = "BETWEEN_WINDOWS" if lead > REFINEMENT_MAXIMUM else "TOO_LATE"
    return FixtureState(
        fixture_id=fixture["fixture_id"],
        status=status,
        kickoff_utc=_iso(kickoff),
        lead_seconds=int(lead.total_seconds()),
        initial_window=_window(kickoff, INITIAL_MINIMUM, INITIAL_MAXIMUM),
        refinement_window=_window(kickoff, REFINEMENT_MINIMUM, REFINEMENT_MAXIMUM),
    )


def _phase_for_status(status: str) -> str | None:
    return {"INITIAL_DUE": "initial", "REFINEMENT_DUE": "refinement"}.get(status)


def _prediction_key(record: dict[str, Any]) -> tuple[str, str, str]:
    phase = record.get("phase")
    if phase not in ("initial", "refinement"):
        raise QueueError("stored prediction has invalid phase")
    return record.get("fixture_id", ""), phase, record.get("model_digest", "")


def read_shadow_store(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise QueueError(
                        f"shadow store line {line_number} is not an object"
                    )
                rows.append(value)
    except json.JSONDecodeError as exc:
        raise QueueError(f"invalid shadow store JSONL: {path}") from exc
    return rows


def _capture_index(
    records: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record.get("record_type", "prediction") != "prediction":
            continue
        key = _prediction_key(record)[:2]
        if key in index:
            raise QueueError(f"conflicting duplicate capture: {key[0]}:{key[1]}")
        index[key] = record
    return index


def _capture_status(
    fixture_id: str,
    phase: str,
    expected_digest: str,
    index: dict[tuple[str, str], dict[str, Any]],
) -> str:
    existing = index.get((fixture_id, phase))
    if existing is None:
        return "DUE"
    if existing.get("model_digest") != expected_digest:
        raise QueueError(f"conflicting duplicate capture: {fixture_id}:{phase}")
    expected_record_id = deterministic_record_id(fixture_id, phase)
    if existing.get("record_id") != expected_record_id:
        raise QueueError(f"conflicting duplicate record identity: {fixture_id}:{phase}")
    return "ALREADY_CAPTURED"


def _plan_id(payload: dict[str, Any]) -> str:
    return sha256_json(payload)


def build_capture_plan(
    manifest: Any,
    *,
    as_of: str,
    destination_shadow_store: str = DEFAULT_SHADOW_STORE,
    existing_records: list[dict[str, Any]] | None = None,
    model_digest_value: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic, side-effect-free queue plan."""
    now = _parse_utc(as_of, "as_of")
    rows = validate_manifest(manifest)
    frozen_digest = model_digest()
    if model_digest_value is not None:
        _digest(model_digest_value, "model_digest")
        if model_digest_value != frozen_digest:
            raise QueueError("model digest is not the frozen Nations League v1 digest")
    records = existing_records or []
    index = _capture_index(records)
    states = [classify_fixture(row, _iso(now)) for row in rows]
    plans: list[dict[str, Any]] = []
    for state in states:
        phase = _phase_for_status(state.status)
        if phase is None:
            continue
        plan: dict[str, Any] = {
            "schema": QUEUE_SCHEMA_VERSION,
            "fixture_id": state.fixture_id,
            "lifecycle": phase.upper(),
            "phase": phase,
            "prediction_cutoff": _iso(now),
            "kickoff_utc": state.kickoff_utc,
            "model_digest": frozen_digest,
            "required_input_provenance": list(REQUIRED_INPUT_PROVENANCE),
            "destination_shadow_store": destination_shadow_store,
            "signal_status": SHADOW_ONLY,
            "no_bet": NO_BET,
            "publication_enabled": False,
            "ledger_mutation": False,
        }
        plan["record_id"] = deterministic_record_id(state.fixture_id, phase)
        plan["status"] = _capture_status(state.fixture_id, phase, frozen_digest, index)
        plan["plan_id"] = _plan_id(plan)
        plans.append(plan)
    return {
        "schema": QUEUE_SCHEMA_VERSION,
        "as_of": _iso(now),
        "model_digest": frozen_digest,
        "destination_shadow_store": destination_shadow_store,
        "plans": plans,
        "states": [state.__dict__ for state in states],
        "summary": summarize(states, plans, rows, captured_keys=index),
    }


def summarize(
    states: list[FixtureState],
    plans: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    captured_keys: Any = (),
) -> dict[str, Any]:
    row_by_id = {row["fixture_id"]: row for row in rows}
    captured_key_set = set(captured_keys)
    captured = sorted(
        f"{fixture_id}:{phase.upper()}" for fixture_id, phase in captured_key_set
    )
    captured_fixture_ids = {fixture_id for fixture_id, _ in captured_key_set}
    due = [plan for plan in plans if plan["status"] == "DUE"]
    missed = [
        state.fixture_id
        for state in states
        if state.status == "TOO_LATE"
        or (state.status == "STARTED" and state.fixture_id not in captured_fixture_ids)
    ]
    next_initial = sorted(
        (
            state.initial_window[0],
            state.fixture_id,
            row_by_id[state.fixture_id]["home_team"],
            row_by_id[state.fixture_id]["away_team"],
        )
        for state in states
        if state.status == "TOO_EARLY"
        and (state.fixture_id, "initial") not in captured_key_set
    )
    next_refinement = sorted(
        (
            state.refinement_window[0],
            state.fixture_id,
            row_by_id[state.fixture_id]["home_team"],
            row_by_id[state.fixture_id]["away_team"],
        )
        for state in states
        if state.status in {"TOO_EARLY", "BETWEEN_WINDOWS"}
        and (state.fixture_id, "refinement") not in captured_key_set
    )
    return {
        "state_counts": dict(Counter(state.status for state in states)),
        "next_initial": next_initial[0] if next_initial else None,
        "next_refinement": next_refinement[0] if next_refinement else None,
        "due_now": [plan["fixture_id"] + ":" + plan["lifecycle"] for plan in due],
        "missed": missed,
        "captured": captured,
        "plan_count": len(plans),
    }


def execute_plan(
    manifest: Any,
    plan: dict[str, Any],
    *,
    input_state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Execute only due entries through the #229 READY-gated input state."""
    rows = validate_manifest(manifest)
    if plan.get("model_digest") != model_digest():
        raise QueueError("plan model digest is not frozen")
    store = Path(plan["destination_shadow_store"])
    records = read_shadow_store(store)
    index = _capture_index(records)
    by_id = {row["fixture_id"]: row for row in rows}
    due_items: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for item in plan.get("plans", []):
        fixture_id = item["fixture_id"]
        phase = item["phase"]
        status = _capture_status(fixture_id, phase, model_digest(), index)
        if status == "ALREADY_CAPTURED":
            results.append({"fixture_id": fixture_id, "phase": phase, "status": status})
            continue
        if fixture_id not in by_id:
            raise QueueError(f"plan fixture is absent from manifest: {fixture_id}")
        due_items.append(item)

    if not due_items:
        return results

    if not isinstance(input_state, dict):
        raise QueueError("execute requires a #229 input-state object")
    snapshot_cutoff = input_state.get("prediction_cutoff")
    plan_cutoff = plan.get("as_of")
    if snapshot_cutoff != plan_cutoff or any(
        item["prediction_cutoff"] != plan_cutoff for item in due_items
    ):
        raise QueueError("input-state, plan, and --as-of prediction cutoffs differ")
    try:
        rebuilt = build_input_state(
            input_state["fixtures"],
            input_state["training_records"],
            prediction_cutoff=snapshot_cutoff,
            provenance=input_state["provenance"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise QueueError(f"#229 input-state rebuild failed: {exc}") from exc
    if rebuilt != input_state:
        raise QueueError("#229 input-state digest mismatch")
    if rebuilt.get("model_digest") != model_digest():
        raise QueueError("#229 input-state model digest is not frozen")

    input_fixtures = {fixture["fixture_id"]: fixture for fixture in rebuilt["fixtures"]}
    records_to_append: list[dict[str, Any]] = []
    for item in due_items:
        fixture_id = item["fixture_id"]
        fixture = by_id[fixture_id]
        snapshot_fixture = input_fixtures.get(fixture_id)
        if snapshot_fixture is None:
            raise QueueError(f"#229 input-state fixture is absent: {fixture_id}")
        for field in (
            "fixture_id",
            "edition",
            "evaluation_block",
            "home_team",
            "away_team",
            "kickoff_utc",
            "competition",
            "source_digest",
        ):
            if snapshot_fixture.get(field) != fixture.get(field):
                raise QueueError(
                    f"#228 manifest binding mismatch: {fixture_id}:{field}"
                )
        readiness = rebuilt.get("team_readiness", {})
        for team in (fixture["home_team"], fixture["away_team"]):
            if readiness.get(team) != "READY":
                raise QueueError(
                    f"#229 input-state is not READY: {fixture_id}:{team}:{readiness.get(team)}"
                )
        try:
            record = predict_from_input_state(rebuilt, fixture_id, phase=phase)
        except (KeyError, TypeError, ValueError) as exc:
            raise QueueError(f"#229 prediction blocked: {fixture_id}:{exc}") from exc
        if (
            record.get("model_digest") != model_digest()
            or record.get("shadow") is not True
            or record.get("no_bet") is not True
            or record.get("publication_enabled") is not False
            or record.get("ledger_mutation") is not False
        ):
            raise QueueError("offline prediction safety contract failed")
        if record.get("prediction_timestamp") != item["prediction_cutoff"]:
            raise QueueError("prediction cutoff binding failed")
        records_to_append.append(record)

    for record in records_to_append:
        fixture_id = record["fixture_id"]
        phase = record["phase"]
        store.parent.mkdir(parents=True, exist_ok=True)
        with store.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        index[(fixture_id, phase)] = record
        results.append({"fixture_id": fixture_id, "phase": phase, "status": "CAPTURED"})
    return results


def render_summary(plan: dict[str, Any]) -> str:
    summary = plan["summary"]

    def _window(value: Any) -> str:
        return (
            "NONE"
            if value is None
            else f"{value[1]} {value[2]} vs {value[3]} (opens {value[0]})"
        )

    return "\n".join(
        (
            f"NEXT INITIAL: {_window(summary['next_initial'])}",
            f"NEXT REFINEMENT: {_window(summary['next_refinement'])}",
            "DUE NOW: " + (", ".join(summary["due_now"]) or "NONE"),
            "MISSED: " + (", ".join(summary["missed"]) or "NONE"),
            "CAPTURED: " + (", ".join(summary["captured"]) or "NONE"),
        )
    )

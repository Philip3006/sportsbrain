"""Causal, non-betting market-edge evidence for the Nations League LIVE path.

This module is intentionally a pure input/materialization seam.  It accepts
already captured market evidence, selects only a snapshot available at the
prediction cutoff, and records transparent model-vs-market measurements.  It
never acquires odds, sizes a position, or mutates financial state.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from src.betting.odds_utils import remove_margin_shin
from src.config import canonical_name

EDGE_SCHEMA = "nations-league-live-edge-v1"
SNAPSHOT_SCHEMA = "nations-league-live-market-snapshot-v1"
SNAPSHOT_BATCH_SCHEMA = "nations-league-live-market-snapshot-batch-v1"
MAX_MARKET_AGE = timedelta(minutes=15)
OUTCOMES = ("home", "draw", "away")
EDGE_STATUSES = frozenset(
    {"EDGE_MEASURED", "NO_EDGE", "NO_MARKET_SNAPSHOT", "MARKET_STALE", "MARKET_INVALID"}
)


class NationsLeagueLiveEdgeError(ValueError):
    """Market evidence or edge analysis violates the causal contract."""


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise NationsLeagueLiveEdgeError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NationsLeagueLiveEdgeError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise NationsLeagueLiveEdgeError(f"{field} must be explicit UTC")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _round(value: float) -> float:
    return round(float(value), 6)


def _digest(value: Any) -> str:
    try:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NationsLeagueLiveEdgeError("edge value is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NationsLeagueLiveEdgeError(f"{field} is required")
    return value


def _probabilities(
    value: object, field: str = "probabilities", *, tolerance: float = 1e-9
) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(OUTCOMES):
        raise NationsLeagueLiveEdgeError(f"{field} is incomplete")
    result: dict[str, float] = {}
    for outcome in OUTCOMES:
        item = value[outcome]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise NationsLeagueLiveEdgeError(f"{field}.{outcome} is not numeric")
        number = float(item)
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise NationsLeagueLiveEdgeError(f"{field}.{outcome} is invalid")
        result[outcome] = number
    if abs(sum(result.values()) - 1.0) > tolerance:
        raise NationsLeagueLiveEdgeError(f"{field} is not normalized")
    return result


def build_market_snapshot(
    raw: Mapping[str, Any], *, fixture_id: str | None = None
) -> dict[str, Any]:
    """Validate and canonicalize one already-captured 1X2 market snapshot."""

    if not isinstance(raw, Mapping):
        raise NationsLeagueLiveEdgeError("market snapshot is malformed")
    provider = _text(raw.get("provider", raw.get("source")), "provider")
    bookmaker = _text(raw.get("bookmaker"), "bookmaker")
    captured_at = _text(raw.get("captured_at"), "captured_at")
    _utc(captured_at, "captured_at")
    target_fixture_id = _text(fixture_id or raw.get("fixture_id"), "fixture_id")
    odds = raw.get("odds_decimal")
    if not isinstance(odds, Mapping) or set(odds) != set(OUTCOMES):
        raise NationsLeagueLiveEdgeError("complete 1X2 odds are required")
    normalized_odds: dict[str, float] = {}
    for outcome in OUTCOMES:
        item = odds[outcome]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise NationsLeagueLiveEdgeError(f"odds_decimal.{outcome} is not numeric")
        number = float(item)
        if not math.isfinite(number) or number <= 1:
            raise NationsLeagueLiveEdgeError(f"odds_decimal.{outcome} must be greater than 1")
        normalized_odds[outcome] = _round(number)
    odds_tuple = tuple(normalized_odds[outcome] for outcome in OUTCOMES)
    overround = _round(sum(1.0 / value for value in odds_tuple) - 1.0)
    fair = remove_margin_shin(odds_tuple)
    fair_values = [_round(fair[index]) for index in range(len(OUTCOMES))]
    fair_values[-1] = _round(1.0 - sum(fair_values[:-1]))
    body: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "provider": provider,
        "bookmaker": bookmaker,
        "captured_at": _stamp(_utc(captured_at, "captured_at")),
        "fixture_id": target_fixture_id,
        "odds_decimal": dict(zip(OUTCOMES, odds_tuple, strict=True)),
        "overround": overround,
        "margin_free_probabilities": {
            outcome: fair_values[index] for index, outcome in enumerate(OUTCOMES)
        },
    }
    provider_match_id = raw.get("provider_match_id")
    if provider_match_id is not None:
        body["provider_match_id"] = _text(provider_match_id, "provider_match_id")
    body["snapshot_digest"] = _digest(body)
    return body


def _snapshot_digest_body(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != "snapshot_digest"}


def validate_market_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an immutable canonical snapshot, including its digest."""

    normalized = build_market_snapshot(snapshot)
    if dict(snapshot) != normalized:
        raise NationsLeagueLiveEdgeError("market snapshot is not canonical")
    if snapshot.get("snapshot_digest") != _digest(_snapshot_digest_body(snapshot)):
        raise NationsLeagueLiveEdgeError("market snapshot digest mismatch")
    return dict(snapshot)


def _iter_snapshots(raw_snapshots: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    normalized: list[dict[str, Any]] = []
    invalid = False
    for raw in raw_snapshots:
        try:
            normalized.append(validate_market_snapshot(raw))
        except NationsLeagueLiveEdgeError:
            invalid = True
    return normalized, invalid


def select_causal_market_snapshot(
    raw_snapshots: Iterable[Mapping[str, Any]],
    *,
    fixture_id: str,
    prediction_timestamp: str,
) -> tuple[dict[str, Any] | None, str]:
    """Select the newest valid snapshot at or before the prediction cutoff."""

    prediction = _utc(prediction_timestamp, "prediction_timestamp")
    valid: list[dict[str, Any]] = []
    invalid = False
    for raw in raw_snapshots:
        try:
            snapshot = validate_market_snapshot(raw)
        except NationsLeagueLiveEdgeError:
            invalid = True
            continue
        if snapshot["fixture_id"] != fixture_id:
            continue
        captured = _utc(snapshot["captured_at"], "captured_at")
        if captured <= prediction:
            valid.append(snapshot)
    if not valid:
        return None, "MARKET_INVALID" if invalid else "NO_MARKET_SNAPSHOT"
    selected = max(valid, key=lambda item: (item["captured_at"], item["snapshot_digest"]))
    age = prediction - _utc(selected["captured_at"], "captured_at")
    if age > MAX_MARKET_AGE:
        return None, "MARKET_STALE"
    return selected, "VALID"


def _edge_measurements(
    model_probabilities: Mapping[str, float], snapshot: Mapping[str, Any]
) -> dict[str, dict[str, float]]:
    model = _probabilities(model_probabilities, "model_probabilities")
    market = _probabilities(
        snapshot["margin_free_probabilities"], "market_probabilities", tolerance=1e-6
    )
    odds = snapshot["odds_decimal"]
    return {
        outcome: {
            "decimal_odds": odds[outcome],
            "model_probability": model_value,
            "market_probability": market_value,
            "probability_edge": _round(model_value - market_value),
            "ev": _round(model_value * odds[outcome] - 1.0),
        }
        for outcome in OUTCOMES
        for model_value, market_value in [(_round(model[outcome]), _round(market[outcome]))]
    }


def build_edge_analysis(
    model_probabilities: Mapping[str, float],
    *,
    fixture_id: str,
    phase: str,
    model_release_id: str,
    prediction_record_id: str,
    prediction_timestamp: str,
    market_snapshots: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build immutable raw edge measurements without an actionability threshold."""

    fixture = _text(fixture_id, "fixture_id")
    phase_value = _text(phase, "phase")
    release = _text(model_release_id, "model_release_id")
    record = _text(prediction_record_id, "prediction_record_id")
    evaluated = _stamp(_utc(prediction_timestamp, "prediction_timestamp"))
    model = _probabilities(model_probabilities, "model_probabilities")
    snapshot, selection_status = select_causal_market_snapshot(
        market_snapshots, fixture_id=fixture, prediction_timestamp=evaluated
    )
    result: dict[str, Any] = {
        "schema": EDGE_SCHEMA,
        "fixture_id": fixture,
        "phase": phase_value,
        "model_release_id": release,
        "prediction_record_id": record,
        "evaluated_at": evaluated,
        "market_snapshot": None,
        "outcomes": {},
        "candidate_outcomes": [],
        "highest_edge_outcome": None,
        "edge_status": selection_status if selection_status != "VALID" else "NO_EDGE",
        "no_bet": True,
        "betting_enabled": False,
        "ledger_mutation": False,
    }
    if snapshot is not None:
        measurements = _edge_measurements(model, snapshot)
        candidates = [
            outcome
            for outcome in OUTCOMES
            if measurements[outcome]["probability_edge"] > 0
            and measurements[outcome]["ev"] > 0
        ]
        result["market_snapshot"] = snapshot
        result["outcomes"] = measurements
        result["candidate_outcomes"] = candidates
        result["highest_edge_outcome"] = max(
            OUTCOMES,
            key=lambda outcome: (
                measurements[outcome]["probability_edge"],
                measurements[outcome]["ev"],
                -OUTCOMES.index(outcome),
            ),
        )
        result["edge_status"] = "EDGE_MEASURED" if candidates else "NO_EDGE"
    result["edge_digest"] = _digest({key: value for key, value in result.items() if key != "edge_digest"})
    return result


def no_market_edge_analysis(
    *,
    fixture_id: str,
    phase: str,
    model_release_id: str,
    prediction_record_id: str,
    prediction_timestamp: str,
) -> dict[str, Any]:
    return build_edge_analysis(
        {"home": 1 / 3, "draw": 1 / 3, "away": 1 / 3},
        fixture_id=fixture_id,
        phase=phase,
        model_release_id=model_release_id,
        prediction_record_id=prediction_record_id,
        prediction_timestamp=prediction_timestamp,
    )


def validate_edge_analysis(
    value: Mapping[str, Any], *, fixture_id: str, prediction_record_id: str
) -> dict[str, Any]:
    """Validate a stored edge record and reject private financial fields."""

    if not isinstance(value, Mapping):
        raise NationsLeagueLiveEdgeError("edge analysis is malformed")
    forbidden = {"stake", "bankroll", "kelly", "position", "bet_id"}
    if forbidden.intersection(value):
        raise NationsLeagueLiveEdgeError("edge analysis contains private financial state")
    expected = {
        "schema", "fixture_id", "phase", "model_release_id", "prediction_record_id",
        "evaluated_at", "market_snapshot", "outcomes", "candidate_outcomes",
        "highest_edge_outcome", "edge_status", "no_bet", "betting_enabled",
        "ledger_mutation", "edge_digest",
    }
    if set(value) != expected:
        raise NationsLeagueLiveEdgeError("edge analysis fields are not allowlisted")
    if value["schema"] != EDGE_SCHEMA or value["fixture_id"] != fixture_id:
        raise NationsLeagueLiveEdgeError("edge analysis identity is invalid")
    if value["prediction_record_id"] != prediction_record_id:
        raise NationsLeagueLiveEdgeError("edge analysis record binding is invalid")
    _text(value["phase"], "edge phase")
    _text(value["model_release_id"], "edge model_release_id")
    _utc(value["evaluated_at"], "edge evaluated_at")
    if value["edge_status"] not in EDGE_STATUSES:
        raise NationsLeagueLiveEdgeError("edge status is invalid")
    if value["no_bet"] is not True or value["betting_enabled"] is not False or value["ledger_mutation"] is not False:
        raise NationsLeagueLiveEdgeError("edge safety contract is invalid")
    if not isinstance(value["candidate_outcomes"], list) or any(item not in OUTCOMES for item in value["candidate_outcomes"]):
        raise NationsLeagueLiveEdgeError("edge candidates are invalid")
    if value["highest_edge_outcome"] is not None and value["highest_edge_outcome"] not in OUTCOMES:
        raise NationsLeagueLiveEdgeError("highest edge outcome is invalid")
    snapshot = value["market_snapshot"]
    if snapshot is not None:
        validate_market_snapshot(snapshot)
        if _utc(snapshot["captured_at"], "captured_at") > _utc(value["evaluated_at"], "edge evaluated_at"):
            raise NationsLeagueLiveEdgeError("market snapshot is after prediction cutoff")
    if not isinstance(value["outcomes"], Mapping):
        raise NationsLeagueLiveEdgeError("edge outcomes are malformed")
    if snapshot is None and value["outcomes"]:
        raise NationsLeagueLiveEdgeError("edge outcomes exist without a market snapshot")
    if snapshot is not None:
        if set(value["outcomes"]) != set(OUTCOMES):
            raise NationsLeagueLiveEdgeError("edge outcomes are incomplete")
        for outcome in OUTCOMES:
            row = value["outcomes"][outcome]
            if not isinstance(row, Mapping) or set(row) != {
                "decimal_odds", "model_probability", "market_probability", "probability_edge", "ev"
            }:
                raise NationsLeagueLiveEdgeError("edge measurement is malformed")
            for field in row:
                if isinstance(row[field], bool) or not isinstance(row[field], (int, float)) or not math.isfinite(float(row[field])):
                    raise NationsLeagueLiveEdgeError("edge measurement is not numeric")
                if field == "decimal_odds" and float(row[field]) <= 1:
                    raise NationsLeagueLiveEdgeError("edge decimal odds are invalid")
            expected_edge = _round(row["model_probability"] - row["market_probability"])
            expected_ev = _round(row["model_probability"] * row["decimal_odds"] - 1.0)
            if row["probability_edge"] != expected_edge or row["ev"] != expected_ev:
                raise NationsLeagueLiveEdgeError("edge measurement formula mismatch")
            if row["market_probability"] != snapshot["margin_free_probabilities"][outcome]:
                raise NationsLeagueLiveEdgeError("edge market probability mismatch")
        expected_candidates = [
            outcome
            for outcome in OUTCOMES
            if value["outcomes"][outcome]["probability_edge"] > 0
            and value["outcomes"][outcome]["ev"] > 0
        ]
        if value["candidate_outcomes"] != expected_candidates:
            raise NationsLeagueLiveEdgeError("edge candidate classification mismatch")
        expected_highest = max(
            OUTCOMES,
            key=lambda outcome: (
                value["outcomes"][outcome]["probability_edge"],
                value["outcomes"][outcome]["ev"],
                -OUTCOMES.index(outcome),
            ),
        )
        if value["highest_edge_outcome"] != expected_highest:
            raise NationsLeagueLiveEdgeError("highest edge classification mismatch")
        expected_status = "EDGE_MEASURED" if expected_candidates else "NO_EDGE"
        if value["edge_status"] != expected_status:
            raise NationsLeagueLiveEdgeError("edge status classification mismatch")
    elif value["candidate_outcomes"] or value["highest_edge_outcome"] is not None or value["edge_status"] not in {
        "NO_MARKET_SNAPSHOT", "MARKET_STALE", "MARKET_INVALID"
    }:
        raise NationsLeagueLiveEdgeError("edge status does not match market availability")
    expected_digest = _digest({key: value[key] for key in value if key != "edge_digest"})
    if value["edge_digest"] != expected_digest:
        raise NationsLeagueLiveEdgeError("edge analysis digest mismatch")
    return dict(value)


def normalize_market_snapshot_input(
    raw: object, *, fixtures: Iterable[Mapping[str, Any]] = ()
) -> dict[str, list[dict[str, Any]]]:
    """Load a local canonical batch or the existing iSports shadow artifact."""

    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, Mapping) and raw.get("schema") == SNAPSHOT_BATCH_SCHEMA:
        rows = raw.get("snapshots")
    elif isinstance(raw, Mapping) and raw.get("schema") == "nations-league-isports-shadow-v2":
        fixture_index: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        for row in fixtures:
            if not isinstance(row, Mapping):
                continue
            try:
                key = (
                    canonical_name(_text(row.get("home_team"), "home_team")),
                    canonical_name(_text(row.get("away_team"), "away_team")),
                    _stamp(_utc(row.get("kickoff_utc"), "kickoff_utc")),
                )
            except NationsLeagueLiveEdgeError:
                continue
            if key in fixture_index:
                raise NationsLeagueLiveEdgeError("market fixture identity mapping is ambiguous")
            fixture_index[key] = row
        rows = []
        for item in raw.get("fixtures", []):
            if not isinstance(item, Mapping) or not isinstance(item.get("market"), Mapping):
                continue
            kickoff = item.get("kickoff_utc", item.get("kickoff"))
            try:
                item_key = (
                    canonical_name(_text(item.get("home_team"), "home_team")),
                    canonical_name(_text(item.get("away_team"), "away_team")),
                    _stamp(_utc(kickoff, "kickoff")),
                )
            except NationsLeagueLiveEdgeError:
                continue
            target = fixture_index.get(item_key)
            if target is None:
                continue
            market = item["market"]
            rows.append(
                {
                    "provider": raw.get("provider", "isports_api"),
                    "bookmaker": market.get("bookmaker"),
                    "captured_at": item.get("captured_at", raw.get("captured_at")),
                    "fixture_id": target.get("fixture_id"),
                    "provider_match_id": item.get("provider_match_id"),
                    "odds_decimal": market.get("odds_decimal"),
                }
            )
    else:
        raise NationsLeagueLiveEdgeError("unsupported market snapshot input schema")
    if not isinstance(rows, list):
        raise NationsLeagueLiveEdgeError("market snapshot rows are missing")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        snapshot = build_market_snapshot(row)
        grouped.setdefault(snapshot["fixture_id"], []).append(snapshot)
    return {key: sorted(value, key=lambda item: (item["captured_at"], item["snapshot_digest"])) for key, value in sorted(grouped.items())}

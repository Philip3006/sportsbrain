"""Immutable post-prediction market evidence for the Nations League LIVE view.

An enrichment is deliberately separate from a prediction record.  It binds a
later, valid market observation to the already-issued prediction without
rewriting its probabilities, timestamp, or identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.analysis.nations_league_live_edge import (
    build_edge_analysis,
    validate_edge_analysis,
    validate_market_snapshot,
)
from src.analysis.nations_league_live_runtime import due_phase
from src.utils.atomic_io import atomic_write_text

SCHEMA = "nations-league-live-market-enrichment-v1"
_FIELDS = frozenset(
    {
        "schema",
        "fixture_id",
        "phase",
        "prediction_record_id",
        "model_release_id",
        "prediction_timestamp",
        "captured_at",
        "snapshot_digest",
        "edge_analysis",
        "no_bet",
        "betting_enabled",
        "ledger_mutation",
        "enrichment_id",
    }
)


class NationsLeagueLiveMarketEnrichmentError(ValueError):
    """Market evidence cannot be bound immutably to a LIVE prediction."""


def _digest(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment is not canonical JSON"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NationsLeagueLiveMarketEnrichmentError(f"{field} is required")
    return value.strip()


def _utc(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NationsLeagueLiveMarketEnrichmentError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise NationsLeagueLiveMarketEnrichmentError(f"{field} must be explicit UTC")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest_text(value: object, field: str) -> str:
    result = _text(value, field)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise NationsLeagueLiveMarketEnrichmentError(
            f"{field} must be a SHA-256 digest"
        )
    return result


def _enrichment_digest_body(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "enrichment_id"}


def _identity(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _text(value.get("fixture_id"), "fixture_id"),
        _text(value.get("phase"), "phase"),
        _digest_text(value.get("prediction_record_id"), "prediction_record_id"),
        _stamp(_utc(value.get("captured_at"), "captured_at")),
    )


def _record_identity(record: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _text(record.get("fixture_id"), "record.fixture_id"),
        _text(record.get("phase"), "record.phase"),
        _digest_text(record.get("record_id"), "record.record_id"),
        _text(record.get("model_release_id"), "record.model_release_id"),
    )


def record_has_valid_market_edge(record: Mapping[str, Any]) -> bool:
    """Return whether a LIVE prediction already has usable market evidence."""

    edge = record.get("edge_analysis")
    if not isinstance(edge, Mapping):
        return False
    try:
        validate_edge_analysis(
            edge,
            fixture_id=_text(record.get("fixture_id"), "record.fixture_id"),
            prediction_record_id=_digest_text(
                record.get("record_id"), "record.record_id"
            ),
        )
    except (TypeError, ValueError):
        return False
    return edge.get("market_snapshot") is not None


def validate_market_enrichment(
    value: Mapping[str, Any], *, record: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validate an enrichment and, when supplied, its prediction binding."""

    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment fields are not allowlisted"
        )
    result = dict(value)
    if result["schema"] != SCHEMA:
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment schema is invalid"
        )
    if (
        result["no_bet"] is not True
        or result["betting_enabled"] is not False
        or result["ledger_mutation"] is not False
    ):
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment safety contract is invalid"
        )
    phase = _text(result.get("phase"), "phase")
    if phase not in {"initial", "refinement"}:
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment phase is invalid"
        )
    fixture_id = _text(result.get("fixture_id"), "fixture_id")
    prediction_record_id = _digest_text(
        result.get("prediction_record_id"), "prediction_record_id"
    )
    model_release_id = _digest_text(result.get("model_release_id"), "model_release_id")
    prediction_timestamp = _stamp(
        _utc(result.get("prediction_timestamp"), "prediction_timestamp")
    )
    captured_at = _stamp(_utc(result.get("captured_at"), "captured_at"))
    if _utc(captured_at, "captured_at") < _utc(
        prediction_timestamp, "prediction_timestamp"
    ):
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment cannot backdate market evidence"
        )
    snapshot_digest = _digest_text(result.get("snapshot_digest"), "snapshot_digest")
    try:
        edge = validate_edge_analysis(
            result.get("edge_analysis"),
            fixture_id=fixture_id,
            prediction_record_id=prediction_record_id,
        )
        snapshot = edge.get("market_snapshot")
        if snapshot is None:
            raise NationsLeagueLiveMarketEnrichmentError(
                "market enrichment requires a market snapshot"
            )
        snapshot = validate_market_snapshot(snapshot)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, NationsLeagueLiveMarketEnrichmentError):
            raise
        raise NationsLeagueLiveMarketEnrichmentError(str(exc)) from exc
    if snapshot["snapshot_digest"] != snapshot_digest:
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment snapshot binding differs"
        )
    if edge["model_release_id"] != model_release_id or edge["phase"] != phase:
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment edge binding differs"
        )
    if edge["evaluated_at"] != captured_at or snapshot["captured_at"] != captured_at:
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment capture timing differs"
        )
    body = _enrichment_digest_body(result)
    if result.get("enrichment_id") != _digest(body):
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment digest mismatch"
        )
    if record is not None:
        record_fixture, record_phase, record_id, record_release = _record_identity(
            record
        )
        if (
            record.get("status") != "LIVE"
            or record_fixture != fixture_id
            or record_phase != phase
            or record_id != prediction_record_id
            or record_release != model_release_id
        ):
            raise NationsLeagueLiveMarketEnrichmentError(
                "market enrichment prediction binding differs"
            )
        if (
            _stamp(
                _utc(record.get("prediction_timestamp"), "record.prediction_timestamp")
            )
            != prediction_timestamp
        ):
            raise NationsLeagueLiveMarketEnrichmentError(
                "market enrichment prediction timestamp differs"
            )
        if snapshot["fixture_id"] != fixture_id:
            raise NationsLeagueLiveMarketEnrichmentError(
                "market enrichment snapshot fixture differs"
            )
        if (
            due_phase(
                _text(record.get("kickoff_utc"), "record.kickoff_utc"), captured_at
            )[0]
            != phase
        ):
            raise NationsLeagueLiveMarketEnrichmentError(
                "market enrichment is outside its lifecycle phase"
            )
    return result


def build_market_enrichment(
    record: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a new immutable edge enrichment for an existing LIVE record."""

    validated_snapshot = validate_market_snapshot(snapshot)
    fixture_id, phase, record_id, model_release_id = _record_identity(record)
    captured_at = validated_snapshot["captured_at"]
    if record.get("status") != "LIVE":
        raise NationsLeagueLiveMarketEnrichmentError(
            "only LIVE prediction records may receive market enrichment"
        )
    prediction_timestamp = _stamp(
        _utc(record.get("prediction_timestamp"), "record.prediction_timestamp")
    )
    if _utc(captured_at, "captured_at") < _utc(
        prediction_timestamp, "prediction_timestamp"
    ):
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment cannot backdate market evidence"
        )
    if (
        due_phase(_text(record.get("kickoff_utc"), "record.kickoff_utc"), captured_at)[
            0
        ]
        != phase
    ):
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment is outside its lifecycle phase"
        )
    probabilities = record.get("probabilities")
    edge = build_edge_analysis(
        probabilities,
        fixture_id=fixture_id,
        phase=phase,
        model_release_id=model_release_id,
        prediction_record_id=record_id,
        prediction_timestamp=captured_at,
        market_snapshots=[validated_snapshot],
    )
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "fixture_id": fixture_id,
        "phase": phase,
        "prediction_record_id": record_id,
        "model_release_id": model_release_id,
        "prediction_timestamp": prediction_timestamp,
        "captured_at": captured_at,
        "snapshot_digest": validated_snapshot["snapshot_digest"],
        "edge_analysis": edge,
        "no_bet": True,
        "betting_enabled": False,
        "ledger_mutation": False,
    }
    body["enrichment_id"] = _digest(body)
    return validate_market_enrichment(body, record=record)


def load_market_enrichments(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment store is invalid"
        ) from exc
    if any(not isinstance(row, dict) for row in rows):
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment row is malformed"
        )
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        validate_market_enrichment(row)
        identity = _identity(row)
        if identity in seen:
            raise NationsLeagueLiveMarketEnrichmentError(
                "market enrichment identity is duplicated"
            )
        seen.add(identity)
    return rows


def append_market_enrichments(
    path: Path,
    existing: Iterable[Mapping[str, Any]],
    new_enrichments: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Append immutable enrichments; exact repeats are idempotent."""

    rows = [validate_market_enrichment(dict(row)) for row in existing]
    by_id = {row["enrichment_id"]: row for row in rows}
    by_identity = {_identity(row): row for row in rows}
    if len(by_id) != len(rows) or len(by_identity) != len(rows):
        raise NationsLeagueLiveMarketEnrichmentError(
            "market enrichment store has duplicate identity"
        )
    changed = False
    for raw in new_enrichments:
        row = validate_market_enrichment(dict(raw))
        existing_by_id = by_id.get(row["enrichment_id"])
        if existing_by_id is not None:
            if existing_by_id != row:
                raise NationsLeagueLiveMarketEnrichmentError(
                    "market enrichment substitution"
                )
            continue
        identity = _identity(row)
        existing_by_identity = by_identity.get(identity)
        if existing_by_identity is not None:
            if existing_by_identity != row:
                raise NationsLeagueLiveMarketEnrichmentError(
                    "market enrichment identity conflict"
                )
            continue
        by_id[row["enrichment_id"]] = row
        by_identity[identity] = row
        rows.append(row)
        changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            "".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in rows
            ),
        )
    return rows


def select_market_enrichment(
    enrichments: Iterable[Mapping[str, Any]], record: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Select the newest valid enrichment for one immutable record."""

    record_id = _digest_text(record.get("record_id"), "record.record_id")
    candidates: list[dict[str, Any]] = []
    for raw in enrichments:
        if not isinstance(raw, Mapping):
            raise NationsLeagueLiveMarketEnrichmentError(
                "market enrichment row is malformed"
            )
        if raw.get("prediction_record_id") != record_id:
            continue
        candidates.append(validate_market_enrichment(raw, record=record))
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row["captured_at"], row["enrichment_id"]))


def market_enrichment_keys(
    enrichments: Iterable[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    """Return lifecycle keys already covered by valid market enrichments."""

    result: set[tuple[str, str]] = set()
    for raw in enrichments:
        row = validate_market_enrichment(raw)
        result.add((_text(row["fixture_id"], "fixture_id"), row["phase"]))
    return result

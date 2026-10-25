"""Provider-free adapters for SportsBrain prediction and result records.

These adapters only normalize already supplied records.  They never acquire
scores, inspect credentials, write a ledger, publish output, or mutate an
existing prediction store.  A caller must supply all authoritative provenance
and model identity required by the universal contracts; missing provenance is
reported as an explicit conversion failure.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from src.betting.tennis_settlement import settle_tennis_market
from src.learning.outcome_contracts import (
    AuthoritativeResultV1,
    LifecycleError,
    PredictionSnapshotV1,
    SettlementState,
    build_outcome_attachment,
    canonical_digest,
)


class AdapterError(LifecycleError):
    """Raised when an upstream record cannot satisfy immutable provenance."""


def _required_text(raw: Mapping[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"missing {field}")
    return value.strip()


def _required_digest(raw: Mapping[str, Any], field: str) -> str:
    value = _required_text(raw, field)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise AdapterError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _utc(raw: Mapping[str, Any], field: str) -> str:
    value = _required_text(raw, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdapterError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise AdapterError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_record_id(raw: Mapping[str, Any], *, source_system: str) -> str:
    value = raw.get("source_record_id") or raw.get("record_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    fixture = raw.get("fixture_id") or raw.get("match_id")
    market = raw.get("market") or raw.get("selection")
    timestamp = raw.get("prediction_timestamp") or raw.get("scan_ts")
    if not all(
        isinstance(item, str) and item.strip() for item in (fixture, market, timestamp)
    ):
        raise AdapterError("source record identity is missing")
    return canonical_digest(
        {
            "source_system": source_system,
            "fixture_id": fixture,
            "market": market,
            "timestamp": timestamp,
        }
    )


def _prediction_id(identity: Mapping[str, Any]) -> str:
    return canonical_digest(identity)


def _argmax(probabilities: Mapping[str, Any]) -> str:
    values: dict[str, float] = {}
    for key in ("home", "draw", "away"):
        value = probabilities.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AdapterError("probabilities are incomplete")
        values[key] = float(value)
    if (
        any(value < 0 or value > 1 for value in values.values())
        or abs(sum(values.values()) - 1) > 1e-9
    ):
        raise AdapterError("probabilities are not normalized")
    return {"home": "HOME", "draw": "DRAW", "away": "AWAY"}[max(values, key=values.get)]


def _prediction(
    *,
    raw: Mapping[str, Any],
    source_system: str,
    fixture_id: str,
    sport: str,
    competition: str,
    model_family: str,
    model_release_id: str,
    phase: str,
    prediction_timestamp: str,
    feature_cutoff: str,
    prediction: Mapping[str, Any],
    source_record_id: str,
) -> PredictionSnapshotV1:
    payload = dict(prediction)
    payload.setdefault("source_system", source_system)
    payload_digest = canonical_digest({"prediction": payload})
    identity = {
        "schema": "sportsbrain-prediction-snapshot-v1",
        "signal_id": str(raw.get("signal_id") or source_record_id),
        "fixture_id": fixture_id,
        "sport": sport,
        "competition": competition,
        "model_family": model_family,
        "model_release_id": model_release_id,
        "lifecycle_version": str(raw.get("lifecycle_version") or phase),
        "phase": phase,
        "prediction_timestamp": prediction_timestamp,
        "feature_cutoff": feature_cutoff,
        "prediction_digest": payload_digest,
        "source_record_id": source_record_id,
    }
    return PredictionSnapshotV1(
        prediction_id=_prediction_id(identity),
        signal_id=identity["signal_id"],
        fixture_id=fixture_id,
        sport=sport,
        competition=competition,
        model_family=model_family,
        model_release_id=model_release_id,
        lifecycle_version=identity["lifecycle_version"],
        phase=phase,
        prediction_timestamp=prediction_timestamp,
        feature_cutoff=feature_cutoff,
        prediction=payload,
        prediction_digest=payload_digest,
        source_record_id=source_record_id,
    )


def nations_league_prediction_from_record(
    raw: Mapping[str, Any],
) -> PredictionSnapshotV1:
    """Convert one NL INITIAL or REFINEMENT record without mutating its store."""

    if raw.get("record_type") != "prediction":
        raise AdapterError("Nations League record is not a prediction")
    fixture_id = _required_text(raw, "fixture_id")
    phase = _required_text(raw, "phase").upper()
    if phase not in {"INITIAL", "REFINEMENT"}:
        raise AdapterError("Nations League phase is unsupported")
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, Mapping):
        raise AdapterError("Nations League probabilities are missing")
    model_family = _required_text(raw, "model_version")
    model_release_id = _required_digest(raw, "model_release_id")
    prediction_timestamp = _utc(raw, "prediction_timestamp")
    provenance = raw.get("source_provenance")
    if not isinstance(provenance, Mapping):
        raise AdapterError("Nations League source provenance is missing")
    feature_cutoff = _utc(provenance, "capture_cutoff")
    source_record_id = _source_record_id(raw, source_system="nations_league")
    payload = {
        "market": "1X2",
        "selection": _argmax(probabilities),
        "probabilities": dict(probabilities),
        "model_digest": _required_digest(raw, "model_digest"),
        "no_bet": raw.get("no_bet") is True,
        "publication_enabled": raw.get("publication_enabled") is True,
        "betting_enabled": raw.get("betting_enabled") is True,
    }
    return _prediction(
        raw=raw,
        source_system="nations_league",
        fixture_id=fixture_id,
        sport="football",
        competition=_required_text(raw, "competition"),
        model_family=model_family,
        model_release_id=model_release_id,
        phase=phase,
        prediction_timestamp=prediction_timestamp,
        feature_cutoff=feature_cutoff,
        prediction=payload,
        source_record_id=source_record_id,
    )


def legacy_signal_prediction_from_record(
    raw: Mapping[str, Any], *, source_system: str
) -> PredictionSnapshotV1:
    """Convert a legacy signal only when model/time provenance is present."""

    fixture_id = str(raw.get("fixture_id") or raw.get("match_id") or "").strip()
    if not fixture_id:
        raise AdapterError("fixture identity is missing")
    sport = _required_text(raw, "sport").lower()
    competition = str(raw.get("competition") or raw.get("league") or "").strip()
    if not competition:
        raise AdapterError("competition provenance is missing")
    model_family = str(
        raw.get("model_family") or raw.get("model_version") or ""
    ).strip()
    if not model_family:
        raise AdapterError("model_family provenance is missing")
    model_release_id = str(
        raw.get("model_release_id") or raw.get("model_id") or ""
    ).strip()
    if not model_release_id:
        raise AdapterError("model_release_id provenance is missing")
    prediction_timestamp = _utc(raw, "prediction_timestamp")
    feature_cutoff = _utc(raw, "feature_cutoff")
    market = _required_text(raw, "market")
    source_record_id = _source_record_id(raw, source_system=source_system)
    payload = {
        "market": market,
        "selection": str(raw.get("selection") or market),
        "model_prob": raw.get("model_prob"),
        "fair_prob": raw.get("fair_prob"),
        "decimal_odds": raw.get("decimal_odds"),
        "confidence": raw.get("confidence"),
        "no_bet": raw.get("no_bet") is True,
        "placed": raw.get("placed") is True,
    }
    for field in ("features", "feature_available_at", "training_cutoff"):
        if field in raw:
            payload[field] = raw[field]
    return _prediction(
        raw=raw,
        source_system=source_system,
        fixture_id=fixture_id,
        sport=sport,
        competition=competition,
        model_family=model_family,
        model_release_id=model_release_id,
        phase=str(raw.get("phase") or "LEGACY"),
        prediction_timestamp=prediction_timestamp,
        feature_cutoff=feature_cutoff,
        prediction=payload,
        source_record_id=source_record_id,
    )


def tennis_prediction_from_record(raw: Mapping[str, Any]) -> PredictionSnapshotV1:
    return legacy_signal_prediction_from_record(raw, source_system="tennis")


def bundesliga2_prediction_from_record(raw: Mapping[str, Any]) -> PredictionSnapshotV1:
    return legacy_signal_prediction_from_record(raw, source_system="bundesliga2")


def generic_football_prediction_from_record(
    raw: Mapping[str, Any],
) -> PredictionSnapshotV1:
    return legacy_signal_prediction_from_record(raw, source_system="generic_football")


def top5_prediction_from_record(raw: Mapping[str, Any]) -> PredictionSnapshotV1:
    if (
        raw.get("production_activation_authorized") is True
        or raw.get("betting_authorized") is True
    ):
        raise AdapterError("Top-5 production authority cannot enter settlement adapter")
    if raw.get("no_bet") is not True:
        raise AdapterError("Top-5 settlement evidence must be no-bet")
    return legacy_signal_prediction_from_record(raw, source_system="top5")


def prediction_from_record(
    raw: Mapping[str, Any], *, source_system: str
) -> PredictionSnapshotV1:
    adapters = {
        "nations_league": nations_league_prediction_from_record,
        "tennis": tennis_prediction_from_record,
        "bundesliga2": bundesliga2_prediction_from_record,
        "generic_football": generic_football_prediction_from_record,
        "top5": top5_prediction_from_record,
    }
    adapter = adapters.get(source_system)
    if adapter is None:
        raise AdapterError(f"unsupported prediction source_system: {source_system}")
    return adapter(raw)


def football_result_from_record(
    raw: Mapping[str, Any],
    *,
    fixture_id: str,
    competition: str,
    source: str,
    source_record_id: str,
    completed_at: str,
    result_safe_available_at: str,
    provenance_digest: str,
) -> AuthoritativeResultV1:
    status = str(raw.get("status") or "completed")
    home_score = raw.get("home_score")
    away_score = raw.get("away_score")
    terminal_without_score = {"cancelled", "void", "walkover"}
    if status not in terminal_without_score:
        if isinstance(home_score, bool) or not isinstance(home_score, int):
            raise AdapterError("football home_score is missing")
        if isinstance(away_score, bool) or not isinstance(away_score, int):
            raise AdapterError("football away_score is missing")
    actual = {
        "status": status,
        "home_score": home_score,
        "away_score": away_score,
    }
    if actual["status"] not in {
        "completed",
        "finished",
        "final",
        "cancelled",
        "void",
        "walkover",
    }:
        raise AdapterError("football result is not result-safe")
    digest = canonical_digest(actual)
    identity = {
        "schema": "sportsbrain-authoritative-result-v1",
        "fixture_id": fixture_id,
        "sport": "football",
        "competition": competition,
        "source": source,
        "source_record_id": source_record_id,
        "completed_at": completed_at,
        "result_safe_available_at": result_safe_available_at,
        "result_digest": digest,
        "provenance_digest": provenance_digest,
    }
    return AuthoritativeResultV1(
        result_id=canonical_digest(identity),
        fixture_id=fixture_id,
        sport="football",
        competition=competition,
        source=source,
        source_record_id=source_record_id,
        completed_at=completed_at,
        result_safe_available_at=result_safe_available_at,
        actual_result=actual,
        result_digest=digest,
        provenance_digest=provenance_digest,
    )


def tennis_result_from_record(
    raw: Mapping[str, Any],
    *,
    fixture_id: str,
    competition: str,
    source: str,
    source_record_id: str,
    completed_at: str,
    result_safe_available_at: str,
    provenance_digest: str,
) -> AuthoritativeResultV1:
    status = _required_text(raw, "status")
    if status in {"scheduled", "in_progress", "suspended", "postponed"}:
        raise AdapterError("tennis result is not result-safe")
    actual = {
        "status": status,
        "sets": [list(item) for item in raw.get("sets", [])],
        "winner": raw.get("winner"),
        "retired_by": raw.get("retired_by"),
        "best_of": raw.get("best_of", 3),
    }
    digest = canonical_digest(actual)
    identity = {
        "schema": "sportsbrain-authoritative-result-v1",
        "fixture_id": fixture_id,
        "sport": "tennis",
        "competition": competition,
        "source": source,
        "source_record_id": source_record_id,
        "completed_at": completed_at,
        "result_safe_available_at": result_safe_available_at,
        "result_digest": digest,
        "provenance_digest": provenance_digest,
    }
    return AuthoritativeResultV1(
        result_id=canonical_digest(identity),
        fixture_id=fixture_id,
        sport="tennis",
        competition=competition,
        source=source,
        source_record_id=source_record_id,
        completed_at=completed_at,
        result_safe_available_at=result_safe_available_at,
        actual_result=actual,
        result_digest=digest,
        provenance_digest=provenance_digest,
    )


def resolve_prediction_outcome(
    actual_result: Mapping[str, Any], prediction: Mapping[str, Any]
) -> str:
    """Resolve a market using existing pure settlement semantics only."""

    status = str(actual_result.get("status") or "")
    if status == "cancelled":
        return SettlementState.CANCELLED
    if status in {"walkover", "postponed", "void"}:
        return SettlementState.VOID
    market = str(prediction.get("market") or "")
    if prediction.get("sport") == "tennis":
        value = settle_tennis_market(market, dict(actual_result))
    elif market == "1X2":
        home_score = actual_result.get("home_score")
        away_score = actual_result.get("away_score")
        if not isinstance(home_score, int) or not isinstance(away_score, int):
            raise AdapterError("football 1X2 result scores are missing")
        actual = (
            "HOME"
            if home_score > away_score
            else "AWAY"
            if away_score > home_score
            else "DRAW"
        )
        value = "won" if actual == prediction.get("selection") else "lost"
    else:
        from scripts.settle_bets import settle_market

        value = settle_market(
            market, actual_result["home_score"], actual_result["away_score"]
        )
    if value in {"won", "lost", "push"}:
        return {"push": SettlementState.PUSH}.get(value, value)
    raise AdapterError(f"market {market!r} cannot be settled from supplied result")


def attach_prediction(
    prediction: PredictionSnapshotV1,
    result: AuthoritativeResultV1,
    *,
    settled_at: str,
    provenance_digest: str,
):
    def resolver(actual: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
        return resolve_prediction_outcome(
            actual,
            {**payload, "sport": prediction.sport},
        )

    return build_outcome_attachment(
        prediction,
        result,
        settled_at=settled_at,
        provenance_digest=provenance_digest,
        outcome_resolver=resolver,
    )

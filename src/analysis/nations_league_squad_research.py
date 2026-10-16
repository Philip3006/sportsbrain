"""Causal, offline-only Nations League squad/lineup research contracts.

This module is deliberately not imported by the scanner, signal detector,
feature builder, runtime, publication, or betting paths. It consumes already
captured evidence; it does not fetch provider data.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import random
import stat
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import canonical_name

SNAPSHOT_SCHEMA = "nl-squad-lineup-snapshot-v1"
FEATURE_SCHEMA = "nl-squad-lineup-research-feature-v1"
REPORT_SCHEMA = "nl-squad-lineup-research-report-v1"
CAPTURE_STORE_SCHEMA = "nl-squad-forward-capture-store-v1"
REAL_EVIDENCE = "REAL_OBSERVED"
TEST_EVIDENCE = "TEST_FIXTURE"
MAX_STATUS_AGE = timedelta(hours=24)
HIGH_QUALITY_MAX_AGE = timedelta(hours=6)
MAX_LINEUP_AGE = timedelta(hours=6)
BOOTSTRAP_REPLICATES = 1000
OUT_STATUSES = {"injured", "suspended", "unavailable", "out"}
AVAILABLE_STATUSES = {"fit", "available"}
POSITION_ALIASES = {
    "GK": "GK",
    "Goalkeeper": "Goalkeeper",
    "keeper": "Goalkeeper",
    "DEF": "DEF",
    "Defender": "Defender",
    "Centre-Back": "Centre-Back",
    "Left-Back": "Left-Back",
    "Right-Back": "Right-Back",
    "MID": "MID",
    "Midfielder": "Midfielder",
    "Central Midfield": "Central Midfield",
    "Defensive Midfield": "Defensive Midfield",
    "Attacking Midfield": "Attacking Midfield",
    "FWD": "FWD",
    "Forward": "Forward",
    "Centre-Forward": "Centre-Forward",
    "Left Winger": "Left Winger",
    "Right Winger": "Right Winger",
    "Second Striker": "Second Striker",
    "unknown": "unknown",
}
POSITION_GROUPS = {
    "goalkeeper": {"GK", "Goalkeeper", "keeper"},
    "defender": {"DEF", "Defender", "Centre-Back", "Left-Back", "Right-Back"},
    "midfielder": {
        "MID",
        "Midfielder",
        "Central Midfield",
        "Defensive Midfield",
        "Attacking Midfield",
    },
    "forward": {
        "FWD",
        "Forward",
        "Centre-Forward",
        "Left Winger",
        "Right Winger",
        "Second Striker",
    },
}
ABLATION_VARIANTS = (
    "baseline",
    "availability",
    "positional_absences",
    "weighted_player_impact",
    "squad_market_value",
    "lineup_strength",
    "rotation_load",
    "full_candidate",
)
FORWARD_CAPTURE_SLOTS = {"T24H", "T6H", "T90M", "CONFIRMED_LINEUP"}
FORWARD_CAPTURE_WINDOWS_HOURS = {
    "T24H": (22.0, 26.0),
    "T6H": (5.0, 7.0),
    "T90M": (1.0, 2.0),
}
FORWARD_CAPTURE_WINDOWS_HOURS = {
    "T24H": (22.0, 26.0),
    "T6H": (5.0, 7.0),
    "T90M": (1.0, 2.0),
}


class ResearchContractError(ValueError):
    """Raised when evidence cannot safely support a causal feature."""


def canonical_json_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ResearchContractError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchContractError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchContractError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _number(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchContractError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ResearchContractError(f"{field} must be finite")
    if minimum is not None and result < minimum:
        raise ResearchContractError(f"{field} is below its valid range")
    if maximum is not None and result > maximum:
        raise ResearchContractError(f"{field} is above its valid range")
    return result


def _canonical_team(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchContractError(f"{field} is required")
    return canonical_name(value.strip())


def _validate_source_catalog(
    snapshot: Mapping[str, Any], prediction_at: datetime
) -> dict[str, dict[str, Any]]:
    sources = snapshot.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ResearchContractError("sources must contain timestamped provenance")
    result: dict[str, dict[str, Any]] = {}
    captured_at = _utc(snapshot.get("observed_at"), "observed_at")
    if captured_at > prediction_at:
        raise ResearchContractError("snapshot was observed after prediction time")
    for source in sources:
        if not isinstance(source, dict):
            raise ResearchContractError("source provenance entry is invalid")
        source_id = source.get("source_id")
        if (
            not isinstance(source_id, str)
            or not source_id.strip()
            or source_id in result
        ):
            raise ResearchContractError("source_id must be unique and non-empty")
        observed = _utc(source.get("observed_at"), "source.observed_at")
        source_as_of = _utc(source.get("source_as_of"), "source.source_as_of")
        if (
            observed > captured_at
            or source_as_of > observed
            or observed > prediction_at
        ):
            raise ResearchContractError(
                "source provenance is future-dated or inconsistent"
            )
        if (
            not isinstance(source.get("record_id"), str)
            or not source["record_id"].strip()
        ):
            raise ResearchContractError("source record_id is required")
        source_digest = source.get("source_sha256")
        if (
            not isinstance(source_digest, str)
            or len(source_digest) != 64
            or any(char not in "0123456789abcdef" for char in source_digest)
        ):
            raise ResearchContractError(
                "source_sha256 must be a lowercase SHA-256 digest"
            )
        result[source_id] = {**source, "_observed": observed, "_as_of": source_as_of}
    return result


def _resolve_player(
    player: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    prediction_at: datetime,
) -> dict[str, Any]:
    player_id = player.get("player_id")
    if not isinstance(player_id, str) or not player_id.strip():
        raise ResearchContractError("every player needs a stable player_id")
    name = player.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ResearchContractError("every player needs a display name")
    raw_position = player.get("position", "unknown")
    if raw_position not in POSITION_ALIASES:
        raise ResearchContractError("player position is not in the research contract")
    roster_source = player.get("roster_source_id")
    if roster_source not in sources:
        raise ResearchContractError("player identity/position needs a roster source")
    claims = player.get("status_claims", [])
    if not isinstance(claims, list):
        raise ResearchContractError("status_claims must be a list")
    normalized_claims: list[tuple[str, float | None]] = []
    claim_times: list[datetime] = []
    claim_sources: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise ResearchContractError("player status claim is invalid")
        source_id = claim.get("source_id")
        if source_id not in sources:
            raise ResearchContractError("player status claim references unknown source")
        observed = _utc(claim.get("observed_at"), "status_claim.observed_at")
        if (
            observed > prediction_at
            or observed > sources[source_id]["_observed"]
            or observed > sources[source_id]["_as_of"]
        ):
            raise ResearchContractError("player status claim is future-dated")
        status = claim.get("status")
        if status not in AVAILABLE_STATUSES | OUT_STATUSES | {
            "doubtful",
            "probable",
            "unknown",
        }:
            raise ResearchContractError("player status is unsupported")
        availability_raw = claim.get("availability")
        availability = (
            None
            if availability_raw is None
            else _number(
                availability_raw, "status_claim.availability", minimum=0.0, maximum=1.0
            )
        )
        if status in AVAILABLE_STATUSES and availability != 1.0:
            raise ResearchContractError(
                "fit/available claims must state availability=1"
            )
        if status in OUT_STATUSES and availability != 0.0:
            raise ResearchContractError(
                "out/injured/suspended claims must state availability=0"
            )
        if status in {"doubtful", "probable"} and availability in (None, 0.0, 1.0):
            raise ResearchContractError(
                "doubtful/probable claims need explicit intermediate availability"
            )
        normalized_claims.append((status, availability))
        claim_times.append(observed)
        claim_sources.add(source_id)
    conflict = len(set(normalized_claims)) > 1
    resolved_status: str | None = None
    resolved_availability: float | None = None
    if normalized_claims and not conflict:
        resolved_status, resolved_availability = normalized_claims[0]
        if resolved_status == "unknown":
            resolved_availability = None
    key_evidence = player.get("key_player_evidence")
    key_player: bool | None = None
    if key_evidence is not None:
        if not isinstance(key_evidence, dict) or not isinstance(
            key_evidence.get("value"), bool
        ):
            raise ResearchContractError(
                "key-player evidence must contain a boolean value"
            )
        key_source = key_evidence.get("source_id")
        key_observed = _utc(
            key_evidence.get("observed_at"), "key_player_evidence.observed_at"
        )
        if (
            key_source not in sources
            or key_observed > prediction_at
            or key_observed > sources[key_source]["_as_of"]
        ):
            raise ResearchContractError(
                "key-player evidence is not point-in-time bound"
            )
        key_player = key_evidence["value"]

    value = player.get("market_value")
    normalized_value: float | None = None
    value_source: str | None = None
    if value is not None:
        if not isinstance(value, dict):
            raise ResearchContractError("market_value evidence is malformed")
        normalized_value = _number(
            value.get("value_eur_m"), "market_value.value_eur_m", minimum=0.0
        )
        value_source = value.get("source_id")
        if value_source not in sources:
            raise ResearchContractError("market value references unknown source")
        value_as_of = _utc(value.get("as_of"), "market_value.as_of")
        if (
            value_as_of > prediction_at
            or value_as_of > sources[value_source]["_observed"]
            or value_as_of > sources[value_source]["_as_of"]
        ):
            raise ResearchContractError("market value is not point-in-time safe")

    strength = player.get("player_strength")
    normalized_strength: float | None = None
    if strength is not None:
        if not isinstance(strength, dict):
            raise ResearchContractError("player_strength evidence is malformed")
        normalized_strength = _number(
            strength.get("score"), "player_strength.score", minimum=0, maximum=1
        )
        if strength.get("source_id") not in sources:
            raise ResearchContractError("player strength references unknown source")
        strength_as_of = _utc(strength.get("as_of"), "player_strength.as_of")
        if (
            strength_as_of > prediction_at
            or strength_as_of > sources[strength["source_id"]]["_observed"]
            or strength_as_of > sources[strength["source_id"]]["_as_of"]
        ):
            raise ResearchContractError("player strength is future-dated")
        if (
            not isinstance(strength.get("methodology_id"), str)
            or not strength["methodology_id"].strip()
        ):
            raise ResearchContractError(
                "player strength needs a named, versioned methodology"
            )

    return {
        **player,
        "_position": POSITION_ALIASES[raw_position],
        "_status": resolved_status,
        "_availability": resolved_availability,
        "_conflict": conflict,
        "_claim_times": claim_times,
        "_claim_sources": claim_sources | {roster_source},
        "_key_player": key_player,
        "_market_value": normalized_value,
        "_value_source": value_source,
        "_strength": normalized_strength,
    }


def _validate_team(
    team: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    prediction_at: datetime,
) -> dict[str, Any]:
    if not isinstance(team, dict):
        raise ResearchContractError("team snapshot is invalid")
    scope = team.get("roster_scope")
    complete = team.get("roster_complete")
    expected = team.get("expected_player_count")
    players = team.get("players")
    if scope not in {"matchday_squad", "national_roster"}:
        raise ResearchContractError("roster_scope is unsupported")
    if (
        not isinstance(complete, bool)
        or not isinstance(expected, int)
        or isinstance(expected, bool)
        or expected <= 0
    ):
        raise ResearchContractError(
            "roster completeness and expected_player_count are required"
        )
    if not isinstance(players, list) or len(players) > expected:
        raise ResearchContractError("players must be a bounded list")
    normalized = [_resolve_player(p, sources, prediction_at) for p in players]
    if len({p["player_id"] for p in normalized}) != len(normalized):
        raise ResearchContractError("duplicate player_id in team snapshot")
    if complete and len(normalized) != expected:
        raise ResearchContractError("roster_complete conflicts with player count")
    return {
        "roster_scope": scope,
        "roster_complete": complete,
        "expected_player_count": expected,
        "players": normalized,
    }


def _validate_lineup(
    lineup: Any,
    team: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    prediction_at: datetime,
) -> dict[str, Any]:
    if not isinstance(lineup, dict) or lineup.get("status") not in {
        "confirmed",
        "probable",
        "unavailable",
    }:
        raise ResearchContractError(
            "lineup status must be confirmed, probable, or unavailable"
        )
    status = lineup["status"]
    announced_at = lineup.get("announced_at")
    source_id = lineup.get("source_id")
    ids = lineup.get("player_ids", [])
    if source_id is not None and source_id not in sources:
        raise ResearchContractError("lineup references unknown source")
    if status == "unavailable":
        if ids:
            raise ResearchContractError("unavailable lineup cannot contain player_ids")
        return {"status": status, "player_ids": set(), "announced": None}
    announced = _utc(announced_at, "lineup.announced_at")
    if (
        announced > prediction_at
        or source_id is None
        or announced > sources[source_id]["_as_of"]
    ):
        raise ResearchContractError(
            "lineup evidence is not timestamped before prediction"
        )
    if not isinstance(ids, list) or len(ids) != 11 or len(set(ids)) != 11:
        raise ResearchContractError(
            "lineup evidence must contain exactly 11 distinct players"
        )
    roster_ids = {p["player_id"] for p in team["players"]}
    if not set(ids).issubset(roster_ids):
        raise ResearchContractError(
            "lineup contains a player outside the captured squad"
        )
    return {"status": status, "player_ids": set(ids), "announced": announced}


def _validate_match_history(
    team: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    prediction_at: datetime,
) -> tuple[list[dict[str, Any]], datetime | None]:
    complete_since_raw = team.get("match_history_complete_since")
    if complete_since_raw is None:
        complete_since = None
    else:
        complete_since = _utc(complete_since_raw, "match_history_complete_since")
        history_source = team.get("match_history_source_id")
        if (
            history_source not in sources
            or complete_since > sources[history_source]["_as_of"]
        ):
            raise ResearchContractError(
                "match history completeness needs timestamped source provenance"
            )
        if complete_since > prediction_at - timedelta(days=14):
            complete_since = None
    matches = team.get("prior_international_matches", [])
    if not isinstance(matches, list):
        raise ResearchContractError("prior_international_matches must be a list")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in matches:
        if not isinstance(match, dict):
            raise ResearchContractError("prior match history entry is malformed")
        fixture_id = match.get("fixture_id")
        source_id = match.get("source_id")
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in seen:
            raise ResearchContractError("prior match fixture_id must be unique")
        if source_id not in sources:
            raise ResearchContractError("prior match references unknown source")
        kickoff = _utc(match.get("kickoff_at"), "prior_match.kickoff_at")
        finished = _utc(match.get("finished_at"), "prior_match.finished_at")
        observed = _utc(match.get("observed_at"), "prior_match.observed_at")
        if not kickoff < finished <= prediction_at or observed > prediction_at:
            raise ResearchContractError(
                "prior match history is not known before prediction"
            )
        if (
            observed < finished
            or observed > sources[source_id]["_observed"]
            or observed > sources[source_id]["_as_of"]
        ):
            raise ResearchContractError(
                "prior match result was not captured after completion and before prediction"
            )
        players = match.get("players")
        if not isinstance(players, list) or not players:
            raise ResearchContractError("prior match needs player-minute evidence")
        normalized_players = []
        player_ids: set[str] = set()
        for row in players:
            if not isinstance(row, dict) or not isinstance(row.get("player_id"), str):
                raise ResearchContractError("prior match player identity is invalid")
            player_id = row["player_id"]
            minutes = _number(
                row.get("minutes"), "prior_match.minutes", minimum=0, maximum=120
            )
            started = row.get("started")
            if not isinstance(started, bool) or player_id in player_ids:
                raise ResearchContractError(
                    "prior match starter/minutes evidence is invalid"
                )
            player_ids.add(player_id)
            normalized_players.append(
                {"player_id": player_id, "minutes": minutes, "started": started}
            )
        starters = {p["player_id"] for p in normalized_players if p["started"]}
        if len(starters) != 11:
            raise ResearchContractError("prior match must identify exactly 11 starters")
        parsed.append(
            {
                "fixture_id": fixture_id,
                "kickoff": kickoff,
                "finished": finished,
                "players": normalized_players,
                "starters": starters,
            }
        )
        seen.add(fixture_id)
    parsed.sort(key=lambda m: (m["kickoff"], m["fixture_id"]))
    return parsed, complete_since


def _team_quality(
    team: Mapping[str, Any], lineup: Mapping[str, Any], prediction_at: datetime
) -> tuple[str, float, int, float | None, float | None]:
    players = team["players"]
    expected = team["expected_player_count"]
    coverage = len(players) / expected if expected else 0.0
    conflicts = sum(p["_conflict"] for p in players)
    valid_claim_times = [t for p in players for t in p["_claim_times"]]
    latest = max(valid_claim_times) if valid_claim_times else None
    age_hours = (prediction_at - latest).total_seconds() / 3600 if latest else None
    complete = (
        team["roster_scope"] == "matchday_squad"
        and team["roster_complete"]
        and len(players) == expected
        and all(p["_availability"] is not None for p in players)
        and conflicts == 0
        and age_hours is not None
        and age_hours <= MAX_STATUS_AGE.total_seconds() / 3600
    )
    if (
        complete
        and coverage == 1.0
        and age_hours <= HIGH_QUALITY_MAX_AGE.total_seconds() / 3600
        and len({s for p in players for s in p["_claim_sources"]}) >= 2
    ):
        quality = "HIGH"
    elif complete:
        quality = "MEDIUM"
    elif players:
        quality = "LOW"
    else:
        quality = "UNAVAILABLE"
    lineup_age = None
    if lineup["announced"] is not None:
        lineup_age = (prediction_at - lineup["announced"]).total_seconds() / 3600
        if lineup_age > MAX_LINEUP_AGE.total_seconds() / 3600:
            lineup["status"] = "stale"
            lineup["player_ids"] = set()
    return quality, coverage, conflicts, age_hours, lineup_age


def _side_features(
    team: Mapping[str, Any],
    lineup: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    history_complete_since: datetime | None,
    prediction_at: datetime,
) -> dict[str, Any]:
    players = team["players"]
    team_quality, coverage, conflicts, age_hours, lineup_age_hours = _team_quality(
        team, lineup, prediction_at
    )
    usable = team_quality in {"HIGH", "MEDIUM"}
    positions_known = all(p["_position"] != "unknown" for p in players)
    result: dict[str, Any] = {
        "data_quality": team_quality,
        "source_count": len({sid for p in players for sid in p["_claim_sources"]}),
        "expected_player_coverage": coverage,
        "conflicting_player_status_count": conflicts,
        "status_age_hours": age_hours,
        "lineup_age_hours": lineup_age_hours,
        "lineup_status": lineup["status"],
        "fallback_usage": False,
    }
    metrics: dict[str, Any] = {
        "squad_availability": None,
        "unavailable_player_count": None,
        "unavailable_starter_count": None,
        "goalkeeper_absence": None,
        "defender_absence": None,
        "midfielder_absence": None,
        "forward_absence": None,
        "weighted_impact_lost": None,
        "key_player_risk": None,
        "squad_market_value": None,
        "starting_xi_strength": None,
        "bench_strength": None,
        "returning_starter_count": None,
        "missing_usual_starter_count": None,
        "starter_turnover_count": None,
        "starter_turnover_rate": None,
        "previous_match_starting_xi_minutes": None,
        "cumulative_international_minutes_7d": None,
        "cumulative_international_minutes_14d": None,
        "days_since_last_international_match": None,
        "matches_last_7d": None,
        "matches_last_14d": None,
    }
    if usable:
        metrics["squad_availability"] = sum(p["_availability"] for p in players) / len(
            players
        )
        metrics["unavailable_player_count"] = sum(
            p["_availability"] == 0.0 for p in players
        )
        if positions_known:
            for name, positions in POSITION_GROUPS.items():
                metrics[f"{name}_absence"] = sum(
                    p["_availability"] == 0.0 and p["_position"] in positions
                    for p in players
                )
        if all(p["_key_player"] is not None for p in players):
            metrics["key_player_risk"] = sum(
                p["_key_player"] and p["_availability"] < 1.0 for p in players
            )
        values = [p["_market_value"] for p in players]
        if all(value is not None for value in values):
            metrics["squad_market_value"] = sum(values)
        lineup_ids = lineup["player_ids"] if lineup["status"] == "confirmed" else set()
        if lineup_ids:
            starters = [p for p in players if p["player_id"] in lineup_ids]
            bench = [p for p in players if p["player_id"] not in lineup_ids]
            metrics["unavailable_starter_count"] = (
                sum(
                    p["_availability"] == 0.0
                    for p in starters
                    if p["_availability"] is not None
                )
                if all(p["_availability"] is not None for p in starters)
                else None
            )
            if all(p["_strength"] is not None for p in starters):
                metrics["starting_xi_strength"] = sum(
                    p["_strength"] for p in starters
                ) / len(starters)
            if bench and all(p["_strength"] is not None for p in bench):
                metrics["bench_strength"] = sum(p["_strength"] for p in bench) / len(
                    bench
                )

    if history_complete_since is not None:
        cutoff_7d = prediction_at - timedelta(days=7)
        cutoff_14d = prediction_at - timedelta(days=14)
        window_7 = [m for m in history if cutoff_7d <= m["kickoff"] < prediction_at]
        window_14 = [m for m in history if cutoff_14d <= m["kickoff"] < prediction_at]
        metrics["matches_last_7d"] = len(window_7)
        metrics["matches_last_14d"] = len(window_14)
        metrics["cumulative_international_minutes_7d"] = sum(
            p["minutes"] for m in window_7 for p in m["players"]
        )
        metrics["cumulative_international_minutes_14d"] = sum(
            p["minutes"] for m in window_14 for p in m["players"]
        )
        if history:
            previous = history[-1]
            metrics["days_since_last_international_match"] = (
                prediction_at - previous["kickoff"]
            ).total_seconds() / 86400
            metrics["previous_match_starting_xi_minutes"] = sum(
                p["minutes"] for p in previous["players"] if p["started"]
            )
            lineup_ids = (
                lineup["player_ids"] if lineup["status"] == "confirmed" else set()
            )
            if lineup_ids:
                returning = len(previous["starters"] & lineup_ids)
                metrics["returning_starter_count"] = returning
                metrics["missing_usual_starter_count"] = 11 - returning
                metrics["starter_turnover_count"] = 11 - returning
                metrics["starter_turnover_rate"] = (11 - returning) / 11
    result.update(metrics)
    return result


def extract_match_features(
    snapshot: Mapping[str, Any],
    *,
    fixture_id: str,
    home_team: str,
    away_team: str,
    kickoff_at: str,
    prediction_at: str,
    allow_test_fixture: bool = False,
) -> dict[str, Any]:
    """Validate and derive a research-only match feature row.

    Missing/incomplete evidence yields null features; it is never converted to
    full availability. Test fixtures require an explicit opt-in and remain
    marked TEST_FIXTURE, which the evaluation path rejects.
    """
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("schema_version") != SNAPSHOT_SCHEMA
    ):
        raise ResearchContractError("unsupported or missing squad snapshot schema")
    evidence_kind = snapshot.get("evidence_kind")
    if evidence_kind not in {REAL_EVIDENCE, TEST_EVIDENCE}:
        raise ResearchContractError(
            "evidence_kind must be REAL_OBSERVED or TEST_FIXTURE"
        )
    if evidence_kind == TEST_EVIDENCE and not allow_test_fixture:
        raise ResearchContractError(
            "TEST_FIXTURE cannot enter a real research feature row"
        )
    if snapshot.get("competition") != "UEFA Nations League":
        raise ResearchContractError("snapshot competition is not UEFA Nations League")
    prediction = _utc(prediction_at, "prediction_at")
    kickoff = _utc(kickoff_at, "kickoff_at")
    if prediction >= kickoff:
        raise ResearchContractError("prediction time must precede kickoff")
    fixture = snapshot.get("fixture")
    if not isinstance(fixture, dict):
        raise ResearchContractError("fixture binding is required")
    if fixture.get("fixture_id") != fixture_id:
        raise ResearchContractError("fixture identity mismatch")
    if _canonical_team(
        fixture.get("home_team"), "fixture.home_team"
    ) != _canonical_team(home_team, "home_team"):
        raise ResearchContractError("home participant mismatch")
    if _canonical_team(
        fixture.get("away_team"), "fixture.away_team"
    ) != _canonical_team(away_team, "away_team"):
        raise ResearchContractError("away participant mismatch")
    if _utc(fixture.get("kickoff_at"), "fixture.kickoff_at") != kickoff:
        raise ResearchContractError("fixture kickoff mismatch")
    sources = _validate_source_catalog(snapshot, prediction)
    teams = snapshot.get("teams")
    if not isinstance(teams, dict) or set(teams) != {"home", "away"}:
        raise ResearchContractError("snapshot must bind both home and away teams")
    home = _validate_team(teams["home"], sources, prediction)
    away = _validate_team(teams["away"], sources, prediction)
    home_lineup = _validate_lineup(
        teams["home"].get("lineup"), home, sources, prediction
    )
    away_lineup = _validate_lineup(
        teams["away"].get("lineup"), away, sources, prediction
    )
    home_history, home_history_since = _validate_match_history(
        teams["home"], sources, prediction
    )
    away_history, away_history_since = _validate_match_history(
        teams["away"], sources, prediction
    )

    home_features = _side_features(
        home, home_lineup, home_history, home_history_since, prediction
    )
    away_features = _side_features(
        away, away_lineup, away_history, away_history_since, prediction
    )
    output_features: dict[str, Any] = {}
    for name in (
        "squad_availability",
        "unavailable_player_count",
        "unavailable_starter_count",
        "goalkeeper_absence",
        "defender_absence",
        "midfielder_absence",
        "forward_absence",
        "weighted_impact_lost",
        "key_player_risk",
        "squad_market_value",
        "starting_xi_strength",
        "bench_strength",
        "returning_starter_count",
        "missing_usual_starter_count",
        "starter_turnover_count",
        "starter_turnover_rate",
        "previous_match_starting_xi_minutes",
        "cumulative_international_minutes_7d",
        "cumulative_international_minutes_14d",
        "days_since_last_international_match",
        "matches_last_7d",
        "matches_last_14d",
    ):
        output_features[f"{name}_home"] = home_features[name]
        output_features[f"{name}_away"] = away_features[name]
    output_features["squad_availability_diff"] = (
        home_features["squad_availability"] - away_features["squad_availability"]
        if home_features["squad_availability"] is not None
        and away_features["squad_availability"] is not None
        else None
    )
    output_features["weighted_impact_lost_diff"] = None
    output_features["starting_xi_strength_diff"] = (
        home_features["starting_xi_strength"] - away_features["starting_xi_strength"]
        if home_features["starting_xi_strength"] is not None
        and away_features["starting_xi_strength"] is not None
        else None
    )
    output_features["bench_strength_diff"] = (
        home_features["bench_strength"] - away_features["bench_strength"]
        if home_features["bench_strength"] is not None
        and away_features["bench_strength"] is not None
        else None
    )
    output_features["squad_market_value_ratio"] = None
    output_features["squad_market_value_log_ratio"] = None
    weighted_impact_method = None
    if (
        home_features["data_quality"] in {"HIGH", "MEDIUM"}
        and away_features["data_quality"] in {"HIGH", "MEDIUM"}
        and all(p["_position"] != "unknown" for p in home["players"])
        and all(p["_position"] != "unknown" for p in away["players"])
    ):
        # Keep the canonical SportsBrain impact calculation, but only after the
        # research layer has proved complete, timestamp-valid matchday rosters.
        import pandas as pd

        from src.data.squad_merger import squad_impact_features
        from src.data.squad_models import PlayerStatus, SquadReport

        def report(team_name: str, team_data: Mapping[str, Any]) -> SquadReport:
            market_values_complete = all(
                p["_market_value"] is not None for p in team_data["players"]
            )
            players = [
                PlayerStatus(
                    name=p["name"],
                    position=p["_position"],
                    availability=p["_availability"],
                    status=p["_status"] or "unknown",
                    key_player=bool(p["_key_player"]),
                    market_value_eur_m=p["_market_value"]
                    if market_values_complete
                    else 0.0,
                )
                for p in team_data["players"]
            ]
            return SquadReport(
                team=team_name,
                report_date=pd.Timestamp(prediction),
                players=players,
                data_source="validated_point_in_time_research",
            )

        impact = squad_impact_features(
            report(_canonical_team(home_team, "home_team"), home),
            report(_canonical_team(away_team, "away_team"), away),
        )
        output_features["weighted_impact_lost_home"] = impact[
            "weighted_impact_lost_home"
        ]
        output_features["weighted_impact_lost_away"] = impact[
            "weighted_impact_lost_away"
        ]
        output_features["weighted_impact_lost_diff"] = impact[
            "weighted_impact_lost_diff"
        ]
        # Existing implementation assumes every player is key=false when not
        # flagged. Preserve missingness rather than using that implicit default.
        output_features["key_player_risk_home"] = home_features["key_player_risk"]
        output_features["key_player_risk_away"] = away_features["key_player_risk"]
        home_value = output_features.get("squad_market_value_home")
        away_value = output_features.get("squad_market_value_away")
        output_features["squad_market_value_ratio"] = (
            home_value / away_value
            if home_value is not None and away_value not in (None, 0.0)
            else None
        )
        output_features["squad_market_value_log_ratio"] = (
            math.log(home_value / away_value)
            if home_value is not None
            and away_value is not None
            and home_value > 0
            and away_value > 0
            else None
        )
        weighted_impact_method = (
            "market_value_position_weighted"
            if all(
                p["_market_value"] is not None
                for p in home["players"] + away["players"]
            )
            and sum(p["_market_value"] for p in home["players"] + away["players"]) > 0
            else "position_weighted"
        )
        for side, team_data in (("home", home), ("away", away)):
            if team_data["roster_complete"] and all(
                p["_market_value"] is not None for p in team_data["players"]
            ):
                output_features[f"squad_market_value_{side}"] = sum(
                    p["_market_value"] for p in team_data["players"]
                )

    home_value = output_features.get("squad_market_value_home")
    away_value = output_features.get("squad_market_value_away")
    if home_value is not None and away_value not in (None, 0.0):
        output_features["squad_market_value_ratio"] = home_value / away_value
        if home_value > 0:
            output_features["squad_market_value_log_ratio"] = math.log(
                home_value / away_value
            )

    home_quality = home_features["data_quality"]
    away_quality = away_features["data_quality"]
    quality_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "UNAVAILABLE": 3}
    match_quality = max((home_quality, away_quality), key=quality_order.get)
    lineup_status = (
        "confirmed"
        if home_lineup["status"] == away_lineup["status"] == "confirmed"
        else (
            "probable"
            if "probable" in {home_lineup["status"], away_lineup["status"]}
            else "unavailable"
        )
    )
    snapshot_digest = canonical_json_digest(snapshot)
    feature_row = {
        "schema_version": FEATURE_SCHEMA,
        "evidence_kind": evidence_kind,
        "fixture_id": fixture_id,
        "competition": "UEFA Nations League",
        "home_team": _canonical_team(home_team, "home_team"),
        "away_team": _canonical_team(away_team, "away_team"),
        "kickoff_at": kickoff.isoformat(),
        "prediction_at": prediction.isoformat(),
        "snapshot_observed_at": _utc(
            snapshot.get("observed_at"), "observed_at"
        ).isoformat(),
        "snapshot_digest": snapshot_digest,
        "source_count": len(sources),
        "source_timestamps": sorted(s["_as_of"].isoformat() for s in sources.values()),
        "data_quality": match_quality,
        "home_data_quality": home_quality,
        "away_data_quality": away_quality,
        "lineup_status": lineup_status,
        "home_conflicting_player_status_count": home_features[
            "conflicting_player_status_count"
        ],
        "away_conflicting_player_status_count": away_features[
            "conflicting_player_status_count"
        ],
        "fallback_usage": False,
        "feature_metadata": {"weighted_impact_method": weighted_impact_method},
        "features": output_features,
    }
    feature_row["feature_digest"] = canonical_json_digest(feature_row)
    return feature_row


def _probability_metrics(
    rows: Sequence[Mapping[str, Any]], *, target_count: int | None = None
) -> dict[str, Any]:
    if not rows:
        return {
            "status": "not_evaluated",
            "matches_evaluated": 0,
            "coverage": 0.0,
            "metrics": None,
        }
    outcomes = [row["outcome"] for row in rows]
    if any(
        isinstance(outcome, bool)
        or not isinstance(outcome, int)
        or outcome not in (0, 1, 2)
        for outcome in outcomes
    ):
        raise ResearchContractError("outcome must be integer Home/Draw/Away index 0..2")
    probs = [[float(x) for x in row["probabilities"]] for row in rows]
    if any(outcome not in (0, 1, 2) for outcome in outcomes):
        raise ResearchContractError("outcome must be home/draw/away index 0..2")
    for vector in probs:
        if len(vector) != 3 or any(
            not math.isfinite(p) or p < 0 or p > 1 for p in vector
        ):
            raise ResearchContractError(
                "probability vector must contain three valid values"
            )
        if not math.isclose(sum(vector), 1.0, abs_tol=1e-6):
            raise ResearchContractError("probability vector must sum to one")
    n = len(rows)
    one_hot = [[1.0 if outcome == i else 0.0 for i in range(3)] for outcome in outcomes]
    brier = (
        sum(sum((p[i] - y[i]) ** 2 for i in range(3)) for p, y in zip(probs, one_hot))
        / n
    )
    logloss = -sum(math.log(max(probs[j][outcomes[j]], 1e-15)) for j in range(n)) / n
    classes: dict[str, Any] = {}
    eces = []
    for index, name in enumerate(("home", "draw", "away")):
        class_probs = [p[index] for p in probs]
        class_truth = [y[index] for y in one_hot]
        bins = []
        class_ece = 0.0
        for bin_index in range(10):
            low, high = bin_index / 10, (bin_index + 1) / 10
            indices = [
                i
                for i, p in enumerate(class_probs)
                if low <= p < high or (bin_index == 9 and p == 1.0)
            ]
            if indices:
                mean_p = sum(class_probs[i] for i in indices) / len(indices)
                observed = sum(class_truth[i] for i in indices) / len(indices)
                err = abs(mean_p - observed)
                class_ece += len(indices) / n * err
                bins.append(
                    {
                        "lower": low,
                        "upper": high,
                        "count": len(indices),
                        "mean_probability": mean_p,
                        "observed_frequency": observed,
                    }
                )
        eces.append(class_ece)
        classes[name] = {
            "brier_score_one_vs_rest": sum(
                (p - y) ** 2 for p, y in zip(class_probs, class_truth)
            )
            / n,
            "mean_probability": sum(class_probs) / n,
            "observed_frequency": sum(class_truth) / n,
            "ece_10_bins": class_ece,
            "reliability_bins": bins,
        }
    denominator = target_count if target_count is not None else n
    return {
        "status": "evaluated",
        "matches_evaluated": n,
        "coverage": n / denominator if denominator else 0.0,
        "metrics": {
            "brier_score_multiclass": brier,
            "log_loss_multiclass": logloss,
            "expected_calibration_error_10_bins_mean_one_vs_rest": sum(eces) / 3,
            "home_draw_away_calibration": classes,
            "accuracy_argmax": sum(
                max(range(3), key=lambda i: p[i]) == outcomes[j]
                for j, p in enumerate(probs)
            )
            / n,
            "mean_max_probability_sharpness": sum(max(p) for p in probs) / n,
        },
    }


def _cluster_bootstrap_differences(
    rows: Sequence[Mapping[str, Any]], variant: str, *, replicates: int, seed: int
) -> dict[str, Any]:
    clusters: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        cluster = row["kickoff_at"][:10]
        clusters.setdefault(cluster, []).append(row)
    if len(clusters) < 2:
        return {
            "status": "not_evaluated",
            "reason": "at least two date clusters are required",
        }
    names = sorted(clusters)
    rng = random.Random(seed)
    differences: dict[str, list[float]] = {
        "brier_score_multiclass": [],
        "log_loss_multiclass": [],
    }
    for _ in range(replicates):
        sampled_names = [names[rng.randrange(len(names))] for _ in names]
        sample = [row for name in sampled_names for row in clusters[name]]
        base_rows = [
            {"outcome": row["outcome"], "probabilities": row["predictions"]["baseline"]}
            for row in sample
        ]
        variant_rows = [
            {"outcome": row["outcome"], "probabilities": row["predictions"][variant]}
            for row in sample
        ]
        base = _probability_metrics(base_rows)["metrics"]
        cand = _probability_metrics(variant_rows)["metrics"]
        differences["brier_score_multiclass"].append(
            cand["brier_score_multiclass"] - base["brier_score_multiclass"]
        )
        differences["log_loss_multiclass"].append(
            cand["log_loss_multiclass"] - base["log_loss_multiclass"]
        )
    return {
        "status": "evaluated",
        "cluster_count": len(clusters),
        "replicates": replicates,
        "seed": seed,
        "candidate_minus_baseline": {
            metric: {
                "mean": sum(values) / len(values),
                "lower_95": sorted(values)[int(0.025 * (len(values) - 1))],
                "upper_95": sorted(values)[int(0.975 * (len(values) - 1))],
            }
            for metric, values in differences.items()
        },
    }


def evaluate_causal_ablation(
    rows: Iterable[Mapping[str, Any]],
    *,
    target_match_count: int,
    variants: Sequence[str] = ABLATION_VARIANTS,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = 20260929,
) -> dict[str, Any]:
    """Evaluate paired model rows only when each has real PIT feature evidence."""
    materialized = list(rows)
    if target_match_count < 0 or bootstrap_replicates <= 0:
        raise ResearchContractError("evaluation bounds are invalid")
    if not materialized:
        return {
            "status": "not_evaluated",
            "reason": "HISTORICAL_POINT_IN_TIME_UNAVAILABLE",
            "eligible_match_count": target_match_count,
            "paired_match_count": 0,
            "variants": {
                name: {
                    "status": "not_evaluated",
                    "matches_evaluated": 0,
                    "coverage": 0.0,
                    "metrics": None,
                }
                for name in variants
            },
            "paired_date_cluster_bootstrap": None,
        }
    seen: set[str] = set()
    if "baseline" not in variants or len(set(variants)) != len(variants):
        raise ResearchContractError("variants must be unique and include baseline")
    expected_variants = set(variants)
    for row in materialized:
        if row.get("evidence_kind") != REAL_EVIDENCE:
            raise ResearchContractError(
                "non-real evidence cannot enter causal ablation evaluation"
            )
        feature_row = row.get("feature_row")
        if (
            not isinstance(feature_row, dict)
            or feature_row.get("schema_version") != FEATURE_SCHEMA
        ):
            raise ResearchContractError(
                "evaluation requires the canonical research feature row"
            )
        feature_digest = feature_row.get("feature_digest")
        unsigned_feature_row = {
            key: value for key, value in feature_row.items() if key != "feature_digest"
        }
        if feature_digest != canonical_json_digest(unsigned_feature_row):
            raise ResearchContractError("feature row digest does not validate")
        if feature_row.get("evidence_kind") != REAL_EVIDENCE or feature_row.get(
            "data_quality"
        ) not in {"HIGH", "MEDIUM"}:
            raise ResearchContractError(
                "feature row is not eligible real point-in-time evidence"
            )
        fixture_id = row.get("fixture_id")
        if not isinstance(fixture_id, str) or fixture_id in seen:
            raise ResearchContractError("evaluation fixture IDs must be unique")
        seen.add(fixture_id)
        kickoff = _utc(row.get("kickoff_at"), "kickoff_at")
        prediction = _utc(row.get("prediction_at"), "prediction_at")
        observed = _utc(row.get("snapshot_observed_at"), "snapshot_observed_at")
        if not observed <= prediction < kickoff:
            raise ResearchContractError("evaluation evidence is not point-in-time safe")
        if row.get("data_quality") not in {"HIGH", "MEDIUM"}:
            raise ResearchContractError(
                "causal evaluation requires usable HIGH or MEDIUM data quality"
            )
        digest = row.get("snapshot_digest")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
        ):
            raise ResearchContractError("evaluation snapshot digest is invalid")
        if (
            feature_row.get("fixture_id") != fixture_id
            or feature_row.get("snapshot_digest") != digest
            or feature_row.get("kickoff_at")
            != _utc(row.get("kickoff_at"), "kickoff_at").isoformat()
            or feature_row.get("prediction_at")
            != _utc(row.get("prediction_at"), "prediction_at").isoformat()
            or feature_row.get("data_quality") != row.get("data_quality")
        ):
            raise ResearchContractError(
                "evaluation row does not bind to its validated feature row"
            )
        prediction_map = row.get("predictions")
        if (
            not isinstance(prediction_map, dict)
            or set(prediction_map) != expected_variants
        ):
            raise ResearchContractError(
                "every paired fixture needs predictions for every variant"
            )
        outcome = row.get("outcome")
        if (
            isinstance(outcome, bool)
            or not isinstance(outcome, int)
            or outcome not in (0, 1, 2)
        ):
            raise ResearchContractError("evaluation outcome is invalid")
    if len(materialized) > target_match_count:
        raise ResearchContractError(
            "paired fixture count exceeds eligible target count"
        )
    result_variants: dict[str, Any] = {}
    for name in variants:
        selected = [
            {"outcome": row["outcome"], "probabilities": row["predictions"][name]}
            for row in materialized
        ]
        result_variants[name] = _probability_metrics(
            selected, target_count=target_match_count
        )
    paired = {
        name: _cluster_bootstrap_differences(
            materialized, name, replicates=bootstrap_replicates, seed=seed
        )
        for name in variants
        if name != "baseline"
    }
    paired_fixture_differences = {}
    baseline_metrics = result_variants["baseline"]["metrics"]
    for name in variants:
        if name == "baseline":
            continue
        candidate_metrics = result_variants[name]["metrics"]
        paired_fixture_differences[name] = {
            "candidate_minus_baseline_brier_mean": (
                candidate_metrics["brier_score_multiclass"]
                - baseline_metrics["brier_score_multiclass"]
            ),
            "candidate_minus_baseline_log_loss_mean": (
                candidate_metrics["log_loss_multiclass"]
                - baseline_metrics["log_loss_multiclass"]
            ),
            "paired_fixture_count": len(materialized),
        }
    return {
        "status": "evaluated",
        "eligible_match_count": target_match_count,
        "paired_match_count": len(materialized),
        "variants": result_variants,
        "paired_fixture_differences": paired_fixture_differences,
        "paired_date_cluster_bootstrap": paired,
    }


def append_forward_capture(
    path: str | Path, snapshot: Mapping[str, Any], *, capture_slot: str
) -> str:
    """Append one validated real capture to a private JSONL research store.

    No provider access occurs here. The caller must supply a source-captured
    REAL_OBSERVED snapshot. Existing content is never rewritten or replaced.
    """
    if capture_slot not in FORWARD_CAPTURE_SLOTS:
        raise ResearchContractError("unsupported forward-capture slot")
    if snapshot.get("evidence_kind") != REAL_EVIDENCE:
        raise ResearchContractError(
            "forward capture accepts REAL_OBSERVED evidence only"
        )
    fixture = snapshot.get("fixture")
    if not isinstance(fixture, dict):
        raise ResearchContractError("forward capture needs fixture identity")
    identity = str(fixture.get("fixture_id", ""))
    if not identity:
        raise ResearchContractError("forward capture fixture_id is required")
    prediction_at = snapshot.get("observed_at")
    feature_row = extract_match_features(
        snapshot,
        fixture_id=identity,
        home_team=fixture.get("home_team"),
        away_team=fixture.get("away_team"),
        kickoff_at=fixture.get("kickoff_at"),
        prediction_at=prediction_at,
    )
    kickoff = _utc(fixture.get("kickoff_at"), "fixture.kickoff_at")
    captured = _utc(prediction_at, "observed_at")
    lead_hours = (kickoff - captured).total_seconds() / 3600
    if capture_slot in FORWARD_CAPTURE_WINDOWS_HOURS:
        lower, upper = FORWARD_CAPTURE_WINDOWS_HOURS[capture_slot]
        if not lower <= lead_hours <= upper:
            raise ResearchContractError(
                "capture timestamp does not match its declared forward-capture slot"
            )
    elif feature_row["lineup_status"] != "confirmed":
        raise ResearchContractError(
            "CONFIRMED_LINEUP slot requires two fresh confirmed lineups"
        )
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ResearchContractError("capture store may not be a symlink")
    if not path.parent.exists() or not path.parent.is_dir():
        raise ResearchContractError("capture store parent directory must already exist")
    if path.parent.is_symlink():
        raise ResearchContractError(
            "capture store parent directory may not be a symlink"
        )
    parent_stat = path.parent.stat()
    if parent_stat.st_mode & 0o077:
        raise ResearchContractError("capture store parent directory must be private")
    existed = path.exists()
    if existed and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ResearchContractError(
            "existing capture store permissions must already be 0600"
        )
    record = {
        "store_schema": CAPTURE_STORE_SCHEMA,
        "capture_slot": capture_slot,
        "fixture_id": identity,
        "snapshot_digest": canonical_json_digest(snapshot),
        "snapshot": dict(snapshot),
    }
    line = (
        json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | nofollow, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ResearchContractError("capture store must be a regular file")
        if not existed:
            os.fchmod(fd, 0o600)
        elif stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise ResearchContractError(
                "existing capture store permissions must already be 0600"
            )
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = b""
        read_fd = os.open(path, os.O_RDONLY)
        try:
            os.lseek(read_fd, 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            existing = b"".join(chunks)
        finally:
            os.close(read_fd)
        key = (identity, capture_slot)
        for raw in existing.splitlines():
            prior = json.loads(raw)
            if prior.get("store_schema") != CAPTURE_STORE_SCHEMA:
                raise ResearchContractError(
                    "capture store contains an unsupported record"
                )
            prior_snapshot = prior.get("snapshot")
            if (
                not isinstance(prior_snapshot, dict)
                or prior.get("snapshot_digest") != canonical_json_digest(prior_snapshot)
                or prior.get("fixture_id")
                != prior_snapshot.get("fixture", {}).get("fixture_id")
                or prior.get("capture_slot") not in FORWARD_CAPTURE_SLOTS
            ):
                raise ResearchContractError(
                    "capture store contains an invalid or modified prior record"
                )
            if (prior.get("fixture_id"), prior.get("capture_slot")) == key:
                raise ResearchContractError(
                    "fixture/capture slot already exists; append-only store cannot replace it"
                )
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise ResearchContractError("capture store permissions must be 0600")
        offset = 0
        while offset < len(line):
            written = os.write(fd, line[offset:])
            if written <= 0:
                raise OSError("append-only capture write made no progress")
            offset += written
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return record["snapshot_digest"]


def build_local_research_report(
    repository_root: str | Path,
    *,
    source_main_sha: str,
    generated_at: str,
) -> dict[str, Any]:
    """Build a reproducible coverage/ablation report from local audit files only."""
    root = Path(repository_root)
    audit_path = root / "results/audits/nations_league_model_validation_20260927.json"
    squads_path = root / "docs/data/squads.json"
    suspensions_path = root / "data/suspensions.json"
    if not all(p.is_file() for p in (audit_path, squads_path, suspensions_path)):
        raise ResearchContractError("required local read-only audit inputs are missing")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    current_squads = json.loads(squads_path.read_text(encoding="utf-8"))
    current_suspensions = json.loads(suspensions_path.read_text(encoding="utf-8"))
    if audit.get("tournament") != "UEFA Nations League":
        raise ResearchContractError("historical audit input is not the Nations League")
    match_count = int(audit["strict_validation"]["match_count"])
    period_total = sum(int(row["match_count"]) for row in audit["historical_periods"])
    if match_count != period_total:
        raise ResearchContractError("historical match total does not reconcile")
    current_team_entries = current_squads.get("teams", {})
    current_team_sources = sorted(
        {
            str(data.get("source", "unknown"))
            for data in current_team_entries.values()
            if isinstance(data, dict)
        }
    )
    suspension_team_count = sum(
        1 for key in current_suspensions if not str(key).startswith("_")
    )
    candidate_roots = [root / "data", root / "results", root / "docs/data"]
    squad_cache_root = root / "data/cache/squad"
    candidate_names: list[str] = []
    snapshot_record_count = 0
    markers = ("squad", "lineup", "injur", "availab", "starting_xi", "suspension")
    for candidate_root in candidate_roots:
        if not candidate_root.exists():
            continue
        for candidate in candidate_root.rglob("*"):
            in_squad_cache = candidate.is_relative_to(squad_cache_root)
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or candidate.name == "nations_league_squad_lineup_research_v1.json"
            ):
                continue
            if candidate.suffix.lower() not in {".json", ".jsonl"}:
                continue
            if not in_squad_cache and not any(
                marker in candidate.name.lower() for marker in markers
            ):
                continue
            candidate_names.append(str(candidate.relative_to(root)))
            try:
                if candidate.suffix.lower() == ".jsonl":
                    records = [
                        json.loads(line)
                        for line in candidate.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                else:
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                    records = payload if isinstance(payload, list) else [payload]
                snapshot_record_count += sum(
                    1
                    for record in records
                    if isinstance(record, dict)
                    and record.get("schema_version") == SNAPSHOT_SCHEMA
                    and record.get("evidence_kind") == REAL_EVIDENCE
                )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    frozen = audit["retrospective_frozen_model_diagnostic"][
        "frozen_dixon_coles_component"
    ]
    frozen_metrics = frozen.get("metrics", {})
    variants = {
        name: {
            "status": "HISTORICAL_POINT_IN_TIME_UNAVAILABLE",
            "matches_evaluated": 0,
            "coverage": 0.0,
            "metrics": None,
            "reason": "No fixture-bound historical REAL_OBSERVED squad snapshot with source and capture timestamps was located.",
        }
        for name in ABLATION_VARIANTS
    }
    report = {
        "schema_version": REPORT_SCHEMA,
        "research_feature_schema": FEATURE_SCHEMA,
        "research_status": "NL_SQUAD_FEATURES_FORWARD_EVIDENCE_REQUIRED",
        "generated_at": generated_at,
        "source_main_sha": source_main_sha,
        "tournament": "UEFA Nations League",
        "historical_sample": {
            "match_count": match_count,
            "periods": audit["historical_periods"],
            "verified_historical_pit_squad_captures": 0,
            "schema_matching_real_snapshot_records_in_audited_paths": snapshot_record_count,
            "fixture_matched_pit_coverage": 0.0,
            "status": "HISTORICAL_POINT_IN_TIME_UNAVAILABLE",
        },
        "baseline_context_only": {
            "source_artifact": "results/audits/nations_league_model_validation_20260927.json",
            "label": frozen.get("label", "frozen_dixon_coles_component"),
            "matches_evaluated": frozen.get("matches_evaluated", 0),
            "coverage": frozen.get("coverage", 0.0),
            "metrics": {
                key: frozen_metrics.get(key)
                for key in (
                    "brier_score_multiclass",
                    "expected_calibration_error_10_bins_mean_one_vs_rest",
                    "accuracy_argmax",
                    "mean_max_probability_sharpness",
                    "home_draw_away_calibration",
                )
            },
            "log_loss_multiclass": None,
            "causal_squad_comparison": False,
            "limitations": [
                "Context only: the source audit says historical model reconstruction uses current static squad-value inputs and default squad context where historical snapshots are absent.",
                "These metrics are not a matched baseline for any squad ablation and must not be read as squad-feature evidence.",
            ],
        },
        "data_quality_and_coverage": {
            "historical_fixture_count": match_count,
            "historical_fixture_count_with_verified_pit_squad_data": 0,
            "historical_fixture_count_unavailable_for_squad_features": match_count,
            "data_quality_strata": {
                "HIGH": 0,
                "MEDIUM": 0,
                "LOW": 0,
                "UNAVAILABLE": match_count,
            },
            "current_nonhistorical_snapshot": {
                "path": "docs/data/squads.json",
                "updated": current_squads.get("updated"),
                "team_count": len(current_team_entries),
                "sources": current_team_sources,
                "usable_for_historical_nations_league": False,
                "reason": "Current/future national squads without fixture binding or archived point-in-time capture.",
            },
            "current_nonhistorical_suspension_overlay": {
                "path": "data/suspensions.json",
                "last_updated": current_suspensions.get("_injuries_last_updated"),
                "team_count": suspension_team_count,
                "usable_for_historical_nations_league": False,
                "reason": "One current overlay without per-player, per-fixture observation timestamps.",
            },
            "forward_capture_count": 0,
            "forward_capture_started": False,
            "inspected_snapshot_candidate_files": sorted(candidate_names),
            "inspected_snapshot_roots": [
                "data/cache/squad",
                "data",
                "results",
                "docs/data",
            ],
        },
        "feature_ablations": variants,
        "subgroup_analyses": {
            name: {"status": "not_evaluated", "matches_evaluated": 0, "metrics": None}
            for name in (
                "zero_important_absences",
                "one_or_more_important_absences",
                "large_strength_difference",
                "favorites",
                "balanced",
                "underdogs",
                "tier_A_B_C_D",
                "confirmed_lineup",
                "probable_lineup",
                "quality_HIGH",
                "quality_MEDIUM",
                "quality_LOW",
            )
        },
        "evaluation_contract": {
            "metrics": [
                "multiclass_brier",
                "multiclass_log_loss",
                "ece",
                "home_draw_away_calibration",
                "coverage",
                "sharpness",
            ],
            "paired_fixture_comparison": True,
            "paired_date_cluster_bootstrap": {
                "replicates": BOOTSTRAP_REPLICATES,
                "seed": 20260929,
            },
            "fair_comparison_rule": "Every variant must use the same fixture IDs, outcomes, prediction cutoffs, and PIT evidence rows; otherwise the ablation fails closed.",
            "result": "NOT_RUN_NO_VERIFIED_PIT_SQUAD_FEATURES",
        },
        "source_audit": {
            "existing_squad_impact_features": [
                "squad_availability_home",
                "squad_availability_away",
                "squad_availability_diff",
                "key_player_risk_home",
                "key_player_risk_away",
                "weighted_impact_lost_home",
                "weighted_impact_lost_away",
                "weighted_impact_lost_diff",
            ],
            "limitations": [
                "Empty SquadReport availability_score is 1.0 and default_report is empty; in this research contract, incomplete/missing data emits nulls and UNAVAILABLE.",
                "Covers supplies current unavailable/doubtful players only; its current injury source is not match-date queried and cannot establish full-roster availability.",
                "Transfermarkt scrapes current/previous-season pages with a filesystem-mtime 24-hour cache, not archived as-of snapshots.",
                "Wikipedia squad source is current/future 2026 World Cup roster data, not historical Nations League matchday squads.",
                "SofaScore values are current/live and require an API key; no provider call or credential read was made.",
                "Manual suspension overlay is current and not event/player timestamp bound.",
                "FotMob lineups/ratings are read from finished-match pages and are post-match for the target fixture; excluded as prediction inputs.",
                "Static national-team market values are June 2026 approximations and are not point-in-time historical Nations League values.",
                "StatsBomb player xG coverage is limited to other tournament competitions and is not a historical Nations League lineup dataset.",
            ],
        },
        "safety": {
            "provider_requests": 0,
            "credential_accesses": 0,
            "quota_consumed": 0,
            "production_code_changed": False,
            "scanner_or_signal_detector_changed": False,
            "activation_publication_betting_ledger": False,
        },
    }
    report["report_digest"] = canonical_json_digest(report)
    return report


def write_research_report(report: Mapping[str, Any], output_path: str | Path) -> str:
    """Write the versioned JSON research artifact, refusing symlink outputs."""
    path = Path(output_path)
    if path.is_symlink():
        raise ResearchContractError("research report output may not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(
            report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        + "\n"
    )
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

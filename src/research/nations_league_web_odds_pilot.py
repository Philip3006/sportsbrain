"""Deterministic, offline normalization for the Nations League web-odds pilot.

This module deliberately does not fetch URLs.  Human/agent research records are
entered as observations and are accepted only when the evidence timing is
explicit enough for the requested phase.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

PHASES = ("INITIAL", "REFINEMENT", "CLOSING_BENCHMARK")
EVIDENCE_STATUSES = (
    "VERIFIED_EXACT",
    "VERIFIED_NEAR_TARGET",
    "OPENING_ONLY",
    "CLOSING_ONLY",
    "UNTIMESTAMPED_HISTORICAL",
    "UNAVAILABLE",
    "IDENTITY_AMBIGUOUS",
)
TARGET_OFFSETS = {
    "INITIAL": timedelta(hours=24),
    "REFINEMENT": timedelta(minutes=90),
    "CLOSING_BENCHMARK": timedelta(0),
}
TOLERANCE = {
    "INITIAL": timedelta(hours=2),
    "REFINEMENT": timedelta(minutes=30),
}


def parse_utc(value: str) -> datetime:
    """Parse the canonical timeline's UTC representation."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be explicitly UTC")
    return parsed.astimezone(timezone.utc)


def target_timestamp(kickoff_utc: str, phase: str) -> str:
    if phase not in PHASES:
        raise ValueError(f"unknown phase: {phase}")
    return (parse_utc(kickoff_utc) - TARGET_OFFSETS[phase]).isoformat().replace("+00:00", "Z")


def normalize_odds(odds_decimal: list[float] | tuple[float, float, float]) -> dict[str, dict[str, float]]:
    """Return raw implied and normalized no-vig probabilities for 1X2 odds."""
    if len(odds_decimal) != 3:
        raise ValueError("1X2 odds must contain home, draw, away")
    if any(not isinstance(value, (int, float)) or not isfinite(value) or value <= 1.0 for value in odds_decimal):
        raise ValueError("decimal odds must be finite and greater than 1")
    implied = [1.0 / float(value) for value in odds_decimal]
    total = sum(implied)
    normalized = [value / total for value in implied]
    labels = ("home", "draw", "away")
    return {
        "raw_implied": dict(zip(labels, implied)),
        "normalized_no_vig": dict(zip(labels, normalized)),
    }


def classify_timing(phase: str, target_utc: str, observed_at: str | None) -> tuple[str, int | None]:
    """Classify evidence without turning an untimestamped page into a timestamp."""
    if not observed_at:
        return "UNTIMESTAMPED_HISTORICAL", None
    observed = parse_utc(observed_at)
    target = parse_utc(target_utc)
    offset_minutes = round((observed - target).total_seconds() / 60)
    if phase == "CLOSING_BENCHMARK":
        return ("VERIFIED_EXACT" if offset_minutes <= 0 else "UNAVAILABLE"), offset_minutes
    tolerance_minutes = round(TOLERANCE[phase].total_seconds() / 60)
    if abs(offset_minutes) <= tolerance_minutes:
        return "VERIFIED_EXACT" if offset_minutes == 0 else "VERIFIED_NEAR_TARGET", offset_minutes
    if observed <= target:
        return "OPENING_ONLY", offset_minutes
    return "UNAVAILABLE", offset_minutes


def phase_record(fixture: dict[str, Any], phase: str, observation: dict[str, Any] | None = None) -> dict[str, Any]:
    target = target_timestamp(fixture["kickoff_utc"], phase)
    has_observation = observation is not None
    observation = observation or {}
    status, offset = (
        classify_timing(phase, target, observation.get("observed_at"))
        if has_observation
        else ("UNAVAILABLE", None)
    )
    if observation.get("evidence_status") == "IDENTITY_AMBIGUOUS":
        status = "IDENTITY_AMBIGUOUS"
    record: dict[str, Any] = {
        "fixture_id": fixture["fixture_id"],
        "edition": fixture["edition"],
        "group": fixture.get("group"),
        "tier": fixture.get("tier"),
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
        "kickoff_utc": fixture["kickoff_utc"],
        "phase": phase,
        "target_utc": target,
        "valid_window": {"start_utc": None, "end_utc": None},
        "source_name": observation.get("source_name"),
        "source_url": observation.get("source_url"),
        "observed_at": observation.get("observed_at"),
        "evidence_status": observation.get("evidence_status", status),
        "timing_relation": "not_demonstrated" if status == "UNTIMESTAMPED_HISTORICAL" else status,
        "offset_minutes": offset,
        "odds_decimal": observation.get("odds_decimal"),
        "raw_implied_probabilities": None,
        "normalized_no_vig_probabilities": None,
        "source_quality": observation.get("source_quality", {}),
        "notes": observation.get("notes"),
    }
    if phase in TOLERANCE:
        delta = TOLERANCE[phase]
        target_dt = parse_utc(target)
        record["valid_window"] = {
            "start_utc": (target_dt - delta).isoformat().replace("+00:00", "Z"),
            "end_utc": (target_dt + delta).isoformat().replace("+00:00", "Z"),
    }
    if observation.get("odds_decimal") is not None:
        normalized = normalize_odds(observation["odds_decimal"])
        record["raw_implied_probabilities"] = normalized["raw_implied"]
        record["normalized_no_vig_probabilities"] = normalized["normalized_no_vig"]
    return record


def summarize(records: list[dict[str, Any]], attempted_fixture_count: int) -> dict[str, Any]:
    by_phase: dict[str, dict[str, int]] = {}
    for phase in PHASES:
        by_phase[phase] = {status: 0 for status in EVIDENCE_STATUSES}
        for row in records:
            if row["phase"] == phase:
                by_phase[phase][row["evidence_status"]] += 1
    return {
        "attempted_fixtures": attempted_fixture_count,
        "attempted_phases": len(records),
        "by_phase": by_phase,
        "initial_exact_or_near": sum(by_phase["INITIAL"].get(status, 0) for status in ("VERIFIED_EXACT", "VERIFIED_NEAR_TARGET")),
        "refinement_exact_or_near": sum(by_phase["REFINEMENT"].get(status, 0) for status in ("VERIFIED_EXACT", "VERIFIED_NEAR_TARGET")),
        "closing_only": sum(1 for row in records if row["evidence_status"] == "CLOSING_ONLY"),
        "unavailable_or_unusable": sum(
            1 for row in records if row["evidence_status"] in ("UNAVAILABLE", "IDENTITY_AMBIGUOUS")
        ),
        "decision": "NL_WEB_ODDS_PILOT_NOT_VIABLE",
    }

"""Bounded, no-network integrity checks for Champions League replay inputs.

The existing Top-5 shadow path deliberately does not accept Champions League
records.  This module is a separate evidence boundary for UCL data quality and
offline replay coverage.  It never calls a provider, writes a cache, publishes
an artifact, or enables betting.

Callers supply an explicit ``as_of`` time and freshness policy.  Reports keep
complete deterministic counts but only a bounded sample of issue details so a
bad provider payload cannot grow the observability context without limit.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import isfinite
from types import MappingProxyType
from typing import Any

_OUTCOMES = ("home", "draw", "away")
from src.football.champions_league_contracts import (
    _LEAGUE_ALIASES,
    ChampionsLeagueIntegrityPolicy,
    ChampionsLeagueIntegrityReport,
    IntegrityCode,
    IntegrityIssue,
)


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _row_value(row: Mapping[str, object], *names: str) -> object | None:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _nested_row(row: Mapping[str, object]) -> Mapping[str, object]:
    nested = row.get("prediction_artifact") or row.get("prediction")
    if not isinstance(nested, Mapping):
        return row
    merged = dict(row)
    merged.update(nested)
    return merged


def _parse_datetime(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} is malformed") from exc
    else:
        raise ValueError(f"{field} is missing")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} is timezone-naive")
    return parsed.astimezone(timezone.utc)


def _league(row: Mapping[str, object]) -> str:
    value = _as_text(_row_value(row, "league_code", "league", "competition"))
    return value.casefold().replace("-", "_")


def _fixture_key(row: Mapping[str, object]) -> str:
    return _as_text(_row_value(row, "fixture_key", "fixture_id", "event_id"))


def _team(row: Mapping[str, object], side: str) -> str:
    identifier = _as_text(
        _row_value(row, f"{side}_team_id", f"{side}_id", f"{side}_entity_id")
    )
    if identifier:
        return f"id:{identifier.casefold()}"
    name = _as_text(_row_value(row, f"{side}_team", side, f"{side}_name"))
    if not name:
        return ""
    # This is canonical comparison only; it does not silently map aliases.
    return "name:" + " ".join(name.casefold().split())


def _kickoff(row: Mapping[str, object], field: str = "kickoff") -> datetime:
    value = _row_value(row, field, "commence_time")
    return _parse_datetime(value, field)


def _finite_number(value: object) -> bool:
    try:
        return not isinstance(value, bool) and isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _add_issue(
    issues: list[IntegrityIssue],
    counts: Counter[str],
    limit: int,
    code: IntegrityCode,
    source: str,
    fixture_key: str,
    field: str,
    detail: str,
) -> None:
    key = IntegrityCode(code).value
    counts[key] += 1
    if len(issues) < limit:
        issues.append(IntegrityIssue(code, source, fixture_key, field, detail))


def _validate_identity(
    row: Mapping[str, object],
    source: str,
    expected: tuple[str, str, str, datetime],
    kickoff_tolerance_seconds: int,
    add_issue: Any,
) -> bool:
    expected_key, expected_home, expected_away, expected_kickoff = expected
    key = _fixture_key(row)
    valid = True
    if key and key != expected_key:
        add_issue(IntegrityCode.ENTITY_MISMATCH, source, key, "fixture_key", "fixture key differs from expected fixture")
        valid = False
    observed_league = _league(row)
    if observed_league and observed_league not in _LEAGUE_ALIASES:
        add_issue(IntegrityCode.ENTITY_MISMATCH, source, key, "league", "record is outside Champions League scope")
        valid = False
    for side, expected_team in (("home", expected_home), ("away", expected_away)):
        observed_team = _team(row, side)
        if observed_team and observed_team != expected_team:
            add_issue(IntegrityCode.ENTITY_MISMATCH, source, key, f"{side}_team", "team identity differs from fixture")
            valid = False
    if _row_value(row, "kickoff", "commence_time") is not None:
        try:
            observed_kickoff = _kickoff(row)
        except ValueError as exc:
            add_issue(IntegrityCode.TIMEZONE_MISMATCH, source, key, "kickoff", str(exc))
            return False
        if abs((observed_kickoff - expected_kickoff).total_seconds()) > kickoff_tolerance_seconds:
            add_issue(IntegrityCode.KICKOFF_MISMATCH, source, key, "kickoff", "kickoff differs from fixture")
            valid = False
    return valid


def audit_champions_league_inputs(
    fixtures: Sequence[Mapping[str, object]],
    *,
    results: Sequence[Mapping[str, object]] = (),
    odds: Sequence[Mapping[str, object]] = (),
    features: Sequence[Mapping[str, object]] = (),
    serializer_inputs: Sequence[Mapping[str, object]] = (),
    cache_records: Sequence[Mapping[str, object]] = (),
    policy: ChampionsLeagueIntegrityPolicy,
) -> ChampionsLeagueIntegrityReport:
    """Audit all replay inputs and return deterministic, bounded diagnostics."""

    policy.validate()
    issues: list[IntegrityIssue] = []
    counts: Counter[str] = Counter()
    add = lambda *args: _add_issue(issues, counts, policy.max_issue_samples, *args)
    inventory = {
        "fixture_inputs": len(fixtures),
        "result_inputs": len(results),
        "odds_inputs": len(odds),
        "feature_inputs": len(features),
        "serializer_inputs": len(serializer_inputs),
        "cache_inputs": len(cache_records),
        "timestamp_inputs": 0,
        "team_identity_inputs": 0,
    }
    fixture_map: dict[str, tuple[str, str, str, datetime]] = {}
    fixture_order: list[str] = []
    if not fixtures:
        add(IntegrityCode.MISSING_FIXTURE, "fixtures", "", "fixture_key", "at least one fixture is required")
    for index, raw in enumerate(fixtures):
        source = "fixtures"
        if not isinstance(raw, Mapping):
            add(IntegrityCode.MISSING_FIXTURE, source, "", str(index), "fixture input must be an object")
            continue
        key = _fixture_key(raw)
        if not key:
            add(IntegrityCode.MISSING_FIXTURE, source, "", "fixture_key", "fixture identity is required")
            continue
        if key in fixture_map:
            add(IntegrityCode.DUPLICATE_FIXTURE, source, key, "fixture_key", "fixture identity appears more than once")
            continue
        if _league(raw) not in _LEAGUE_ALIASES:
            add(IntegrityCode.ENTITY_MISMATCH, source, key, "league", "fixture is not Champions League")
        home, away = _team(raw, "home"), _team(raw, "away")
        inventory["team_identity_inputs"] += int(bool(home)) + int(bool(away))
        if not home or not away or home == away:
            add(IntegrityCode.ENTITY_MISMATCH, source, key, "teams", "distinct home and away identities are required")
            continue
        try:
            kickoff = _kickoff(raw)
            inventory["timestamp_inputs"] += 1
        except ValueError as exc:
            add(IntegrityCode.TIMEZONE_MISMATCH, source, key, "kickoff", str(exc))
            continue
        fixture_map[key] = (key, home, away, kickoff)
        fixture_order.append(key)

    def expected_for(raw: object, source: str) -> tuple[str, str, str, datetime] | None:
        if not isinstance(raw, Mapping):
            add(IntegrityCode.MISSING_FIXTURE, source, "", "fixture_key", "attachment must be an object")
            return None
        key = _fixture_key(raw)
        expected = fixture_map.get(key)
        if expected is None:
            add(IntegrityCode.MISSING_FIXTURE, source, key, "fixture_key", "attachment references an unknown fixture")
            return None
        if not _validate_identity(raw, source, expected, policy.kickoff_tolerance_seconds, add):
            return None
        return expected

    seen_results: set[str] = set()
    for raw in results:
        expected = expected_for(raw, "results")
        if expected is None or not isinstance(raw, Mapping):
            continue
        key = expected[0]
        if key in seen_results:
            add(IntegrityCode.DUPLICATE_RESULT, "results", key, "fixture_key", "result identity appears more than once")
        seen_results.add(key)
        timestamp = _row_value(raw, "result_timestamp", "played_at", "timestamp")
        if timestamp is not None:
            try:
                result_at = _parse_datetime(timestamp, "result_timestamp")
                inventory["timestamp_inputs"] += 1
                if result_at < expected[3] and _as_text(_row_value(raw, "status")) in {"final", "finished", "completed"}:
                    add(IntegrityCode.KICKOFF_MISMATCH, "results", key, "result_timestamp", "final result precedes kickoff")
            except ValueError as exc:
                add(IntegrityCode.TIMEZONE_MISMATCH, "results", key, "result_timestamp", str(exc))
        for score_field in ("home_score", "away_score"):
            score = raw.get(score_field)
            if score is not None and (isinstance(score, bool) or not isinstance(score, int) or score < 0):
                add(IntegrityCode.MALFORMED_INPUT, "results", key, score_field, "score must be a non-negative integer")

    seen_odds: set[tuple[str, str, str]] = set()
    attached_odds: set[str] = set()
    for raw in odds:
        expected = expected_for(raw, "odds")
        if expected is None or not isinstance(raw, Mapping):
            continue
        key = expected[0]
        attached_odds.add(key)
        bookmaker = _as_text(_row_value(raw, "bookmaker_identity", "bookmaker", "bookie", "bookmaker_key"))
        provider = _as_text(_row_value(raw, "provider", "source", "source_identity"))
        if policy.require_bookmaker_provenance and (not bookmaker or not provider):
            add(IntegrityCode.MISSING_BOOKMAKER_PROVENANCE, "odds", key, "provenance", "bookmaker and provider identity are required")
        source_timestamp_value = _row_value(raw, "source_timestamp", "last_update", "odds_timestamp", "updated_at")
        captured_value = _row_value(raw, "captured_at", "retrieved_at", "observed_at")
        source_timestamp = None
        captured_at = policy.as_of_utc
        if source_timestamp_value is None:
            add(IntegrityCode.MISSING_TIMESTAMP, "odds", key, "source_timestamp", "odds source timestamp is required")
        else:
            try:
                source_timestamp = _parse_datetime(source_timestamp_value, "source_timestamp")
                inventory["timestamp_inputs"] += 1
                if captured_value is not None:
                    captured_at = _parse_datetime(captured_value, "captured_at")
                    inventory["timestamp_inputs"] += 1
                    if captured_at > policy.as_of_utc:
                        add(IntegrityCode.TIMESTAMP_MISMATCH, "odds", key, "captured_at", "capture timestamp is after replay as_of")
                if source_timestamp > captured_at:
                    add(IntegrityCode.TIMESTAMP_MISMATCH, "odds", key, "source_timestamp", "source timestamp is after capture")
                age = (policy.as_of_utc - source_timestamp).total_seconds()
                if age < 0:
                    add(IntegrityCode.TIMESTAMP_MISMATCH, "odds", key, "source_timestamp", "odds timestamp is in the future")
                elif age > policy.max_odds_age_seconds:
                    add(IntegrityCode.STALE_ODDS, "odds", key, "source_timestamp", "odds exceed the replay freshness bound")
            except ValueError as exc:
                add(IntegrityCode.TIMEZONE_MISMATCH, "odds", key, "source_timestamp", str(exc))
        if (
            captured_at > expected[3]
            and captured_at > expected[3].replace(microsecond=0)
            and (captured_at - expected[3]).total_seconds()
            > policy.post_kickoff_tolerance_seconds
        ):
            add(
                IntegrityCode.POST_KICKOFF_CONTAMINATION,
                "odds",
                key,
                "captured_at",
                "odds were captured after kickoff",
            )
        phase = _as_text(_row_value(raw, "market_phase", "snapshot_kind", "phase")).casefold()
        if raw.get("in_play") is True or phase in {"live", "in_play", "closing", "post_match"}:
            add(IntegrityCode.POST_KICKOFF_CONTAMINATION, "odds", key, "market_phase", "non-pre-match odds cannot enter replay input")
        odds_map = raw.get("odds") if isinstance(raw.get("odds"), Mapping) else raw
        normalized_odds: dict[str, object] = {}
        if isinstance(odds_map, Mapping):
            for outcome, aliases in {"home": ("home", "home_odds", "1"), "draw": ("draw", "draw_odds", "x"), "away": ("away", "away_odds", "2")}.items():
                value = next((odds_map[name] for name in aliases if name in odds_map), None)
                if value is not None:
                    normalized_odds[outcome] = value
        if set(normalized_odds) != set(_OUTCOMES) or any(
            not _finite_number(value) or float(value) <= 1.0
            for value in normalized_odds.values()
        ):
            add(IntegrityCode.MISSING_ODDS, "odds", key, "odds", "complete finite home/draw/away odds are required")
        snapshot_key = (key, bookmaker, source_timestamp.isoformat() if source_timestamp else "")
        if snapshot_key in seen_odds:
            add(IntegrityCode.DUPLICATE_ODDS, "odds", key, "snapshot", "odds snapshot identity appears more than once")
        seen_odds.add(snapshot_key)

    attached_features: set[str] = set()
    for raw in features:
        expected = expected_for(raw, "features")
        if expected is None or not isinstance(raw, Mapping):
            continue
        key = expected[0]
        attached_features.add(key)
        values = raw.get("features")
        if not isinstance(values, Mapping) or not values:
            add(IntegrityCode.MISSING_FEATURE, "features", key, "features", "feature mapping is missing or empty")
        elif any(not _as_text(name) or not _finite_number(value) for name, value in values.items()):
            add(IntegrityCode.MISSING_FEATURE, "features", key, "features", "feature values must be finite numbers with names")
        generated = _row_value(raw, "generated_at", "captured_at", "feature_timestamp")
        if generated is not None:
            try:
                generated_at = _parse_datetime(generated, "feature_timestamp")
                inventory["timestamp_inputs"] += 1
                if generated_at > expected[3] and (generated_at - expected[3]).total_seconds() > policy.post_kickoff_tolerance_seconds:
                    add(IntegrityCode.POST_KICKOFF_CONTAMINATION, "features", key, "generated_at", "features were generated after kickoff")
            except ValueError as exc:
                add(IntegrityCode.TIMEZONE_MISMATCH, "features", key, "generated_at", str(exc))

    attached_serializer: set[str] = set()
    for raw in serializer_inputs:
        if not isinstance(raw, Mapping):
            add(IntegrityCode.SERIALIZER_INPUT_MISMATCH, "serializer", "", "record", "serializer input must be an object")
            continue
        normalized = _nested_row(raw)
        expected = expected_for(normalized, "serializer")
        if expected is None:
            continue
        key = expected[0]
        attached_serializer.add(key)
        required = (
            ("prediction_id", ("prediction_id", "id")),
            ("snapshot_id", ("snapshot_id", "signal_snapshot_id")),
            ("generated_at", ("generated_at", "prediction_timestamp", "captured_at")),
        )
        for field, aliases in required:
            if _row_value(normalized, *aliases) is None:
                add(IntegrityCode.SERIALIZER_INPUT_MISMATCH, "serializer", key, field, "serializer provenance field is required")
        probabilities = normalized.get("probabilities") or normalized.get("model_probabilities")
        if not isinstance(probabilities, Mapping) or not probabilities:
            add(IntegrityCode.SERIALIZER_INPUT_MISMATCH, "serializer", key, "probabilities", "serializer probabilities are required")
        elif set(probabilities) != set(_OUTCOMES) or any(
            not _finite_number(value) or not 0 <= float(value) <= 1
            for value in probabilities.values()
        ) or abs(sum(float(value) for value in probabilities.values()) - 1.0) > 1e-9:
            add(IntegrityCode.SERIALIZER_INPUT_MISMATCH, "serializer", key, "probabilities", "serializer probabilities must be finite home/draw/away values summing to one")
        generated = _row_value(normalized, "generated_at", "prediction_timestamp", "captured_at")
        if generated is not None:
            try:
                generated_at = _parse_datetime(generated, "generated_at")
                inventory["timestamp_inputs"] += 1
                if generated_at > expected[3] and (generated_at - expected[3]).total_seconds() > policy.post_kickoff_tolerance_seconds:
                    add(IntegrityCode.POST_KICKOFF_CONTAMINATION, "serializer", key, "generated_at", "prediction was generated after kickoff")
            except ValueError as exc:
                add(IntegrityCode.TIMEZONE_MISMATCH, "serializer", key, "generated_at", str(exc))
        if normalized.get("no_bet") is not True:
            add(IntegrityCode.SERIALIZER_INPUT_MISMATCH, "serializer", key, "no_bet", "offline replay serializer input must be no-bet")
        if normalized.get("publication_enabled") is True or _as_text(normalized.get("activation_state")).casefold() == "live":
            add(IntegrityCode.SERIALIZER_INPUT_MISMATCH, "serializer", key, "publication_enabled", "offline replay serializer input must remain disabled")
        if _as_text(normalized.get("snapshot_kind")).casefold() in {"closing", "post_match"}:
            add(IntegrityCode.POST_KICKOFF_CONTAMINATION, "serializer", key, "snapshot_kind", "closing or post-match snapshot cannot enter replay input")

    for raw in cache_records:
        expected = expected_for(raw, "cache")
        if expected is None or not isinstance(raw, Mapping):
            continue
        key = expected[0]
        cache_time_value = _row_value(raw, "cache_updated_at", "updated_at", "cached_at", "retrieved_at")
        if raw.get("stale") is True:
            add(IntegrityCode.STALE_CACHE, "cache", key, "stale", "cache record is explicitly stale")
        if cache_time_value is None:
            add(IntegrityCode.STALE_CACHE, "cache", key, "cache_updated_at", "cache freshness timestamp is required")
            continue
        try:
            cache_time = _parse_datetime(cache_time_value, "cache_updated_at")
            inventory["timestamp_inputs"] += 1
            age = (policy.as_of_utc - cache_time).total_seconds()
            if age < 0 or age > policy.max_cache_age_seconds:
                add(IntegrityCode.STALE_CACHE, "cache", key, "cache_updated_at", "cache exceeds the replay freshness bound")
        except ValueError as exc:
            add(IntegrityCode.TIMEZONE_MISMATCH, "cache", key, "cache_updated_at", str(exc))

    for key in fixture_order:
        if policy.require_results and key not in seen_results:
            add(IntegrityCode.MISSING_RESULT, "results", key, "fixture_key", "fixture has no result attachment")
        if policy.require_odds and key not in attached_odds:
            add(IntegrityCode.MISSING_ODDS, "odds", key, "fixture_key", "fixture has no odds attachment")
        if policy.require_features and key not in attached_features:
            add(IntegrityCode.MISSING_FEATURE, "features", key, "fixture_key", "fixture has no feature attachment")
        if policy.require_serializer_inputs and key not in attached_serializer:
            add(IntegrityCode.SERIALIZER_INPUT_MISMATCH, "serializer", key, "fixture_key", "fixture has no serializer input")

    inventory = dict(inventory)
    issue_items = sorted(issues, key=lambda item: (item.code.value, item.source, item.fixture_key, item.field, item.detail))
    return ChampionsLeagueIntegrityReport(
        inventory=MappingProxyType(inventory),
        fixture_keys=tuple(sorted(fixture_map)),
        issue_counts=MappingProxyType(dict(sorted(counts.items()))),
        issues=tuple(issue_items[: policy.max_issue_samples]),
        max_issue_samples=policy.max_issue_samples,
    )


def assert_champions_league_integrity(
    fixtures: Sequence[Mapping[str, object]],
    *,
    policy: ChampionsLeagueIntegrityPolicy,
    **sources: Sequence[Mapping[str, object]],
) -> ChampionsLeagueIntegrityReport:
    """Validate inputs and raise only after returning a deterministic report shape."""

    report = audit_champions_league_inputs(fixtures, policy=policy, **sources)
    report.raise_if_invalid()
    return report

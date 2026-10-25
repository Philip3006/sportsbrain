"""Deterministic, read-only audit of local Champions League research data.

The auditor deliberately does not fetch data, infer missing seasons, or write
back to a dataset.  Its output is evidence about what is locally available and
whether that evidence is safe for later offline research.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "champions-league-dataset-readiness-v1"
COMPETITION = "UEFA Champions League"
REQUIRED_FIELDS = ("season", "kickoff_at", "home_team", "away_team", "home_score", "away_score")
COMPETITION_FIELDS = ("competition", "competition_name", "tournament", "league", "league_name")
_CL_COMPETITION_KEYS = {"ucl", "champions league", "uefa champions league", "soccer uefa champs league"}
_SEASON_RE = re.compile(r"^(\d{4})[-/](\d{2}|\d{4})$")
_PROHIBITED_PATH_PARTS = {".env", "private", "production", "ledger", "financial"}

DEFAULT_PARTITIONS: dict[str, tuple[str, ...]] = {
    "development": tuple(f"{year:04d}-{(year + 1) % 100:02d}" for year in range(2015, 2022)),
    "calibration": ("2022-23", "2023-24"),
    "final": ("2024-25",),
    "shadow": ("2025-26",),
}

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "season": ("season", "season_label"),
    "kickoff_at": ("kickoff_at", "kickoff", "kickoff_timestamp", "datetime", "date"),
    "home_team": ("home_team", "home_team_name", "home"),
    "away_team": ("away_team", "away_team_name", "away"),
    "home_score": ("home_score", "home_goals", "goals_home", "fthg"),
    "away_score": ("away_score", "away_goals", "goals_away", "ftag"),
    "fixture_id": ("fixture_id", "match_id", "event_id", "provider_fixture_id"),
    "result_id": ("result_id", "provider_result_id"),
    "home_team_id": ("home_team_id", "home_id", "provider_home_team_id"),
    "away_team_id": ("away_team_id", "away_id", "provider_away_team_id"),
    "source_timestamp": ("source_timestamp", "captured_at", "published_at", "retrieved_at"),
    "result_timestamp": ("result_timestamp", "result_published_at", "result_captured_at"),
}

FEATURE_SPECS: tuple[dict[str, Any], ...] = (
    {"feature_id": "odds_1x2", "field_sets": (("home_odds", "draw_odds", "away_odds"),), "timestamp": ("odds_captured_at", "odds_source_timestamp", "source_timestamp")},
    {"feature_id": "elo_pre_match", "field_sets": (("home_elo", "away_elo"), ("elo_home", "elo_away")), "timestamp": ("elo_as_of", "source_timestamp")},
    {"feature_id": "rolling_form", "field_sets": (("home_form", "away_form"), ("form_pts_diff", "form_gd_diff")), "timestamp": ("form_as_of", "source_timestamp")},
    {"feature_id": "xg", "field_sets": (("home_xg", "away_xg"), ("xg_home", "xg_away")), "timestamp": ("xg_source_timestamp", "source_timestamp")},
    {"feature_id": "ppda", "field_sets": (("home_ppda", "away_ppda"), ("ppda_home", "ppda_away")), "timestamp": ("ppda_source_timestamp", "source_timestamp")},
    {"feature_id": "squad_availability", "field_sets": (("home_squad_availability", "away_squad_availability"),), "timestamp": ("squad_source_timestamp", "source_timestamp")},
    {"feature_id": "market_value", "field_sets": (("home_market_value", "away_market_value"),), "timestamp": ("market_value_as_of", "source_timestamp")},
)

READ_ONLY_REFERENCES: tuple[tuple[str, str], ...] = (
    ("prior_research_contract", "docs/champions_league_model_research_readiness.md"),
    ("prior_candidate_manifest", "docs/champions_league_candidate_manifest.json"),
    ("prior_research_module", "src/football/champions_league_research.py"),
)

SOURCE_REFERENCES: tuple[dict[str, Any], ...] = (
    {"source_id": "football_data_co_uk", "paths": ("src/data/football_data.py",), "cl_coverage": "none", "finding": "local loader enumerates domestic leagues only"},
    {"source_id": "martj42_international_results", "paths": ("src/data/international.py",), "cl_coverage": "not_proven", "finding": "network-backed broad international results; no CL point-in-time snapshot"},
    {"source_id": "statsbomb_open_data", "paths": ("src/data/statsbomb.py",), "cl_coverage": "none", "finding": "configured competition IDs are World Cup, UEFA Euro, and Copa America"},
    {"source_id": "odds_api_discovery", "paths": ("src/data/football_discovery.py", "src/data/odds_api.py"), "cl_coverage": "live_or_discovery_only", "finding": "does not provide a committed historical CL dataset"},
)


class DatasetAuditError(ValueError):
    """Malformed, unsafe, or non-deterministic audit input."""


def canonical_season(value: object) -> str:
    text = str(value).strip().replace("/", "-")
    match = _SEASON_RE.fullmatch(text)
    if not match:
        raise DatasetAuditError(f"invalid season label: {value!r}")
    start, end = int(match.group(1)), int(match.group(2))
    if end >= 100:
        end %= 100
    if end != (start + 1) % 100:
        raise DatasetAuditError(f"season is not consecutive: {value!r}")
    return f"{start:04d}-{end:02d}"


def _season_key(value: str) -> int:
    return int(canonical_season(value)[:4])


def _text(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _field(row: Mapping[str, object], name: str) -> object:
    for alias in FIELD_ALIASES.get(name, (name,)):
        if alias in row and _text(row[alias]) is not None:
            return row[alias]
    return None


def _parse_timestamp(value: object) -> tuple[datetime | None, str]:
    text = _text(value)
    if not text:
        return None, "missing"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None, "date_only"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, "invalid"
    if parsed.tzinfo is None:
        return None, "un_zoned"
    return parsed.astimezone(timezone.utc), "instant"


def _canonical_team(value: object) -> str | None:
    text = _text(value)
    if not text:
        return None
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def _score(value: object) -> int | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() and parsed >= 0 else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: Path, root: Path) -> str:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    if root != resolved and root not in resolved.parents:
        raise DatasetAuditError("dataset path must be inside the audit root")
    relative = resolved.relative_to(root)
    parts = {part.casefold() for part in relative.parts}
    if parts & _PROHIBITED_PATH_PARTS:
        raise DatasetAuditError("dataset path is in a prohibited private/production area")
    return relative.as_posix()


def load_dataset(path: str | Path) -> tuple[list[dict[str, Any]], str]:
    """Load a local JSON/CSV dataset without changing it."""
    source = Path(path)
    if not source.is_file():
        raise DatasetAuditError(f"dataset does not exist: {source}")
    suffix = source.suffix.casefold()
    if suffix == ".csv":
        with source.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        return [dict(row) for row in rows], "csv"
    if suffix != ".json":
        raise DatasetAuditError("dataset format must be .json or .csv")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetAuditError(f"cannot read JSON dataset {source}") from exc
    if isinstance(payload, dict):
        for key in ("matches", "fixtures", "results", "data"):
            if key in payload:
                payload = payload[key]
                break
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise DatasetAuditError("JSON dataset must be a list of objects or contain a list field")
    return [dict(row) for row in payload], "json"


def _normal_row(row: Mapping[str, object], index: int) -> dict[str, Any]:
    kickoff, kickoff_status = _parse_timestamp(_field(row, "kickoff_at"))
    season_value = _field(row, "season")
    season = None
    season_status = "missing"
    if season_value is not None:
        try:
            season = canonical_season(season_value)
            season_status = "valid"
        except DatasetAuditError:
            season_status = "invalid"
    home, away = _text(_field(row, "home_team")), _text(_field(row, "away_team"))
    home_score, away_score = _score(_field(row, "home_score")), _score(_field(row, "away_score"))
    source_ts, source_status = _parse_timestamp(_field(row, "source_timestamp"))
    result_ts, result_status = _parse_timestamp(_field(row, "result_timestamp"))
    return {
        "index": index,
        "raw": row,
        "season": season,
        "season_status": season_status,
        "kickoff": kickoff,
        "kickoff_status": kickoff_status,
        "home_team": home,
        "away_team": away,
        "home_key": _canonical_team(home),
        "away_key": _canonical_team(away),
        "home_score": home_score,
        "away_score": away_score,
        "fixture_id": _text(_field(row, "fixture_id")),
        "result_id": _text(_field(row, "result_id")),
        "home_team_id": _text(_field(row, "home_team_id")),
        "away_team_id": _text(_field(row, "away_team_id")),
        "source_timestamp": source_ts,
        "source_status": source_status,
        "result_timestamp": result_ts,
        "result_status": result_status,
    }


def _status(blocked: bool, conditional: bool) -> str:
    return "BLOCKED" if blocked else "CONDITIONAL" if conditional else "PASS"


def _identity_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fixture_keys: list[str] = []
    fixture_ids: list[str] = []
    result_keys: list[str] = []
    missing = Counter()
    for row in rows:
        for field in ("season", "kickoff", "home_team", "away_team", "home_score", "away_score"):
            if row[field] is None:
                missing[field] += 1
        kickoff = row["kickoff"].isoformat() if row["kickoff"] else "invalid-kickoff"
        fixture_keys.append("|".join((row["season"] or "invalid-season", kickoff, row["home_key"] or "", row["away_key"] or "")))
        if row["fixture_id"]:
            fixture_ids.append(row["fixture_id"])
        result_keys.append(f"{fixture_keys[-1]}|{row['home_score']}|{row['away_score']}")
    duplicate_fixtures = sorted(key for key, count in Counter(fixture_keys).items() if count > 1)
    duplicate_ids = sorted(key for key, count in Counter(fixture_ids).items() if count > 1)
    status = _status(bool(missing or duplicate_fixtures), bool(rows) and len(fixture_ids) < len(rows))
    return {
        "status": status if rows else "BLOCKED",
        "row_count": len(rows),
        "required_field_missing_rows": dict(sorted(missing.items())),
        "explicit_fixture_id_coverage": len(fixture_ids) / len(rows) if rows else 0.0,
        "derived_fixture_key_count": len(rows) - len(fixture_ids),
        "duplicate_fixture_keys": duplicate_fixtures,
        "duplicate_fixture_ids": duplicate_ids,
        "result_identity": {
            "complete_result_rows": sum(row["home_score"] is not None and row["away_score"] is not None for row in rows),
            "unique_result_keys": len(set(result_keys)),
            "result_id_coverage": sum(bool(row["result_id"]) for row in rows) / len(rows) if rows else 0.0,
        },
    }


def _competition_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted({
        _text(row["raw"].get(field))
        for row in rows
        for field in COMPETITION_FIELDS
        if _text(row["raw"].get(field)) is not None
    })
    keys = {re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip() for value in values}
    unexpected = sorted(value for value in values if re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip() not in _CL_COMPETITION_KEYS)
    present_rows = sum(any(_text(row["raw"].get(field)) is not None for field in COMPETITION_FIELDS) for row in rows)
    return {
        "status": "BLOCKED" if unexpected else "PASS" if rows and present_rows == len(rows) and keys else "CONDITIONAL" if rows else "BLOCKED",
        "field_names": [field for field in COMPETITION_FIELDS if any(field in row["raw"] for row in rows)],
        "declared_value_coverage": present_rows / len(rows) if rows else 0.0,
        "observed_values": values,
        "unexpected_values": unexpected,
        "path_scope_is_not_substitute_for_field": True,
    }


def _team_mapping_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    id_to_names: dict[str, set[str]] = {}
    name_to_ids: dict[str, set[str]] = {}
    missing_names = 0
    missing_ids = 0
    for row in rows:
        for side in ("home", "away"):
            name, key, team_id = row[f"{side}_team"], row[f"{side}_key"], row[f"{side}_team_id"]
            if not name or not key:
                missing_names += 1
            if not team_id:
                missing_ids += 1
            if team_id and key:
                id_to_names.setdefault(team_id, set()).add(key)
                name_to_ids.setdefault(key, set()).add(team_id)
    id_conflicts = sorted(key for key, names in id_to_names.items() if len(names) > 1)
    name_conflicts = sorted(key for key, ids in name_to_ids.items() if len(ids) > 1)
    blocked = bool(missing_names or id_conflicts or name_conflicts)
    conditional = bool(missing_ids) and not blocked
    return {
        "status": _status(blocked, conditional) if rows else "BLOCKED",
        "team_side_count": len(rows) * 2,
        "unique_team_names": len({row[key] for row in rows for key in ("home_key", "away_key") if row[key]}),
        "explicit_team_id_coverage": 1.0 - (missing_ids / (len(rows) * 2)) if rows else 0.0,
        "missing_team_name_sides": missing_names,
        "missing_team_id_sides": missing_ids,
        "team_id_to_multiple_names": id_conflicts,
        "team_name_to_multiple_ids": name_conflicts,
    }


def _timestamp_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    source_present = sum(row["source_timestamp"] is not None for row in rows)
    before_kickoff = sum(row["source_timestamp"] is not None and row["kickoff"] is not None and row["source_timestamp"] < row["kickoff"] for row in rows)
    result_present = sum(row["result_timestamp"] is not None for row in rows)
    result_after_kickoff = sum(row["result_timestamp"] is not None and row["kickoff"] is not None and row["result_timestamp"] >= row["kickoff"] for row in rows)
    invalid_kickoff = sum(row["kickoff_status"] != "instant" for row in rows)
    invalid_source = sum(row["source_status"] != "instant" for row in rows)
    invalid_result = sum(row["result_status"] not in {"instant", "missing"} for row in rows)
    blocked = bool(invalid_kickoff or invalid_source or invalid_result or before_kickoff != len(rows))
    return {
        "status": _status(blocked, False) if rows else "BLOCKED",
        "kickoff_instant_coverage": sum(row["kickoff_status"] == "instant" for row in rows) / len(rows) if rows else 0.0,
        "source_timestamp_coverage": source_present / len(rows) if rows else 0.0,
        "source_before_kickoff_coverage": before_kickoff / len(rows) if rows else 0.0,
        "invalid_or_unzoned_kickoff_rows": invalid_kickoff,
        "invalid_or_unzoned_source_rows": invalid_source,
        "invalid_or_unzoned_result_rows": invalid_result,
        "same_or_after_kickoff_rows": source_present - before_kickoff,
        "result_timestamp_coverage": result_present / len(rows) if rows else 0.0,
        "result_after_kickoff_coverage": result_after_kickoff / len(rows) if rows else 0.0,
        "policy": "strict instant timestamps; source_timestamp must be timezone-aware and strictly before kickoff",
    }


def _feature_audit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = len(rows)
    findings = []
    for spec in FEATURE_SPECS:
        field_sets = spec["field_sets"]
        expected_fields = sorted({field for field_set in field_sets for field in field_set})
        present_fields = sorted(field for field in expected_fields if any(field in row["raw"] for row in rows))
        complete = sum(any(all(_text(row["raw"].get(field)) is not None for field in field_set) for field_set in field_sets) for row in rows)
        timestamp_fields = spec["timestamp"]
        timestamp_rows = sum(any(_text(row["raw"].get(field)) is not None for field in timestamp_fields) for row in rows)
        findings.append({
            "feature_id": spec["feature_id"],
            "status": "UNAVAILABLE" if not present_fields else "PASS" if complete == total and timestamp_rows == total else "CONDITIONAL",
            "present_fields": present_fields,
            "expected_fields": expected_fields,
            "complete_row_coverage": complete / total if total else 0.0,
            "timestamp_coverage": timestamp_rows / total if total else 0.0,
            "point_in_time_ready": bool(present_fields) and complete == total and timestamp_rows == total,
        })
    return findings


def _partition_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["season"] for row in rows if row["season"])
    unknown_seasons = sorted({row["season"] for row in rows if row["season"] is None})
    partitions: dict[str, Any] = {}
    for label, seasons in DEFAULT_PARTITIONS.items():
        present = sorted(season for season in seasons if counts[season])
        missing = sorted(set(seasons) - set(present), key=_season_key)
        partitions[label] = {
            "seasons": list(seasons),
            "present_seasons": present,
            "missing_seasons": missing,
            "row_count": sum(counts[season] for season in seasons),
            "selection_eligible": label not in {"final", "shadow"},
            "feature_fit_allowed": label == "development",
            "read_only": label == "final",
        }
    hard_missing = set(DEFAULT_PARTITIONS["development"] + DEFAULT_PARTITIONS["calibration"] + DEFAULT_PARTITIONS["final"]) - set(counts)
    status = "BLOCKED" if not rows or hard_missing or unknown_seasons else "PASS"
    return {
        "status": status,
        "counts_by_season": dict(sorted(counts.items(), key=lambda item: _season_key(item[0]))),
        "unknown_seasons": unknown_seasons,
        "unknown_or_unusable_rows": sum(row["season"] is None for row in rows),
        "partitions": partitions,
        "final_evaluation_guard": {
            "season": DEFAULT_PARTITIONS["final"][0],
            "locked_for_selection": True,
            "feature_fit_allowed": False,
            "mapping_fit_allowed": False,
            "mutated_by_audit": False,
            "scoring_requires_frozen_candidate": True,
        },
    }


def audit_rows(rows: Iterable[Mapping[str, object]]) -> dict[str, Any]:
    """Audit rows already loaded by the caller; never mutates the input."""
    normalized = [_normal_row(dict(row), index) for index, row in enumerate(rows)]
    return {
        "row_count": len(normalized),
        "competition_identity": _competition_audit(normalized),
        "fixture_result_identity": _identity_audit(normalized),
        "team_mapping": _team_mapping_audit(normalized),
        "timestamp_provenance": _timestamp_audit(normalized),
        "feature_availability": _feature_audit(normalized),
        "season_partition_feasibility": _partition_audit(normalized),
    }


def _reference_inventory(root: Path, references: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
    result = []
    for reference_id, relative in references:
        path = root / relative
        result.append({"reference_id": reference_id, "path": relative, "present": path.is_file(), "sha256": _sha256(path) if path.is_file() else None})
    return result


def discover_datasets(root: str | Path) -> list[Path]:
    directory = Path(root) / "data" / "champions_league"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.casefold() in {".json", ".csv"})


def build_manifest(root: str | Path = ".", dataset_paths: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Build a stable manifest using only local allow-listed dataset files."""
    base = Path(root).expanduser().resolve()
    paths = [Path(path) for path in dataset_paths] if dataset_paths is not None else discover_datasets(base)
    paths = sorted(paths, key=lambda path: _safe_relative(path, base))
    datasets: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    for path in paths:
        relative = _safe_relative(path, base)
        rows, file_format = load_dataset(path)
        identity = {
            "path": relative,
            "format": file_format,
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
            "columns": sorted({key for row in rows for key in row}),
        }
        audit = audit_rows(rows)
        datasets.append({"identity": identity, "audit": audit})
        all_rows.extend(rows)
    combined = audit_rows(all_rows)
    source_inventory = []
    for item in SOURCE_REFERENCES:
        source_inventory.append({
            **item,
            "paths": _reference_inventory(base, ((item["source_id"], path) for path in item["paths"])),
            "network_used_by_audit": False,
        })
    critical = ("competition_identity", "fixture_result_identity", "timestamp_provenance", "season_partition_feasibility")
    critical_blocked = any(combined[key]["status"] == "BLOCKED" for key in critical)
    mapping_conditional = combined["team_mapping"]["status"] == "CONDITIONAL"
    competition_conditional = combined["competition_identity"]["status"] == "CONDITIONAL"
    feature_conditional = any(item["status"] != "PASS" for item in combined["feature_availability"])
    if not all_rows:
        overall_status = "BLOCKED_NO_CL_DATA"
    elif critical_blocked:
        overall_status = "BLOCKED"
    elif mapping_conditional or competition_conditional or feature_conditional:
        overall_status = "CONDITIONAL"
    else:
        overall_status = "READY_FOR_OFFLINE_DATA_AUDIT"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "competition": COMPETITION,
        "scope": "offline research shadow/evidence only",
        "overall_status": overall_status,
        "dataset_location": "data/champions_league/",
        "input_contract": {
            "required_fields": list(REQUIRED_FIELDS),
            "accepted_formats": ["csv", "json"],
            "strict_kickoff_timestamp": True,
            "strict_source_timestamp": True,
        },
        "dataset_count": len(datasets),
        "row_count": len(all_rows),
        "dataset_bundle_sha256": hashlib.sha256(json.dumps([item["identity"] for item in datasets], sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "datasets": datasets,
        "combined_audit": combined,
        "partition_spec": {key: list(value) for key, value in DEFAULT_PARTITIONS.items()},
        "final_partition": combined["season_partition_feasibility"]["final_evaluation_guard"],
        "source_inventory": source_inventory,
        "read_only_references": _reference_inventory(base, READ_ONLY_REFERENCES),
        "safety": {"network_accessed": False, "production_mutated": False, "financial_data_accessed": False, "final_partition_mutated": False},
        "limitations": [
            "No local dataset is treated as Champions League evidence unless it is explicitly supplied under data/champions_league/.",
            "A date-only kickoff or source timestamp cannot establish point-in-time provenance.",
            "Team names without stable source IDs are conditional identity evidence, not a validated mapping.",
            "The final 2024-25 partition is read-only, excluded from fitting and selection, and requires a frozen candidate before scoring.",
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    payload["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
    return payload


__all__ = [
    "COMPETITION", "DEFAULT_PARTITIONS", "DatasetAuditError", "SCHEMA_VERSION",
    "audit_rows", "build_manifest", "canonical_season", "discover_datasets", "load_dataset",
]

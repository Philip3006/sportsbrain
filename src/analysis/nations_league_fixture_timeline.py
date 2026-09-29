"""Canonical, provenance-aware UEFA Nations League fixture timeline.

This module joins the frozen 512-result evaluation set to official UEFA
schedule evidence. A result row is never assigned a kickoff by date-only,
pair-only, or approximate matching. Missing or stale schedule evidence remains
explicitly unresolved.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.analysis.nations_league_competition_state import (
    _fixture_stage,
    canonical_digest,
    canonical_team,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS = (
    ROOT / "data/research/nations_league/historical_results_source_v1.json"
)
DEFAULT_CONTRACTS = ROOT / "data/research/nations_league/edition_contracts_v1.json"
DEFAULT_SCHEDULE = (
    ROOT / "data/research/nations_league/fixture_schedule_extract_v1.json"
)
DEFAULT_TIMELINE = ROOT / "results/research/nations_league_fixture_timeline_v1.json"
DEFAULT_COVERAGE = (
    ROOT / "results/audits/nations_league_fixture_timeline_coverage_v1.json"
)
SCHEMA = "uefa-nations-league-fixture-timeline-v1"
REFERENCE_AT = "2022-06-11T00:25:00Z"
LOCAL_ZONE = ZoneInfo("Europe/Paris")

# These scores are administrative awards, not matches with a result becoming
# observable at a normal post-kickoff time. Their official outcome evidence is
# retained, but result_safe_available_at is deliberately null.
AWARDED_FIXTURES = {
    ("2020/21", "2020-11-17", "Switzerland", "Ukraine"),
    ("2024/25", "2024-11-15", "Romania", "Kosovo"),
}
AWARDED_SOURCE_URLS = {
    (
        "2020/21",
        "2020-11-17",
        "Switzerland",
        "Ukraine",
    ): "https://www.uefa.com/news-media/news/0263-10f07669c421-a1b7e2cc6859-1000--ab-switzerland-v-ukraine/",
    (
        "2024/25",
        "2024-11-15",
        "Romania",
        "Kosovo",
    ): "https://www.uefa.com/uefanationsleague/news/028a-1a23b4c739a8-07fe752f503f-1000/",
}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return value


def _utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _result_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        row["edition"],
        row["date"],
        canonical_team(row["home_team"]),
        canonical_team(row["away_team"]),
    )


def _schedule_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        row["edition"],
        row["scheduled_date"],
        canonical_team(row["home_team"]),
        canonical_team(row["away_team"]),
    )


def _fixture_id(key: tuple[str, str, str, str]) -> str:
    return "uefa-nl:" + hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:24]


def _kickoff_utc(row: dict[str, Any]) -> str:
    zone = ZoneInfo(row["timezone"])
    local = datetime.combine(
        date.fromisoformat(row["scheduled_date"]),
        time.fromisoformat(row["scheduled_local_time"]),
    ).replace(tzinfo=zone)
    return _utc(local)


def _phase_projection(
    kickoff_utc: str | None, reference_at: datetime
) -> dict[str, Any]:
    if kickoff_utc is None:
        return {
            "relative_to_reference": "unknown_kickoff_unresolved",
            "initial_t_minus_24h_at": None,
            "refinement_t_minus_90m_at": None,
            "closing_benchmark_kickoff_boundary_at": None,
            "closing_benchmark_capture_at": None,
            "closing_benchmark_status": "not_observed",
            "initial_due_state_at_reference": "unknown",
            "refinement_due_state_at_reference": "unknown",
            "closing_benchmark_due_state_at_reference": "unknown",
            "closing_odds_prediction_input": False,
        }
    kickoff = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))

    def due_state(at: datetime) -> str:
        if at > reference_at:
            return "pending_at_reference"
        if at == reference_at:
            return "at_reference"
        return "past_at_reference"

    initial = kickoff - timedelta(hours=24)
    refinement = kickoff - timedelta(minutes=90)
    return {
        "relative_to_reference": "future" if kickoff > reference_at else "at_or_before",
        "initial_t_minus_24h_at": _utc(initial),
        "refinement_t_minus_90m_at": _utc(refinement),
        "closing_benchmark_kickoff_boundary_at": _utc(kickoff),
        "closing_benchmark_capture_at": None,
        "closing_benchmark_status": "kickoff_boundary_only_not_an_observed_capture",
        "initial_due_state_at_reference": due_state(initial),
        "refinement_due_state_at_reference": due_state(refinement),
        "closing_benchmark_due_state_at_reference": due_state(kickoff),
        "closing_odds_prediction_input": False,
    }


def validate_causal_cutoff_order(
    result_safe_available_at: str,
    training_cutoff: str,
    prediction_cutoff: str,
    target_kickoff: str,
) -> None:
    """Enforce B1's strict result-safe < train < prediction < kickoff order."""

    timestamps = [
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        for value in (
            result_safe_available_at,
            training_cutoff,
            prediction_cutoff,
            target_kickoff,
        )
    ]
    if any(timestamp.tzinfo is None for timestamp in timestamps):
        raise ValueError("causal cutoff timestamps must be timezone-aware")
    if not timestamps[0] < timestamps[1] < timestamps[2] < timestamps[3]:
        raise ValueError(
            "required ordering is result_safe < training < prediction < target kickoff"
        )


def build_timeline(
    results: dict[str, Any], contracts: dict[str, Any], schedule: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = results["matches"]
    schedule_rows = schedule["schedule_rows"]
    by_exact: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    by_pair: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in schedule_rows:
        by_exact[_schedule_key(item)].append(item)
        by_pair[
            (
                item["edition"],
                canonical_team(item["home_team"]),
                canonical_team(item["away_team"]),
            )
        ].append(item)

    maps: dict[str, dict[str, tuple[str, str]]] = {}
    for edition, contract in contracts["editions"].items():
        team_map: dict[str, tuple[str, str]] = {}
        for group, teams in contract["groups"].items():
            for team in teams:
                team_map[canonical_team(team)] = (group[0], group)
        maps[edition] = team_map

    result_source_digest = results["snapshot_digest"]
    schedule_source_digest = canonical_digest(schedule)
    reference = datetime.fromisoformat(REFERENCE_AT.replace("Z", "+00:00"))
    records: list[dict[str, Any]] = []
    identities: Counter[tuple[str, str, str, str]] = Counter()

    for row in rows:
        key = _result_key(row)
        identities[key] += 1
        team_map = maps[row["edition"]]
        stage, tier, group = _fixture_stage(
            {
                "edition": row["edition"],
                "validation_period": row["validation_period"],
                "date": row["date"],
                "home_team": key[2],
                "away_team": key[3],
            },
            team_map,
        )
        exact = by_exact.get(key, [])
        pair = by_pair.get((row["edition"], key[2], key[3]), [])
        eligible = [
            candidate
            for candidate in exact
            if candidate["schedule_status"] == "official_fixture_candidate"
            or candidate["schedule_status"] == "official_exact_fixture"
        ]
        if len(eligible) == 1:
            match = eligible[0]
            kickoff = _kickoff_utc(match)
            provenance_status = "official_schedule_exact_crosswalk"
            unresolved_reason = None
            schedule_ref = {
                "url": match["source_url"],
                "source_digest": match["source_pdf_sha256"] or canonical_digest(match),
                "source_digest_kind": "raw_pdf_sha256"
                if match["source_pdf_sha256"]
                else "normalized_schedule_row_sha256",
                "schedule_status": match["schedule_status"],
            }
            local = {
                "date": match["scheduled_date"],
                "time": match["scheduled_local_time"],
                "timezone": match["timezone"],
                "timezone_semantics": "IANA civil time with DST rules; not a fixed UTC offset",
            }
        else:
            kickoff = None
            local = None
            schedule_ref = None
            if len(eligible) > 1:
                provenance_status, unresolved_reason = (
                    "ambiguous_schedule_crosswalk",
                    "multiple_exact_schedule_rows",
                )
            elif exact:
                provenance_status, unresolved_reason = (
                    "superseded_schedule_only",
                    "only_superseded_or_unverified_exact_schedule_rows",
                )
            elif pair:
                provenance_status, unresolved_reason = (
                    "date_conflict_unresolved",
                    "participant_pair_schedule_date_differs_from_result_source",
                )
            else:
                provenance_status, unresolved_reason = (
                    "schedule_missing",
                    "no_schedule_candidate_for_edition_and_participants",
                )

        identity_digest = canonical_digest(row)
        abnormal = key in AWARDED_FIXTURES
        kickoff_dt = (
            datetime.fromisoformat(kickoff.replace("Z", "+00:00")) if kickoff else None
        )
        safe_at = (
            _utc(kickoff_dt + timedelta(hours=6))
            if kickoff_dt is not None and not abnormal
            else None
        )
        state = {
            "fixture_id": _fixture_id(key),
            "source_fixture_id": None,
            "source_fixture_id_status": "not_present_in_frozen_result_or_schedule_sources",
            "competition": "UEFA Nations League",
            "edition": row["edition"],
            "validation_period": row["validation_period"],
            "tier": tier,
            "stage": stage,
            "group": group,
            "home_team": key[2],
            "away_team": key[3],
            "date": row["date"],
            "source_result_date": row["date"],
            "scheduled_date": row["date"],
            "local_kickoff": local,
            "kickoff_utc": kickoff,
            "status": "administratively_awarded"
            if abnormal
            else "completed_result_recorded",
            "home_score": row["home_score"],
            "away_score": row["away_score"],
            "result_safe_available_at": safe_at,
            "result_safe_methodology": (
                "null_for_administrative_award_without_played_match"
                if abnormal
                else "verified_scheduled_kickoff_plus_6h_conservative_completion_buffer"
                if kickoff is not None
                else "not_computed_without_verified_kickoff"
            ),
            "result_safe_status": "unresolved_award_availability"
            if abnormal
            else "bounded_from_verified_kickoff"
            if kickoff is not None
            else "unresolved_kickoff",
            "source_refs": {
                "result_source": results.get("upstream_url"),
                "result_source_digest": result_source_digest,
                "result_status_source": AWARDED_SOURCE_URLS.get(key),
                "schedule_source": schedule_ref,
                "schedule_extract_digest": schedule_source_digest,
            },
            "provenance_status": provenance_status,
            "unresolved_reason": "administrative_award_has_no_played_match_completion_time"
            if abnormal
            else unresolved_reason,
            "unresolved_schedule_candidates": [
                {
                    "date": item["scheduled_date"],
                    "local_time": item["scheduled_local_time"],
                    "timezone": item["timezone"],
                    "source_url": item["source_url"],
                    "schedule_status": item["schedule_status"],
                }
                for item in (exact if exact else pair)
            ]
            if kickoff is None
            else [],
            "normalized_source_digest": identity_digest,
            "reference_projection": _phase_projection(kickoff, reference),
        }
        state["record_digest"] = canonical_digest(state)
        records.append(state)

    records.sort(
        key=lambda item: (
            item["edition"],
            item["scheduled_date"],
            item["home_team"],
            item["away_team"],
            item["fixture_id"],
        )
    )
    duplicate_keys = [list(key) for key, count in identities.items() if count != 1]
    duplicate_fixture_ids = sorted(
        fixture_id
        for fixture_id, count in Counter(r["fixture_id"] for r in records).items()
        if count != 1
    )
    unresolved = [
        {
            "fixture_id": r["fixture_id"],
            "edition": r["edition"],
            "date": r["scheduled_date"],
            "home_team": r["home_team"],
            "away_team": r["away_team"],
            "reason": r["unresolved_reason"],
            "candidate_count": len(r["unresolved_schedule_candidates"]),
        }
        for r in records
        if r["kickoff_utc"] is None or r["result_safe_available_at"] is None
    ]
    counts_by_edition: dict[str, dict[str, int]] = {}
    for edition in contracts["editions"]:
        edition_rows = [r for r in records if r["edition"] == edition]
        counts_by_edition[edition] = {
            "fixtures": len(edition_rows),
            "verified_kickoffs": sum(
                r["kickoff_utc"] is not None for r in edition_rows
            ),
            "unresolved_kickoffs": sum(r["kickoff_utc"] is None for r in edition_rows),
            "result_safe_bounds": sum(
                r["result_safe_available_at"] is not None for r in edition_rows
            ),
        }
    target_cutoff_rows = [r for r in records if r["scheduled_date"] == "2022-06-11"]
    cutoff_report = [
        {
            "fixture_id": r["fixture_id"],
            "home_team": r["home_team"],
            "away_team": r["away_team"],
            "kickoff_utc": r["kickoff_utc"],
            "relative_to_2022_06_11T00_25Z": r["reference_projection"][
                "relative_to_reference"
            ],
        }
        for r in target_cutoff_rows
    ]

    timeline: dict[str, Any] = {
        "schema_version": SCHEMA,
        "competition": "UEFA Nations League",
        "normalization_version": results["normalization_version"],
        "schedule_source_digest": schedule_source_digest,
        "results_source_digest": result_source_digest,
        "reference_at": REFERENCE_AT,
        "records": records,
    }
    timeline["dataset_digest"] = canonical_digest(timeline)
    coverage: dict[str, Any] = {
        "schema_version": "uefa-nations-league-fixture-timeline-coverage-v1",
        "status": "NL_FIXTURE_TIMELINE_READY"
        if len(records) == 512
        and all(r["kickoff_utc"] and r["result_safe_available_at"] for r in records)
        and not duplicate_keys
        and not duplicate_fixture_ids
        else "NL_FIXTURE_TIMELINE_PARTIAL",
        "source_results_count": len(rows),
        "timeline_record_count": len(records),
        "identity_crosswalk_complete": len(records) == 512
        and not duplicate_keys
        and not duplicate_fixture_ids,
        "unique_fixture_ids": len({r["fixture_id"] for r in records}),
        "verified_utc_kickoffs": sum(r["kickoff_utc"] is not None for r in records),
        "result_safe_bounds": sum(
            r["result_safe_available_at"] is not None for r in records
        ),
        "source_fixture_ids_missing": sum(
            r["source_fixture_id"] is None for r in records
        ),
        "ambiguous_identity_conflicts": len(duplicate_keys)
        + len(duplicate_fixture_ids),
        "source_identity_duplicates": duplicate_keys,
        "duplicate_fixture_ids": duplicate_fixture_ids,
        "by_edition": counts_by_edition,
        "reference_cutoff": {
            "at": REFERENCE_AT,
            "fixtures_on_date": len(target_cutoff_rows),
            "fixtures": cutoff_report,
        },
        "unresolved_count": len(unresolved),
        "unresolved_fixtures": unresolved,
        "dataset_digest": timeline["dataset_digest"],
    }
    coverage["coverage_digest"] = canonical_digest(coverage)
    validate_timeline(timeline, coverage)
    return timeline, coverage


def validate_timeline(timeline: dict[str, Any], coverage: dict[str, Any]) -> None:
    if timeline.get("schema_version") != SCHEMA:
        raise ValueError("unsupported fixture timeline schema")
    records = timeline.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("fixture timeline must contain result-source rows")
    if len(records) != coverage.get("timeline_record_count"):
        raise ValueError("coverage record count does not match timeline")
    ids: set[str] = set()
    for record in records:
        if record["fixture_id"] in ids:
            raise ValueError("duplicate canonical fixture_id")
        ids.add(record["fixture_id"])
        if record["kickoff_utc"]:
            kickoff = datetime.fromisoformat(
                record["kickoff_utc"].replace("Z", "+00:00")
            )
            if kickoff.tzinfo is None:
                raise ValueError("kickoff must be timezone-aware")
            if record["local_kickoff"] is None:
                raise ValueError("verified UTC kickoff requires local provenance")
        if record["result_safe_available_at"] is not None:
            safe_at = datetime.fromisoformat(
                record["result_safe_available_at"].replace("Z", "+00:00")
            )
            kickoff = datetime.fromisoformat(
                record["kickoff_utc"].replace("Z", "+00:00")
            )
            if not safe_at > kickoff:
                raise ValueError("result-safe bound must be after kickoff")
        if (
            record["provenance_status"] == "official_schedule_exact_crosswalk"
            and record["kickoff_utc"] is None
        ):
            raise ValueError("verified crosswalk missing UTC kickoff")
        if record["record_digest"] != canonical_digest(
            {k: v for k, v in record.items() if k != "record_digest"}
        ):
            raise ValueError("fixture timeline record digest mismatch")
    digest_payload = {k: v for k, v in timeline.items() if k != "dataset_digest"}
    if timeline["dataset_digest"] != canonical_digest(digest_payload):
        raise ValueError("fixture timeline dataset digest mismatch")
    if coverage["coverage_digest"] != canonical_digest(
        {k: v for k, v in coverage.items() if k != "coverage_digest"}
    ):
        raise ValueError("fixture timeline coverage digest mismatch")
    if (
        coverage["verified_utc_kickoffs"] >= 512
        and coverage["status"] != "NL_FIXTURE_TIMELINE_READY"
    ):
        raise ValueError("complete verified timeline must be marked ready")


def load_and_validate(
    timeline_path: Path = DEFAULT_TIMELINE, coverage_path: Path = DEFAULT_COVERAGE
) -> tuple[dict[str, Any], dict[str, Any]]:
    timeline, coverage = _read(timeline_path), _read(coverage_path)
    validate_timeline(timeline, coverage)
    return timeline, coverage

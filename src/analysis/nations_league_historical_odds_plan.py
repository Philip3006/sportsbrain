"""Offline deterministic Nations League historical-odds request planner."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TIMELINE_HEAD_SHA = "065c6b40eb9911df3703d2e3079730a556136ee3"
TIMELINE_DATASET_DIGEST = "2c60c6b823b0cae948947fffe2a0e3456495c1fc5d510379ae95690e1c3fa6ef"
TIMELINE_SCHEMA = "uefa-nations-league-fixture-timeline-v1"
HISTORICAL_COVERAGE_BOUNDARY = "2022-06-11T00:25:00Z"
PROVIDER = "the_odds_api"
SPORT_KEY = "soccer_uefa_nations_league"
REQUEST_COST_CREDITS = 10
EXPECTED_FIXTURE_COUNT = 512
EXPECTED_BEFORE_COVERAGE = 233
EXPECTED_AFTER_COVERAGE = 279
PHASE_OFFSETS = {
    "INITIAL": timedelta(hours=-24),
    "REFINEMENT": timedelta(minutes=-90),
    "CLOSING": timedelta(0),
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _utc(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an ISO-8601 UTC string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def verify_timeline(
    path: Path,
    *,
    expected_head_sha: str = TIMELINE_HEAD_SHA,
    expected_digest: str = TIMELINE_DATASET_DIGEST,
) -> tuple[dict[str, Any], str]:
    """Verify the exact PR #215 timeline before any plan is generated."""
    timeline = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(timeline, dict) or timeline.get("schema_version") != TIMELINE_SCHEMA:
        raise ValueError("unexpected fixture timeline schema")
    if timeline.get("dataset_digest") != expected_digest:
        raise ValueError("timeline dataset digest does not match PR #215")
    digest_payload = dict(timeline)
    digest_payload.pop("dataset_digest", None)
    if canonical_digest(digest_payload) != expected_digest:
        raise ValueError("timeline content digest does not match PR #215")
    records = timeline.get("records")
    if not isinstance(records, list) or len(records) != EXPECTED_FIXTURE_COUNT:
        raise ValueError("timeline must contain exactly 512 records")
    fixture_ids = [record.get("fixture_id") for record in records]
    if any(not isinstance(item, str) or not item for item in fixture_ids):
        raise ValueError("every timeline record needs a canonical fixture_id")
    if len(set(fixture_ids)) != len(fixture_ids):
        raise ValueError("duplicate canonical fixture_id")
    for record in records:
        if record.get("kickoff_utc") is None:
            raise ValueError("unresolved kickoff in ready timeline")
        _utc(record["kickoff_utc"], "kickoff_utc")
        if not record.get("record_digest"):
            raise ValueError("timeline record digest is required")
    return timeline, expected_head_sha


def _is_admin(record: dict[str, Any]) -> bool:
    return bool(record.get("administrative_exception")) or record.get("result_safe_available_at") is None


def classify_fixtures(
    records: list[dict[str, Any]], *, coverage_boundary: str = HISTORICAL_COVERAGE_BOUNDARY
) -> list[dict[str, Any]]:
    boundary = _utc(coverage_boundary, "coverage_boundary")
    output = []
    for record in records:
        kickoff = record.get("kickoff_utc")
        admin = _is_admin(record)
        if kickoff is None:
            classification = "UNUSABLE"
            reason = "missing_verified_kickoff"
        elif _utc(kickoff, "kickoff_utc") < boundary:
            classification = "BEFORE_PROVIDER_COVERAGE"
            reason = "before_historical_coverage_boundary"
        elif admin:
            classification = "AFTER_PROVIDER_COVERAGE_ADMINISTRATIVE_EXCEPTION"
            reason = "pre_match_odds_not_dependent_on_result_safe_timestamp"
        else:
            classification = "AFTER_PROVIDER_COVERAGE"
            reason = "historical_snapshot_eligible"
        output.append(
            {
                "fixture_id": record.get("fixture_id"),
                "edition": record.get("edition"),
                "home_team": record.get("home_team"),
                "away_team": record.get("away_team"),
                "kickoff_utc": kickoff,
                "record_digest": record.get("record_digest"),
                "administrative_exception": record.get("administrative_exception"),
                "result_safe_status": record.get("result_safe_status"),
                "classification": classification,
                "historical_odds_eligible": classification.startswith("AFTER_PROVIDER_COVERAGE"),
                "classification_reason": reason,
            }
        )
    return output


def _request(phase: str, timestamp: datetime, fixture_ids: list[str]) -> dict[str, Any]:
    payload = {
        "provider": PROVIDER,
        "sport_key": SPORT_KEY,
        "endpoint_family": "historical_odds_bulk_snapshot",
        "phase": phase,
        "historical_snapshot_at": timestamp.isoformat().replace("+00:00", "Z"),
        "fixture_ids": fixture_ids,
        "fixture_count": len(fixture_ids),
        "estimated_credits": REQUEST_COST_CREDITS,
    }
    payload["request_digest"] = canonical_digest(payload)
    return payload


def _phase(
    phase: str,
    eligible: list[dict[str, Any]],
    offset: timedelta,
    coverage_boundary: datetime,
) -> dict[str, Any]:
    buckets: defaultdict[datetime, list[str]] = defaultdict(list)
    phase_exclusions = []
    phase_eligible = []
    for item in eligible:
        snapshot_at = _utc(item["kickoff_utc"], "kickoff_utc") + offset
        if snapshot_at < coverage_boundary:
            phase_exclusions.append(
                {
                    "fixture_id": item["fixture_id"],
                    "kickoff_utc": item["kickoff_utc"],
                    "requested_snapshot_at": snapshot_at.isoformat().replace("+00:00", "Z"),
                    "reason": "phase_snapshot_before_provider_coverage_boundary",
                }
            )
            continue
        phase_eligible.append(item)
        buckets[snapshot_at].append(item["fixture_id"])
    requests = [_request(phase, timestamp, sorted(ids)) for timestamp, ids in sorted(buckets.items())]
    fixture_count = len(phase_eligible)
    unique_count = len(requests)
    return {
        "phase": phase,
        "fixtures_covered": fixture_count,
        "unique_historical_timestamps": unique_count,
        "unique_http_requests": unique_count,
        "naive_per_fixture_requests": fixture_count,
        "estimated_credits": unique_count * REQUEST_COST_CREDITS,
        "naive_estimated_credits": fixture_count * REQUEST_COST_CREDITS,
        "deduplication_savings": {
            "requests": fixture_count - unique_count,
            "credits": (fixture_count - unique_count) * REQUEST_COST_CREDITS,
        },
        "phase_exclusions": phase_exclusions,
        "requests": requests,
    }


def _edition_coverage(classifications: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    result = {}
    for edition in sorted({item["edition"] for item in classifications}):
        rows = [item for item in classifications if item["edition"] == edition]
        result[edition] = {
            "fixtures": len(rows),
            "before_provider_coverage": sum(item["classification"] == "BEFORE_PROVIDER_COVERAGE" for item in rows),
            "after_provider_coverage": sum(item["classification"].startswith("AFTER_PROVIDER_COVERAGE") for item in rows),
            "administrative_exceptions": sum(bool(item["administrative_exception"]) for item in rows),
            "historical_odds_eligible": sum(item["historical_odds_eligible"] for item in rows),
        }
    return result


def _plan_total(phases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    shared: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {"phases": [], "fixture_ids": []}
    )
    naive_requests = 0
    for phase, details in phases.items():
        naive_requests += details["naive_per_fixture_requests"]
        for request in details["requests"]:
            item = shared[request["historical_snapshot_at"]]
            item["phases"].append(phase)
            item["fixture_ids"].extend(request["fixture_ids"])
    shared_requests = []
    for timestamp, item in sorted(shared.items()):
        payload = {
            "provider": PROVIDER,
            "sport_key": SPORT_KEY,
            "endpoint_family": "historical_odds_bulk_snapshot",
            "historical_snapshot_at": timestamp,
            "phases": sorted(set(item["phases"])),
            "fixture_ids": sorted(set(item["fixture_ids"])),
        }
        payload["request_digest"] = canonical_digest(payload)
        shared_requests.append(payload)
    unique_requests = len(shared_requests)
    return {
        "phase_fixture_counts": {
            phase: details["fixtures_covered"] for phase, details in phases.items()
        },
        "unique_historical_timestamps": unique_requests,
        "unique_http_requests": unique_requests,
        "naive_per_fixture_requests": naive_requests,
        "estimated_credits": unique_requests * REQUEST_COST_CREDITS,
        "naive_estimated_credits": naive_requests * REQUEST_COST_CREDITS,
        "deduplication_savings": {
            "requests": naive_requests - unique_requests,
            "credits": (naive_requests - unique_requests) * REQUEST_COST_CREDITS,
        },
        "shared_bulk_requests": shared_requests,
    }


def build_plan(
    timeline: dict[str, Any], *, timeline_head_sha: str = TIMELINE_HEAD_SHA,
    timeline_digest: str = TIMELINE_DATASET_DIGEST,
) -> dict[str, Any]:
    classifications = classify_fixtures(timeline["records"])
    counts = Counter(item["classification"] for item in classifications)
    if len(classifications) == EXPECTED_FIXTURE_COUNT:
        if counts["BEFORE_PROVIDER_COVERAGE"] != EXPECTED_BEFORE_COVERAGE:
            raise ValueError("unexpected pre-coverage fixture count")
        after = counts["AFTER_PROVIDER_COVERAGE"] + counts["AFTER_PROVIDER_COVERAGE_ADMINISTRATIVE_EXCEPTION"]
        if after != EXPECTED_AFTER_COVERAGE or counts["UNUSABLE"]:
            raise ValueError("timeline contains unexpected unusable classifications")
    eligible = [item for item in classifications if item["historical_odds_eligible"]]
    coverage_boundary = _utc(HISTORICAL_COVERAGE_BOUNDARY, "coverage_boundary")
    phases = {
        name: _phase(name, eligible, offset, coverage_boundary)
        for name, offset in PHASE_OFFSETS.items()
    }
    prediction_only = {
        phase: phases[phase] for phase in ("INITIAL", "REFINEMENT")
    }
    full_research = {
        phase: phases[phase] for phase in ("INITIAL", "REFINEMENT", "CLOSING")
    }
    payload: dict[str, Any] = {
        "schema_version": "nl-historical-odds-request-plan-v1",
        "status_marker": "NL_HISTORICAL_ODDS_FINAL_PLAN_READY",
        "research_only": True,
        "provider_requests_performed": 0,
        "credentials_accessed": False,
        "production_side_effects": False,
        "provider": PROVIDER,
        "sport_key": SPORT_KEY,
        "coverage_boundary": HISTORICAL_COVERAGE_BOUNDARY,
        "request_cost_credits": REQUEST_COST_CREDITS,
        "timeline": {
            "pr": 215, "head_sha": timeline_head_sha, "dataset_digest": timeline_digest,
            "schema_version": TIMELINE_SCHEMA, "record_count": len(timeline["records"]),
        },
        "fixture_universe": {
            "total": len(classifications),
            "before_provider_coverage": sum(item["classification"] == "BEFORE_PROVIDER_COVERAGE" for item in classifications),
            "after_provider_coverage": sum(item["classification"].startswith("AFTER_PROVIDER_COVERAGE") for item in classifications),
            "administrative_exceptions": sum(bool(item["administrative_exception"]) for item in classifications),
            "genuinely_unusable": sum(item["classification"] == "UNUSABLE" for item in classifications),
            "historical_odds_eligible": len(eligible),
        },
        "coverage_by_edition": _edition_coverage(classifications),
        "fixture_classifications": classifications,
        "plans": {
            "PREDICTION_ONLY": prediction_only,
            "FULL_RESEARCH": full_research,
        },
        "plan_totals": {
            "PREDICTION_ONLY": _plan_total(prediction_only),
            "FULL_RESEARCH": _plan_total(full_research),
        },
        "offline_backfill_executor": {
            "existing_manifest_found": False,
            "superseded_request_plan_digest": None,
            "action": "no_manifest_update_required",
        },
    }
    payload["request_plan_digest"] = canonical_digest(payload)
    return payload


def render_markdown(plan: dict[str, Any]) -> str:
    universe = plan["fixture_universe"]
    lines = [
        "# Nations League historical odds request plan", "", "`NL_HISTORICAL_ODDS_FINAL_PLAN_READY`", "",
        "Offline deterministic plan only; no provider request or credential access occurred.", "",
        f"- PR #215 head: `{plan['timeline']['head_sha']}`",
        f"- Timeline digest: `{plan['timeline']['dataset_digest']}`",
        f"- Request-plan digest: `{plan['request_plan_digest']}`",
        f"- Historical coverage boundary: `{plan['coverage_boundary']}`",
        (
            f"- Fixture universe: `{universe['total']}`; before coverage: "
            f"`{universe['before_provider_coverage']}`; after coverage / odds "
            f"eligible: `{universe['historical_odds_eligible']}`"
        ),
        f"- Administrative exceptions: `{universe['administrative_exceptions']}`; genuinely unusable: `{universe['genuinely_unusable']}`",
        "", "## Plan totals", "",
        "| Plan | Phase | Fixtures | Unique timestamps | HTTP requests | Credits | Dedup savings |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for plan_name, phases in plan["plans"].items():
        for phase, details in phases.items():
            lines.append(
                f"| {plan_name} | {phase} | {details['fixtures_covered']} | "
                f"{details['unique_historical_timestamps']} | {details['unique_http_requests']} | "
                f"{details['estimated_credits']} | {details['deduplication_savings']['requests']} requests |"
            )
            if details["phase_exclusions"]:
                lines.append(
                    f"  - phase exclusions: `{len(details['phase_exclusions'])}` "
                    "(snapshot before provider coverage boundary)"
                )
        total = plan["plan_totals"][plan_name]
        lines.append(
            f"| {plan_name} | DEDUPLICATED TOTAL | {sum(total['phase_fixture_counts'].values())} | "
            f"{total['unique_historical_timestamps']} | {total['unique_http_requests']} | "
            f"{total['estimated_credits']} | {total['deduplication_savings']['requests']} requests |"
        )
    lines.extend(["", "## Edition coverage", "", "| Edition | Fixtures | Before | After | Admin | Eligible |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for edition, counts in plan["coverage_by_edition"].items():
        lines.append(
            f"| {edition} | {counts['fixtures']} | {counts['before_provider_coverage']} | "
            f"{counts['after_provider_coverage']} | {counts['administrative_exceptions']} | {counts['historical_odds_eligible']} |"
        )
    lines.extend(["", "## Administrative exceptions", "", "The pre-coverage exception is excluded. The post-coverage exception remains eligible for pre-match odds retrieval; result-safe timing is not needed for an odds request."])
    for item in plan["fixture_classifications"]:
        if item["administrative_exception"]:
            lines.append(f"- `{item['fixture_id']}` {item['home_team']}–{item['away_team']}: `{item['classification']}` — {item['classification_reason']}")
    lines.extend(["", "The JSON artifact contains all 512 classifications and every exact timestamp-to-fixture bulk request mapping.", ""])
    return "\n".join(lines)


def write_artifacts(plan: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "nations_league_historical_odds_request_plan_20260929.json"
    markdown_path = output_dir / "nations_league_historical_odds_request_plan_20260929.md"
    json_path.write_bytes(canonical_json(plan) + b"\n")
    markdown_path.write_text(render_markdown(plan), encoding="utf-8")
    return json_path, markdown_path

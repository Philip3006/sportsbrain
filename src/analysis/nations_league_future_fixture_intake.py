"""Offline UEFA Nations League future-fixture intake.

This module contains only public schedule material and deterministic validation
for the frozen ``nations_league_v1`` forward-shadow runner.  It deliberately
does not call providers, read credentials, or produce predictions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

COMPETITION = "UEFA Nations League"
EDITION = "2026/27"
EVALUATION_BLOCK = "NL_2026_27"
STAGE = "league_phase"
SOURCE_TIMEZONE = "CET (UTC+01:00)"
OBSERVED_AT_UTC = "2026-09-30T15:31:41Z"
OFFICIAL_FIXTURES_URL = (
    "https://www.uefa.com/uefanationsleague/news/"
    "02a2-1fea18abbcbc-456e846509e7-1000--2026-27-uefa-nations-league-all-the-league-phase-fixtures/"
)
OFFICIAL_FIXTURE_PDF_URL = (
    "https://editorial.uefa.com/resources/02a4-207fdf4b084e-"
    "a8a8727f0805-1000/unl_2627_-_league_phase_fixture_list_per_matchday_28_april_2026.pdf"
)
UTC = timezone.utc
CET = timezone(timedelta(hours=1), name="CET")
STATUS_VALUES = frozenset(
    {"VERIFIED", "UNRESOLVED", "STARTED", "CANCELLED", "POSTPONED"}
)

# The official UEFA sources label all times CET and state that unmarked matches
# start at 20:45.  Every row below is a source transcription, not an inferred
# fixture or bookmaker/provider event.
_SCHEDULE_ROWS: tuple[tuple[str, str, str, str, str], ...] = (
    ("2026-10-01", "18:00", "D2", "Azerbaijan", "Liechtenstein"),
    ("2026-10-01", "20:45", "A2", "Germany", "Serbia"),
    ("2026-10-01", "20:45", "A2", "Greece", "Netherlands"),
    ("2026-10-01", "20:45", "A4", "Denmark", "Portugal"),
    ("2026-10-01", "20:45", "A4", "Wales", "Norway"),
    ("2026-10-01", "20:45", "B3", "Israel", "Kosovo"),
    ("2026-10-01", "20:45", "B3", "Republic of Ireland", "Austria"),
    ("2026-10-01", "20:45", "D1", "Malta", "Gibraltar"),
    ("2026-10-02", "16:00", "C3", "Kazakhstan", "Moldova"),
    ("2026-10-02", "18:00", "C2", "Cyprus", "Armenia"),
    ("2026-10-02", "18:00", "C2", "Latvia", "Montenegro"),
    ("2026-10-02", "20:45", "A1", "Belgium", "Türkiye"),
    ("2026-10-02", "20:45", "A1", "France", "Italy"),
    ("2026-10-02", "20:45", "B2", "Hungary", "Georgia"),
    ("2026-10-02", "20:45", "B2", "Ukraine", "Northern Ireland"),
    ("2026-10-02", "20:45", "B4", "Bosnia and Herzegovina", "Sweden"),
    ("2026-10-02", "20:45", "B4", "Poland", "Romania"),
    ("2026-10-02", "20:45", "C3", "Faroe Islands", "Slovakia"),
    ("2026-10-03", "15:00", "C1", "Finland", "Albania"),
    ("2026-10-03", "18:00", "A3", "Croatia", "England"),
    ("2026-10-03", "18:00", "C1", "Belarus", "San Marino"),
    ("2026-10-03", "18:00", "C4", "Estonia", "Luxembourg"),
    ("2026-10-03", "18:00", "C4", "Iceland", "Bulgaria"),
    ("2026-10-03", "20:45", "A3", "Spain", "Czechia"),
    ("2026-10-03", "20:45", "B1", "North Macedonia", "Scotland"),
    ("2026-10-03", "20:45", "B1", "Switzerland", "Slovenia"),
    ("2026-10-04", "15:00", "D2", "Azerbaijan", "Lithuania"),
    ("2026-10-04", "18:00", "B3", "Kosovo", "Austria"),
    ("2026-10-04", "18:00", "D1", "Malta", "Andorra"),
    ("2026-10-04", "20:45", "A2", "Greece", "Germany"),
    ("2026-10-04", "20:45", "A2", "Netherlands", "Serbia"),
    ("2026-10-04", "20:45", "A4", "Portugal", "Norway"),
    ("2026-10-04", "20:45", "A4", "Wales", "Denmark"),
    ("2026-10-04", "20:45", "B3", "Republic of Ireland", "Israel"),
    ("2026-10-05", "18:00", "C2", "Cyprus", "Latvia"),
    ("2026-10-05", "20:45", "A1", "France", "Belgium"),
    ("2026-10-05", "20:45", "A1", "Italy", "Türkiye"),
    ("2026-10-05", "20:45", "B2", "Northern Ireland", "Georgia"),
    ("2026-10-05", "20:45", "B2", "Ukraine", "Hungary"),
    ("2026-10-05", "20:45", "B4", "Bosnia and Herzegovina", "Poland"),
    ("2026-10-05", "20:45", "B4", "Romania", "Sweden"),
    ("2026-10-05", "20:45", "C2", "Montenegro", "Armenia"),
    ("2026-10-06", "16:00", "C3", "Kazakhstan", "Faroe Islands"),
    ("2026-10-06", "20:45", "A3", "Croatia", "Spain"),
    ("2026-10-06", "20:45", "A3", "England", "Czechia"),
    ("2026-10-06", "20:45", "B1", "Scotland", "Slovenia"),
    ("2026-10-06", "20:45", "B1", "Switzerland", "North Macedonia"),
    ("2026-10-06", "20:45", "C1", "Albania", "San Marino"),
    ("2026-10-06", "20:45", "C1", "Belarus", "Finland"),
    ("2026-10-06", "20:45", "C3", "Moldova", "Slovakia"),
    ("2026-10-06", "20:45", "C4", "Estonia", "Iceland"),
    ("2026-10-06", "20:45", "C4", "Luxembourg", "Bulgaria"),
    ("2026-11-12", "18:00", "A1", "Türkiye", "Belgium"),
    ("2026-11-12", "18:00", "C2", "Armenia", "Cyprus"),
    ("2026-11-12", "20:45", "A1", "Italy", "France"),
    ("2026-11-12", "20:45", "A3", "Czechia", "Spain"),
    ("2026-11-12", "20:45", "A3", "England", "Croatia"),
    ("2026-11-12", "20:45", "C1", "Albania", "Finland"),
    ("2026-11-12", "20:45", "C1", "San Marino", "Belarus"),
    ("2026-11-12", "20:45", "C2", "Montenegro", "Latvia"),
    ("2026-11-13", "18:00", "C3", "Moldova", "Kazakhstan"),
    ("2026-11-13", "20:45", "A2", "Netherlands", "Greece"),
    ("2026-11-13", "20:45", "A2", "Serbia", "Germany"),
    ("2026-11-13", "20:45", "B1", "Scotland", "North Macedonia"),
    ("2026-11-13", "20:45", "B1", "Slovenia", "Switzerland"),
    ("2026-11-13", "20:45", "C3", "Slovakia", "Faroe Islands"),
    ("2026-11-13", "20:45", "C4", "Bulgaria", "Iceland"),
    ("2026-11-13", "20:45", "C4", "Luxembourg", "Estonia"),
    ("2026-11-13", "20:45", "D1", "Andorra", "Gibraltar"),
    ("2026-11-13", "20:45", "D2", "Liechtenstein", "Azerbaijan"),
    ("2026-11-14", "15:00", "B3", "Kosovo", "Israel"),
    ("2026-11-14", "18:00", "A4", "Norway", "Wales"),
    ("2026-11-14", "18:00", "B2", "Georgia", "Hungary"),
    ("2026-11-14", "20:45", "A4", "Portugal", "Denmark"),
    ("2026-11-14", "20:45", "B2", "Northern Ireland", "Ukraine"),
    ("2026-11-14", "20:45", "B3", "Austria", "Republic of Ireland"),
    ("2026-11-14", "20:45", "B4", "Romania", "Poland"),
    ("2026-11-14", "20:45", "B4", "Sweden", "Bosnia and Herzegovina"),
    ("2026-11-15", "15:00", "C2", "Cyprus", "Montenegro"),
    ("2026-11-15", "15:00", "C2", "Latvia", "Armenia"),
    ("2026-11-15", "18:00", "C1", "Belarus", "Albania"),
    ("2026-11-15", "18:00", "C1", "Finland", "San Marino"),
    ("2026-11-15", "20:45", "A1", "Belgium", "Italy"),
    ("2026-11-15", "20:45", "A1", "France", "Türkiye"),
    ("2026-11-15", "20:45", "A3", "Croatia", "Czechia"),
    ("2026-11-15", "20:45", "A3", "Spain", "England"),
    ("2026-11-16", "16:00", "C3", "Faroe Islands", "Moldova"),
    ("2026-11-16", "16:00", "C3", "Kazakhstan", "Slovakia"),
    ("2026-11-16", "18:00", "C4", "Estonia", "Bulgaria"),
    ("2026-11-16", "18:00", "C4", "Iceland", "Luxembourg"),
    ("2026-11-16", "18:00", "D2", "Lithuania", "Liechtenstein"),
    ("2026-11-16", "20:45", "A2", "Germany", "Netherlands"),
    ("2026-11-16", "20:45", "A2", "Greece", "Serbia"),
    ("2026-11-16", "20:45", "B1", "North Macedonia", "Slovenia"),
    ("2026-11-16", "20:45", "B1", "Switzerland", "Scotland"),
    ("2026-11-16", "20:45", "D1", "Gibraltar", "Malta"),
    ("2026-11-17", "20:45", "A4", "Denmark", "Norway"),
    ("2026-11-17", "20:45", "A4", "Wales", "Portugal"),
    ("2026-11-17", "20:45", "B2", "Hungary", "Northern Ireland"),
    ("2026-11-17", "20:45", "B2", "Ukraine", "Georgia"),
    ("2026-11-17", "20:45", "B3", "Israel", "Austria"),
    ("2026-11-17", "20:45", "B3", "Republic of Ireland", "Kosovo"),
    ("2026-11-17", "20:45", "B4", "Bosnia and Herzegovina", "Romania"),
    ("2026-11-17", "20:45", "B4", "Poland", "Sweden"),
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def parse_utc(value: str, field: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must include UTC timezone")
    return parsed.astimezone(UTC)


def kickoff_to_utc(date_value: str, time_value: str) -> str:
    try:
        parsed = datetime.strptime(
            f"{date_value} {time_value}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=CET)
    except ValueError as exc:
        raise ValueError("source kickoff must be YYYY-MM-DD HH:MM") from exc
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_future_fixture_id(
    *,
    competition: str,
    edition: str,
    stage: str,
    group: str,
    home_team: str,
    away_team: str,
    kickoff_utc: str,
) -> str:
    """Return an identity derived only from canonical competition data."""
    payload = {
        "competition": competition,
        "edition": edition,
        "stage": stage,
        "group": group,
        "home_team": home_team,
        "away_team": away_team,
        "kickoff_utc": kickoff_utc,
    }
    return f"uefa-nl:future-{sha256_json(payload)[:24]}"


def _source_digest(record: dict[str, Any]) -> str:
    return sha256_json(
        {
            "sources": record["source_provenance_records"],
            "competition": record["competition"],
            "edition": record["edition"],
            "stage": record["stage"],
            "group": record["group"],
            "home_team": record["home_team"],
            "away_team": record["away_team"],
            "kickoff_utc": record["kickoff_utc"],
        }
    )


def _windows(kickoff_utc: str) -> dict[str, dict[str, str]]:
    kickoff = parse_utc(kickoff_utc, "kickoff_utc")
    return {
        "initial": {
            "start_utc": (kickoff - timedelta(hours=26))
            .isoformat()
            .replace("+00:00", "Z"),
            "end_utc": (kickoff - timedelta(hours=22))
            .isoformat()
            .replace("+00:00", "Z"),
        },
        "refinement": {
            "start_utc": (kickoff - timedelta(minutes=120))
            .isoformat()
            .replace("+00:00", "Z"),
            "end_utc": (kickoff - timedelta(minutes=60))
            .isoformat()
            .replace("+00:00", "Z"),
            "target_utc": (kickoff - timedelta(minutes=90))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    }


def _fixture_from_row(
    row: tuple[str, str, str, str, str], observed_at_utc: str
) -> dict[str, Any]:
    date_value, time_value, group, home_team, away_team = row
    kickoff_utc = kickoff_to_utc(date_value, time_value)
    source_provenance = [
        {
            "url": OFFICIAL_FIXTURES_URL,
            "kind": "official_uefa_fixture_article",
            "observed_at_utc": observed_at_utc,
            "source_timezone": SOURCE_TIMEZONE,
            "evidence": f"{date_value} {time_value} {group} {home_team} vs {away_team}",
        },
        {
            "url": OFFICIAL_FIXTURE_PDF_URL,
            "kind": "official_uefa_fixture_pdf",
            "observed_at_utc": observed_at_utc,
            "source_timezone": SOURCE_TIMEZONE,
            "evidence": f"{date_value} {time_value} {group} {home_team} vs {away_team}",
        },
    ]
    record: dict[str, Any] = {
        "fixture_id": canonical_future_fixture_id(
            competition=COMPETITION,
            edition=EDITION,
            stage=STAGE,
            group=group,
            home_team=home_team,
            away_team=away_team,
            kickoff_utc=kickoff_utc,
        ),
        "competition": COMPETITION,
        "edition": EDITION,
        "stage": STAGE,
        "league": group[0],
        "group": group,
        "home_team": home_team,
        "away_team": away_team,
        "kickoff_utc": kickoff_utc,
        "source_kickoff_timezone": SOURCE_TIMEZONE,
        "neutral_status": "NOT_STATED_BY_SOURCE",
        "source_provenance": "official UEFA fixture article and official UEFA fixture PDF",
        "source_provenance_records": source_provenance,
        "observed_at_utc": observed_at_utc,
        "evaluation_block": EVALUATION_BLOCK,
        "predictions": [],
    }
    record["source_digest"] = _source_digest(record)
    record["status"] = (
        "VERIFIED" if parse_utc(kickoff_utc) > parse_utc(observed_at_utc) else "STARTED"
    )
    record["capture_windows"] = _windows(kickoff_utc)
    return record


def build_manifest(observed_at_utc: str = OBSERVED_AT_UTC) -> dict[str, Any]:
    """Build the deterministic public-source manifest without network access."""
    parse_utc(observed_at_utc, "observed_at_utc")
    fixtures = [_fixture_from_row(row, observed_at_utc) for row in _SCHEDULE_ROWS]
    manifest: dict[str, Any] = {
        "schema": "nations-league-future-fixture-manifest-v1",
        "status": "OFFLINE_PUBLIC_SOURCE_INTAKE",
        "competition": COMPETITION,
        "edition": EDITION,
        "observed_at_utc": observed_at_utc,
        "source_timezone": SOURCE_TIMEZONE,
        "source_urls": [OFFICIAL_FIXTURES_URL, OFFICIAL_FIXTURE_PDF_URL],
        "forward_shadow_model": "nations_league_v1",
        "provider_ids_used": [],
        "predictions": [],
        "fixtures": fixtures,
    }
    validate_manifest(manifest)
    manifest["manifest_digest"] = sha256_json(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        raise TypeError("fixtures must be a list")
    seen: set[str] = set()
    observed_at = parse_utc(manifest["observed_at_utc"], "observed_at_utc")
    for fixture in fixtures:
        fixture_id = fixture.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise ValueError("fixture_id is required")
        if fixture_id in seen:
            raise ValueError(f"duplicate fixture identity: {fixture_id}")
        seen.add(fixture_id)
        status = fixture.get("status")
        if status not in STATUS_VALUES:
            raise ValueError(f"unsupported fixture status: {status}")
        if (
            not isinstance(fixture.get("source_provenance"), str)
            or not fixture["source_provenance"].strip()
        ):
            raise ValueError(f"source provenance required: {fixture_id}")
        sources = fixture.get("source_provenance_records")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"source provenance records required: {fixture_id}")
        if not all(
            isinstance(source.get("url"), str) and source.get("observed_at_utc")
            for source in sources
        ):
            raise ValueError(f"source provenance is incomplete: {fixture_id}")
        if fixture.get("predictions") != []:
            raise ValueError("future fixture manifest must not contain predictions")
        kickoff_value = fixture.get("kickoff_utc")
        if status == "UNRESOLVED":
            if kickoff_value is not None:
                raise ValueError("unresolved fixture cannot claim exact kickoff")
            continue
        if not isinstance(kickoff_value, str):
            raise TypeError(f"kickoff_utc is required for {status}: {fixture_id}")
        kickoff = parse_utc(kickoff_value, "kickoff_utc")
        if status == "VERIFIED" and kickoff <= observed_at:
            raise ValueError("started fixture cannot be VERIFIED")
        if status == "STARTED" and kickoff > observed_at:
            raise ValueError("future fixture cannot be STARTED")
        if status in {"VERIFIED", "STARTED"}:
            expected_id = canonical_future_fixture_id(
                competition=fixture["competition"],
                edition=fixture["edition"],
                stage=fixture["stage"],
                group=fixture["group"],
                home_team=fixture["home_team"],
                away_team=fixture["away_team"],
                kickoff_utc=kickoff_value,
            )
            if fixture_id != expected_id:
                raise ValueError(f"fixture identity mismatch: {fixture_id}")
            if not fixture.get("source_digest"):
                raise ValueError(f"source digest required: {fixture_id}")
            if status == "VERIFIED":
                windows = fixture.get("capture_windows")
                if windows != _windows(kickoff_value):
                    raise ValueError(f"capture windows mismatch: {fixture_id}")


def write_manifest(path: Path, manifest: dict[str, Any] | None = None) -> None:
    payload = manifest or build_manifest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-manifest", type=Path, required=True)
    args = parser.parse_args()
    write_manifest(args.write_manifest)


if __name__ == "__main__":
    _main()

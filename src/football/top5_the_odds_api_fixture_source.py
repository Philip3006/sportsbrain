"""Canonical current Top-5 fixture acquisition through The Odds API.

This is a thin B4 adapter over the existing ``src.data.odds_api`` path.  It
does not implement HTTP, credentials, retries, caching, or quota governance.
Those remain owned by the production The Odds API client and provider budget
guard.  The adapter only converts already-normalized provider event records to
canonical ``Fixture`` objects and hands them to the current target-manifest
selector.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.data.odds_api import fetch_upcoming_matches
from src.football.production_contracts import Fixture
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_event_discovery import (
    DISCOVERY_LEAGUE_ORDER,
    EventDiscoveryContractError,
    EventDiscoveryExecutionBlocked,
    Top5CurrentDiscoveryTargetManifestV1,
    select_current_top5_discovery_targets,
)

THE_ODDS_API_FIXTURE_SOURCE = (
    "the_odds_api:production:/v4/sports/{sport_key}/odds;markets=h2h;regions=eu"
)
THE_ODDS_API_FIXTURE_MARKETS = "h2h"
THE_ODDS_API_FIXTURE_REGIONS = "eu"
TOP5_SPORT_KEYS = {
    "EPL": "soccer_epl",
    "BL1": "soccer_germany_bundesliga",
    "LL": "soccer_spain_la_liga",
    "SA": "soccer_italy_serie_a",
    "L1": "soccer_france_ligue_1",
}
TOP5_FIXTURE_SOURCE_SCHEMA_VERSION = "top5-the-odds-api-fixture-acquisition-v1"


class TheOddsApiFixtureSourceError(EventDiscoveryContractError):
    """Fail-closed error for malformed or unusable fixture-source data."""


def _utc_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TheOddsApiFixtureSourceError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise TheOddsApiFixtureSourceError(f"{field_name} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TheOddsApiFixtureSourceError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def fixture_from_the_odds_api_record(
    record: Mapping[str, object], *, league: str
) -> Fixture:
    """Convert one existing The Odds API event record to canonical Fixture."""

    if league not in DISCOVERY_LEAGUE_ORDER:
        raise TheOddsApiFixtureSourceError("fixture league is not a Top-5 league")
    if not isinstance(record, Mapping):
        raise TheOddsApiFixtureSourceError("The Odds API fixture record is invalid")
    expected_sport_key = TOP5_SPORT_KEYS[league]
    supplied_sport_key = record.get("sport_key")
    if supplied_sport_key is not None and supplied_sport_key != expected_sport_key:
        raise TheOddsApiFixtureSourceError(
            "The Odds API sport key does not match league"
        )
    home = record.get("home_team")
    away = record.get("away_team")
    if not isinstance(home, str) or not home.strip():
        raise TheOddsApiFixtureSourceError("The Odds API home team is missing")
    if not isinstance(away, str) or not away.strip():
        raise TheOddsApiFixtureSourceError("The Odds API away team is missing")
    kickoff = _utc_timestamp(record.get("commence_time"), "commence_time")
    fixture_key = make_fixture_key(league, home, away, kickoff)
    fixture = Fixture(fixture_key, league, home.strip(), away.strip(), kickoff)
    fixture.validate()
    return fixture


@dataclass(frozen=True)
class TheOddsApiCurrentFixtureAcquisitionV1:
    """Bounded current fixture batch captured through the existing path."""

    fixtures: tuple[Fixture, ...]
    observed_at: datetime
    source_provenance: str
    source_release_sha: str
    runtime_data_sha: str | None
    request_count: int

    def validate(self) -> None:
        if self.source_provenance != THE_ODDS_API_FIXTURE_SOURCE:
            raise TheOddsApiFixtureSourceError("fixture source identity is invalid")
        if self.request_count != len(DISCOVERY_LEAGUE_ORDER):
            raise EventDiscoveryExecutionBlocked(
                "fixture acquisition request scope must be exactly five leagues"
            )
        if len(self.fixtures) == 0:
            raise EventDiscoveryExecutionBlocked(
                "fixture acquisition returned no fixtures"
            )
        parsed_observed_at = _utc_timestamp(
            self.observed_at.isoformat(), "fixture source observed_at"
        )
        if parsed_observed_at > datetime.now(timezone.utc):
            raise EventDiscoveryExecutionBlocked(
                "fixture source observed_at is future-dated"
            )

    def build_manifest(self, *, now: datetime) -> Top5CurrentDiscoveryTargetManifestV1:
        self.validate()
        manifest = select_current_top5_discovery_targets(
            self.fixtures,
            now=now,
            observed_at=self.observed_at,
            source_provenance=self.source_provenance,
            source_release_sha=self.source_release_sha,
            runtime_data_sha=self.runtime_data_sha,
        )
        manifest.validate(now=now)
        return manifest


def _source_release_binding() -> tuple[str, str | None]:
    metadata_path = (
        Path(__file__).resolve().parents[2] / "docs" / "data" / "provenance_meta.json"
    )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EventDiscoveryExecutionBlocked(
            "The Odds API fixture source provenance is unavailable"
        ) from exc
    source_release_sha = metadata.get("source_release_sha")
    if not isinstance(source_release_sha, str) or not source_release_sha.strip():
        raise EventDiscoveryExecutionBlocked(
            "The Odds API fixture source release binding is missing"
        )
    source_ci = metadata.get("source_ci")
    if not isinstance(source_ci, Mapping) or source_ci.get("status") != "success":
        raise EventDiscoveryExecutionBlocked(
            "The Odds API fixture source release CI binding is invalid"
        )
    if source_ci.get("head_sha") != source_release_sha:
        raise EventDiscoveryExecutionBlocked(
            "The Odds API fixture source release CI binding mismatches"
        )
    runtime_data_sha = metadata.get("runtime_data_sha")
    if runtime_data_sha is not None and not isinstance(runtime_data_sha, str):
        raise EventDiscoveryExecutionBlocked("runtime data binding is invalid")
    return (
        source_release_sha.lower(),
        runtime_data_sha.lower() if runtime_data_sha else None,
    )


def fetch_current_top5_fixture_acquisition(
    *,
    api_key: str | None = None,
    allow_quota_revalidation: bool = False,
) -> TheOddsApiCurrentFixtureAcquisitionV1:
    """Acquire exactly one bounded current fixture batch per Top-5 league.

    The call delegates every network, credential, retry, cache, and budget
    decision to ``src.data.odds_api.fetch_upcoming_matches``.  A failing league
    aborts the batch; no partial manifest is returned.
    """

    source_release_sha, runtime_data_sha = _source_release_binding()
    fixtures: list[Fixture] = []
    for league in DISCOVERY_LEAGUE_ORDER:
        records = fetch_upcoming_matches(
            sport=TOP5_SPORT_KEYS[league],
            regions=THE_ODDS_API_FIXTURE_REGIONS,
            markets=THE_ODDS_API_FIXTURE_MARKETS,
            api_key=api_key,
            force=True,
            allow_quota_revalidation=allow_quota_revalidation,
        )
        if not isinstance(records, list):
            raise TheOddsApiFixtureSourceError(
                f"The Odds API returned an invalid {league} fixture collection"
            )
        fixtures.extend(
            fixture_from_the_odds_api_record(record, league=league)
            for record in records
        )
    acquisition = TheOddsApiCurrentFixtureAcquisitionV1(
        fixtures=tuple(fixtures),
        observed_at=datetime.now(timezone.utc),
        source_provenance=THE_ODDS_API_FIXTURE_SOURCE,
        source_release_sha=source_release_sha,
        runtime_data_sha=runtime_data_sha,
        request_count=len(DISCOVERY_LEAGUE_ORDER),
    )
    acquisition.validate()
    return acquisition


__all__ = [
    "THE_ODDS_API_FIXTURE_MARKETS",
    "THE_ODDS_API_FIXTURE_REGIONS",
    "THE_ODDS_API_FIXTURE_SOURCE",
    "TOP5_FIXTURE_SOURCE_SCHEMA_VERSION",
    "TOP5_SPORT_KEYS",
    "TheOddsApiCurrentFixtureAcquisitionV1",
    "TheOddsApiFixtureSourceError",
    "fetch_current_top5_fixture_acquisition",
    "fixture_from_the_odds_api_record",
]

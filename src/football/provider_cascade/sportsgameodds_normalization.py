"""Pure SportsGameOdds event validation and normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import isfinite

from src.football.production_contracts import Fixture
from src.football.provider_cascade.adapters import (
    RawProviderResponse,
    _observation,
    _parse_kickoff,
    _parse_timestamp,
    _team_key,
)
from src.football.provider_cascade.contracts import (
    NormalizedOddsObservation,
    ProviderConfig,
    ProviderState,
)
from src.football.provider_cascade.sportsgameodds import (
    SPORTSGAMEODDS_PROVIDER,
    SPORTSGAMEODDS_REGULATION_MARKET_IDS,
    SportsGameOddsError,
    SportsGameOddsExperimentConfig,
)


def _name(team: object) -> str:
    if not isinstance(team, Mapping):
        return ""
    names = team.get("names")
    if isinstance(names, Mapping):
        for key in ("long", "medium", "short"):
            value = names.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("name", "teamName"):
        value = team.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _bookmaker_rows(value: object) -> tuple[tuple[str, Mapping[str, object]], ...]:
    if isinstance(value, Mapping):
        rows: list[tuple[str, Mapping[str, object]]] = []
        for key, raw in value.items():
            if not isinstance(raw, Mapping):
                continue
            bookmaker_id = str(raw.get("bookmakerID") or key).strip()
            if bookmaker_id:
                rows.append((bookmaker_id, raw))
        return tuple(rows)
    if isinstance(value, list):
        rows = []
        for raw in value:
            if not isinstance(raw, Mapping):
                continue
            bookmaker_id = str(raw.get("bookmakerID") or "").strip()
            if bookmaker_id:
                rows.append((bookmaker_id, raw))
        return tuple(rows)
    return ()


def _american_or_decimal(value: object) -> float | None:
    """Parse the provider's American examples and already-decimal values."""

    if isinstance(value, bool) or value is None:
        return None
    raw = str(value).strip().replace(" ", "")
    if not raw:
        return None
    try:
        if raw.startswith(("+", "-")):
            american = float(raw)
            if not isfinite(american) or american == 0:
                return None
            decimal = 1.0 + (
                american / 100.0 if american > 0 else 100.0 / abs(american)
            )
        else:
            decimal = float(raw)
    except (TypeError, ValueError):
        return None
    return decimal if isfinite(decimal) and decimal > 1.0 else None


def _event_status(event: Mapping[str, object]) -> Mapping[str, object]:
    status = event.get("status")
    return status if isinstance(status, Mapping) else {}


def _event_kickoff(event: Mapping[str, object]) -> datetime | None:
    status = _event_status(event)
    return _parse_kickoff(
        status.get("startsAt") or event.get("startsAt") or event.get("kickoff")
    )


def _event_identity(
    fixture: Fixture,
    event: Mapping[str, object],
    aliases: Mapping[str, str],
    expected_league: str,
    kickoff_tolerance_seconds: int,
) -> None:
    if str(event.get("sportID", "")).strip().upper() != "SOCCER":
        raise SportsGameOddsError("wrong_sport", ProviderState.UNSUPPORTED_LEAGUE)
    if str(event.get("leagueID", "")).strip() != expected_league:
        raise SportsGameOddsError("wrong_league", ProviderState.UNSUPPORTED_LEAGUE)
    if str(event.get("type", "")).strip().lower() != "match":
        raise SportsGameOddsError(
            "unsupported_event_type", ProviderState.UNSUPPORTED_FIXTURE
        )
    teams = event.get("teams")
    if not isinstance(teams, Mapping):
        raise SportsGameOddsError("missing_teams", ProviderState.MALFORMED)
    home = teams.get("home")
    away = teams.get("away")
    home_name = _name(home)
    away_name = _name(away)
    expected_home = _team_key(fixture.home_team, aliases)
    expected_away = _team_key(fixture.away_team, aliases)
    if not home_name or not away_name:
        raise SportsGameOddsError("missing_team_name", ProviderState.MALFORMED)
    observed_home = _team_key(home_name, aliases)
    observed_away = _team_key(away_name, aliases)
    if (observed_home, observed_away) == (expected_away, expected_home):
        raise SportsGameOddsError("swapped_home_away", ProviderState.QUALITY_REJECTED)
    if (observed_home, observed_away) != (expected_home, expected_away):
        raise SportsGameOddsError("fixture_mismatch", ProviderState.UNSUPPORTED_FIXTURE)
    status = _event_status(event)
    if not status:
        raise SportsGameOddsError("missing_event_status", ProviderState.MALFORMED)
    kickoff = _event_kickoff(event)
    if kickoff is None:
        raise SportsGameOddsError("missing_kickoff", ProviderState.MALFORMED)
    if abs((kickoff - fixture.kickoff).total_seconds()) > kickoff_tolerance_seconds:
        raise SportsGameOddsError("kickoff_mismatch", ProviderState.UNSUPPORTED_FIXTURE)
    if status.get("cancelled") is True:
        raise SportsGameOddsError(
            "cancelled_fixture", ProviderState.UNSUPPORTED_FIXTURE
        )
    if (
        status.get("cancelled") is not False
        or status.get("live") is not False
        or status.get("started") is not False
    ):
        raise SportsGameOddsError("not_prematch", ProviderState.UNSUPPORTED_MARKET)


def _relevant_odds(event: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    odds = event.get("odds")
    if not isinstance(odds, Mapping):
        raise SportsGameOddsError("missing_odds", ProviderState.UNSUPPORTED_MARKET)
    result: dict[str, Mapping[str, object]] = {}
    full_game_seen = False
    for market_id in SPORTSGAMEODDS_REGULATION_MARKET_IDS:
        raw = odds.get(market_id)
        if isinstance(raw, Mapping):
            result[market_id] = raw
    for raw in odds.values():
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("periodID", "")).strip().lower() == "game":
            full_game_seen = True
    if len(result) != len(SPORTSGAMEODDS_REGULATION_MARKET_IDS):
        if full_game_seen and not result:
            raise SportsGameOddsError(
                "full_game_market_not_regulation", ProviderState.UNSUPPORTED_MARKET
            )
        raise SportsGameOddsError(
            "missing_regulation_1x2", ProviderState.UNSUPPORTED_MARKET
        )
    for raw in result.values():
        if (
            str(raw.get("periodID", "")).strip().lower() != "reg"
            or str(raw.get("betTypeID", "")).strip().lower() != "ml3way"
        ):
            raise SportsGameOddsError(
                "unsupported_1x2_semantics", ProviderState.UNSUPPORTED_MARKET
            )
    return result


def observations_for_event(
    fixture: Fixture,
    event: Mapping[str, object],
    *,
    config: ProviderConfig,
    experiment: SportsGameOddsExperimentConfig,
    request_identity: str,
    requested_at: datetime,
    provider_priority: int,
    captured_at: datetime,
    aliases: Mapping[str, str],
    kickoff_tolerance_seconds: int,
    evidence_kind: str,
    raw_response: RawProviderResponse,
) -> tuple[NormalizedOddsObservation, ...]:
    experiment.validate()
    fixture.validate()
    _event_identity(
        fixture,
        event,
        aliases,
        experiment.league_id,
        kickoff_tolerance_seconds,
    )
    provider_fixture_id = str(event.get("eventID", "")).strip()
    if not provider_fixture_id:
        raise SportsGameOddsError("missing_event_id", ProviderState.MALFORMED)
    markets = _relevant_odds(event)
    expected_side = {
        SPORTSGAMEODDS_REGULATION_MARKET_IDS[0]: "home",
        SPORTSGAMEODDS_REGULATION_MARKET_IDS[1]: "draw",
        SPORTSGAMEODDS_REGULATION_MARKET_IDS[2]: "away",
    }
    by_bookmaker: dict[str, dict[str, Mapping[str, object]]] = {}
    for market_id, market in markets.items():
        side = expected_side[market_id]
        rows = _bookmaker_rows(market.get("byBookmaker"))
        seen: set[str] = set()
        for bookmaker_id, row in rows:
            if bookmaker_id in seen:
                raise SportsGameOddsError(
                    "duplicate_bookmaker_outcome", ProviderState.QUALITY_REJECTED
                )
            seen.add(bookmaker_id)
            by_bookmaker.setdefault(bookmaker_id, {})[side] = row
    if not by_bookmaker:
        raise SportsGameOddsError(
            "missing_bookmaker_odds", ProviderState.UNSUPPORTED_MARKET
        )

    valid: list[NormalizedOddsObservation] = []
    stale_seen = False
    malformed_seen = False
    for bookmaker_id in sorted(by_bookmaker):
        rows = by_bookmaker[bookmaker_id]
        if set(rows) != {"home", "draw", "away"}:
            if set(rows) >= {"home", "away"}:
                malformed_seen = True
            continue
        if any(row.get("available") is not True for row in rows.values()):
            malformed_seen = True
            continue
        odds: dict[str, float] = {}
        timestamps: dict[str, datetime] = {}
        for side, row in rows.items():
            price = _american_or_decimal(row.get("odds"))
            timestamp = _parse_timestamp(row.get("lastUpdatedAt"), now=captured_at)
            if price is None:
                malformed_seen = True
                break
            if timestamp is None:
                malformed_seen = True
                break
            odds[side] = price
            timestamps[side] = timestamp
        if set(odds) != {"home", "draw", "away"}:
            continue
        source_timestamp = min(timestamps.values())
        age = (captured_at - source_timestamp).total_seconds()
        if age < 0 or age > experiment.maximum_odds_age_seconds:
            stale_seen = True
            continue
        teams = event.get("teams")
        team_ids = {
            side: teams.get(side, {}).get("teamID", "")
            for side in ("home", "away")
            if isinstance(teams, Mapping) and isinstance(teams.get(side), Mapping)
        }
        metadata = {
            "evidence_kind": evidence_kind,
            "provider_league_id": experiment.league_id,
            "market_ids": list(SPORTSGAMEODDS_REGULATION_MARKET_IDS),
            "regulation_semantics": "90_minutes_plus_stoppage_time",
            "full_game_market_semantics": "not_used; game includes extra time and shootout where applicable",
            "team_ids": team_ids,
            "bookmaker_count": len(by_bookmaker),
            "available_bookmakers": sorted(by_bookmaker),
            "source_age_seconds": age,
        }
        valid.append(
            _observation(
                fixture,
                provider_fixture_id=provider_fixture_id,
                provider_identity=SPORTSGAMEODDS_PROVIDER,
                bookmaker_identity=bookmaker_id,
                odds=odds,
                source_timestamp=source_timestamp,
                captured_at=captured_at,
                request_identity=request_identity,
                requested_at=requested_at,
                provider_priority=provider_priority,
                config=config,
                response=raw_response,
                source_provenance=(
                    "sportsgameodds:v2:/events; byBookmaker.lastUpdatedAt; "
                    "points-{home,all,away}-reg-ml3way"
                ),
                record=event,
                metadata=metadata,
            )
        )
    if valid:
        return tuple(valid)
    if stale_seen:
        raise SportsGameOddsError("stale_observation", ProviderState.STALE)
    if malformed_seen:
        raise SportsGameOddsError(
            "malformed_or_incomplete_1x2", ProviderState.QUALITY_REJECTED
        )
    raise SportsGameOddsError("missing_draw_outcome", ProviderState.QUALITY_REJECTED)


__all__ = ["_bookmaker_rows", "_relevant_odds", "observations_for_event"]

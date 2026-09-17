"""Bounded, redacted TheRundown account/coverage diagnostic.

This is intentionally separate from the active provider cascade.  It makes no
requests unless THERUNDOWN_API_KEY is already present in the process
environment, never prints response bodies or credentials, and makes at most
five sequential UCL requests or fifteen sequential Top-5 requests without
retries.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

import requests

BASE_URL = "https://therundown.io/api/v2"
SPORT_ID = 16
DEFAULT_MAX_REQUESTS = 5
MIN_REQUEST_INTERVAL_SECONDS = 1.30
TOP5_MAX_REQUESTS = 15
TOP5_TARGETS = {
    "EPL": frozenset({"epl", "englishpremierleague", "premierleague"}),
    "Bundesliga": frozenset({"bundesliga", "germanbundesliga", "bundesliga1", "ger1"}),
    "La Liga": frozenset({"laliga", "spanishlaliga", "esp1"}),
    "Serie A": frozenset({"seriea", "italianseriea", "ita1"}),
    "Ligue 1": frozenset({"ligue1", "frenchligue1", "fra1"}),
}


def _headers(response: requests.Response) -> dict[str, str]:
    return {
        str(key).casefold(): str(value).strip()
        for key, value in response.headers.items()
    }


def _integer(headers: dict[str, str], *names: str) -> int | None:
    for name in names:
        raw = headers.get(name.casefold())
        if raw is None:
            continue
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _quota(response: requests.Response) -> dict[str, object]:
    headers = _headers(response)
    return {
        "datapoints": _integer(headers, "x-datapoints"),
        "datapoints_used": _integer(headers, "x-datapoints-used"),
        "datapoints_remaining": _integer(headers, "x-datapoints-remaining"),
        "datapoints_limit": _integer(headers, "x-datapoints-limit"),
        "rate_limit": _integer(headers, "x-rate-limit", "x-ratelimit-limit"),
        "rate_remaining": _integer(
            headers, "x-rate-limit-remaining", "x-ratelimit-remaining"
        ),
        "tier": headers.get("x-tier"),
        "delay_seconds": _integer(headers, "x-data-delay-seconds"),
        "bookmakers": headers.get("x-bookmakers"),
        "history_access": headers.get("x-history-access"),
        "live_odds_access": headers.get("x-live-odds-access"),
        "websocket_access": headers.get("x-websocket-access"),
    }


def _summary(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {"payload": "non_object"}
    summary: dict[str, object] = {}
    for key in ("sports", "affiliates", "dates", "events", "markets"):
        value = payload.get(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    if isinstance(payload.get("dates"), list):
        summary["first_date_present"] = bool(payload["dates"])
    return summary


def _list_value(payload: object, key: str) -> list[object]:
    if isinstance(payload, Mapping):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        value = payload.get("data")
        if isinstance(value, list):
            return value
    if isinstance(payload, list):
        return payload
    return []


def _sport_catalog(payload: object) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    for entry in _list_value(payload, "sports"):
        if not isinstance(entry, Mapping):
            continue
        sport_id = entry.get("sport_id", entry.get("id", entry.get("sportID")))
        name = entry.get("name", entry.get("sport_name", entry.get("league_name")))
        abbreviation = entry.get(
            "abbreviation", entry.get("abbr", entry.get("short_name", ""))
        )
        if sport_id is None or not name:
            continue
        catalog.append(
            {
                "id": str(sport_id).strip(),
                "name": str(name).strip(),
                "abbreviation": str(abbreviation).strip(),
            }
        )
    return catalog


def _verified_league_ids(payload: object) -> dict[str, dict[str, object]]:
    catalog = _sport_catalog(payload)
    verified: dict[str, dict[str, object]] = {}
    for target, aliases in TOP5_TARGETS.items():
        matches = [
            item
            for item in catalog
            if _compact(item["name"]) in aliases
            or _compact(item["abbreviation"]) in aliases
        ]
        if len(matches) == 1:
            verified[target] = matches[0]
    return verified


def _compact(value: object) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(char) and char.isalnum()
    )


def _affiliate_names(payload: object) -> dict[str, str]:
    """Extract affiliate identity without retaining the response body."""

    entries: object = payload
    if isinstance(payload, Mapping):
        entries = payload.get("affiliates", payload.get("data", ()))
    if not isinstance(entries, list):
        return {}
    names: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        affiliate_id = entry.get(
            "affiliate_id", entry.get("id", entry.get("source_id"))
        )
        name = entry.get("name", entry.get("affiliate_name", entry.get("display_name")))
        if affiliate_id is not None and name:
            names[str(affiliate_id).strip()] = str(name).strip()
    return names


def _event_teams(event: Mapping[str, object]) -> tuple[str, str, str, str]:
    teams = event.get("teams")
    if not isinstance(teams, list):
        return "", "", "", ""
    objects = tuple(team for team in teams if isinstance(team, Mapping))
    home = next((team for team in objects if team.get("is_home") is True), None)
    away = next((team for team in objects if team.get("is_away") is True), None)
    if home is None or away is None:
        if len(objects) == 2:
            away, home = objects
        else:
            return "", "", "", ""
    return (
        str(home.get("name", "")).strip(),
        str(away.get("name", "")).strip(),
        str(home.get("team_id", "")).strip(),
        str(away.get("team_id", "")).strip(),
    )


def _outcome_key(
    participant: Mapping[str, object],
    *,
    home_name: str,
    away_name: str,
    home_id: str,
    away_id: str,
) -> str:
    participant_id = str(participant.get("id", "")).strip()
    label = _compact(participant.get("name"))
    if (
        participant.get("is_home") is True
        or participant_id == home_id
        or label == _compact(home_name)
    ):
        return "home"
    if (
        participant.get("is_away") is True
        or participant_id == away_id
        or label == _compact(away_name)
    ):
        return "away"
    if label in {"draw", "tie", "x"}:
        return "draw"
    return ""


def _event_details(
    payload: object, affiliate_names: Mapping[str, str]
) -> list[dict[str, object]]:
    """Return redacted event-level fixture, market, and book evidence."""

    if not isinstance(payload, Mapping) or not isinstance(payload.get("events"), list):
        return []
    details: list[dict[str, object]] = []
    for event in payload["events"]:
        if not isinstance(event, Mapping):
            continue
        home_name, away_name, home_id, away_id = _event_teams(event)
        schedule = event.get("schedule")
        schedule = schedule if isinstance(schedule, Mapping) else {}
        markets: list[dict[str, object]] = []
        raw_markets = event.get("markets")
        if isinstance(raw_markets, list):
            for market in raw_markets:
                if not isinstance(market, Mapping):
                    continue
                market_id = str(market.get("market_id", "")).strip()
                market_detail: dict[str, object] = {
                    "market_id": market_id,
                    "period_id": market.get("period_id"),
                    "name": market.get("name"),
                    "participant_count": len(market.get("participants", ()))
                    if isinstance(market.get("participants"), list)
                    else 0,
                }
                if market_id != "1":
                    markets.append(market_detail)
                    continue
                books: dict[str, dict[str, list[str]]] = {}
                participants = market.get("participants")
                if isinstance(participants, list):
                    for participant in participants:
                        if not isinstance(participant, Mapping):
                            continue
                        outcome = _outcome_key(
                            participant,
                            home_name=home_name,
                            away_name=away_name,
                            home_id=home_id,
                            away_id=away_id,
                        )
                        if not outcome:
                            continue
                        lines = participant.get("lines")
                        if not isinstance(lines, list):
                            continue
                        for line in lines:
                            if not isinstance(line, Mapping) or line.get(
                                "value"
                            ) not in ("", None):
                                continue
                            prices = line.get("prices")
                            if not isinstance(prices, Mapping):
                                continue
                            for affiliate_raw, price in prices.items():
                                if (
                                    not isinstance(price, Mapping)
                                    or price.get("is_main_line") is not True
                                ):
                                    continue
                                affiliate_id = str(affiliate_raw).strip()
                                bucket = books.setdefault(affiliate_id, {})
                                timestamps = bucket.setdefault("timestamps", [])
                                updated_at = price.get("updated_at")
                                if updated_at is not None:
                                    timestamps.append(str(updated_at))
                                bucket.setdefault(outcome, [])
                observed_ids = sorted(books)
                complete_ids = sorted(
                    affiliate_id
                    for affiliate_id, outcomes in books.items()
                    if {key for key in outcomes if key in {"home", "draw", "away"}}
                    == {"home", "draw", "away"}
                )
                market_detail.update(
                    {
                        "bookmakers": [
                            {
                                "affiliate_id": affiliate_id,
                                "name": affiliate_names.get(affiliate_id),
                            }
                            for affiliate_id in observed_ids
                        ],
                        "complete_1x2_bookmakers": [
                            {
                                "affiliate_id": affiliate_id,
                                "name": affiliate_names.get(affiliate_id),
                            }
                            for affiliate_id in complete_ids
                        ],
                        "complete_1x2_count": len(complete_ids),
                        "source_update_timestamps": sorted(
                            {
                                timestamp
                                for outcomes in books.values()
                                for timestamp in outcomes.get("timestamps", [])
                            }
                        ),
                    }
                )
                markets.append(market_detail)
        details.append(
            {
                "event_id": str(event.get("event_id", "")).strip(),
                "event_date": event.get("event_date"),
                "fixture": {"home": home_name, "away": away_name},
                "sport_id": event.get("sport_id"),
                "league_name": schedule.get("league_name"),
                "season_year": schedule.get("season_year"),
                "markets": markets,
            }
        )
    return details


def _request(
    session: requests.Session,
    key: str,
    path: str,
    *,
    params: dict[str, str] | None,
    timeout: float,
) -> tuple[dict[str, object], object | None]:
    try:
        url = path if path.startswith("https://") else f"https://therundown.io{path}"
        response = session.get(
            url,
            params=params,
            headers={"Accept": "application/json", "X-TheRundown-Key": key},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return {"path": path, "error": type(exc).__name__}, None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    quota = _quota(response)
    return {
        "path": urlsplit(response.url).path,
        "status": response.status_code,
        "response_bytes": len(response.content),
        "quota": quota,
        "summary": _summary(payload),
    }, payload


def _date_from_payload(payload: object, fallback: str) -> str:
    valid_dates: list[str] = []
    if isinstance(payload, dict) and isinstance(payload.get("dates"), list):
        for value in payload["dates"]:
            text = str(value).strip()
            try:
                valid_dates.append(
                    datetime.fromisoformat(text.replace("Z", "+00:00"))
                    .date()
                    .isoformat()
                )
            except ValueError:
                continue
    if not valid_dates:
        return fallback
    upcoming = [value for value in valid_dates if value >= fallback]
    return min(upcoming) if upcoming else max(valid_dates)


def _account_snapshot(outputs: list[dict[str, object]]) -> dict[str, object]:
    for output in reversed(outputs):
        quota = output.get("quota")
        if isinstance(quota, dict) and any(
            value is not None for value in quota.values()
        ):
            return quota
    return {}


def _request_cost(output: Mapping[str, object]) -> int | None:
    quota = output.get("quota")
    if not isinstance(quota, Mapping):
        return None
    value = quota.get("datapoints")
    return value if isinstance(value, int) else None


def _league_result(
    target: str,
    league: Mapping[str, object] | None,
    date_output: Mapping[str, object] | None,
    event_output: Mapping[str, object] | None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "league": target,
        "provider_league_id": league.get("id") if league else None,
        "provider_league_name": league.get("name") if league else None,
        "provider_league_abbreviation": league.get("abbreviation") if league else None,
        "access": "UNOBSERVED",
        "real_fixture": None,
        "complete_1x2": "UNOBSERVED",
        "complete_1x2_count": None,
        "complete_1x2_books": [],
        "books": [],
        "freshness": None,
        "cost": {
            "dates_datapoints": _request_cost(date_output or {}),
            "events_datapoints": _request_cost(event_output or {}),
        },
        "result": "UNOBSERVED",
    }
    if league is None or date_output is None:
        return result
    date_status = date_output.get("status")
    if date_status in {401, 403}:
        result["access"] = "NO"
        result["result"] = "FAIL"
        return result
    if date_status != 200:
        return result
    result["access"] = "YES"
    if event_output is None:
        return result
    event_status = event_output.get("status")
    if event_status in {401, 403}:
        result["access"] = "RESTRICTED"
        result["result"] = "FAIL"
        return result
    if event_status != 200:
        return result
    events = event_output.get("events")
    if not isinstance(events, list) or not events:
        result["complete_1x2"] = "NO"
        result["result"] = "PARTIAL"
        return result
    sample = next(
        (
            event
            for event in events
            if isinstance(event, Mapping)
            and any(
                isinstance(market, Mapping) and market.get("market_id") == "1"
                for market in event.get("markets", ())
                if isinstance(event.get("markets"), list)
            )
        ),
        events[0],
    )
    if not isinstance(sample, Mapping):
        result["result"] = "PARTIAL"
        return result
    result["real_fixture"] = {
        "event_id": sample.get("event_id"),
        "event_date": sample.get("event_date"),
        "fixture": sample.get("fixture"),
        "sport_id": sample.get("sport_id"),
        "league_name": sample.get("league_name"),
    }
    markets = sample.get("markets")
    moneyline = (
        next(
            (
                market
                for market in markets
                if isinstance(market, Mapping) and market.get("market_id") == "1"
            ),
            None,
        )
        if isinstance(markets, list)
        else None
    )
    if not isinstance(moneyline, Mapping):
        result["complete_1x2"] = "NO"
        result["result"] = "PARTIAL"
        return result
    result["books"] = moneyline.get("bookmakers", [])
    complete = moneyline.get("complete_1x2_bookmakers", [])
    result["complete_1x2_count"] = len(complete) if isinstance(complete, list) else None
    result["complete_1x2_books"] = complete if isinstance(complete, list) else []
    result["freshness"] = {
        "source_update_timestamps": moneyline.get("source_update_timestamps", []),
        "delay_seconds": _account_snapshot([event_output]).get("delay_seconds"),
    }
    result["complete_1x2"] = "YES" if complete else "NO"
    result["result"] = "PASS_EVIDENCE" if complete else "PARTIAL"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("ucl", "top5"), default="top5")
    parser.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--max-requests", type=int, default=TOP5_MAX_REQUESTS)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    request_limit = TOP5_MAX_REQUESTS if args.scope == "top5" else DEFAULT_MAX_REQUESTS
    if not 0 <= args.max_requests <= request_limit:
        parser.error(f"--max-requests must be between 0 and {request_limit}")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        requested_date = date.fromisoformat(args.date).isoformat()
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")

    key = os.getenv("THERUNDOWN_API_KEY", "")
    if not key.strip():
        print(
            json.dumps(
                {
                    "status": "REAL_TEST_READY",
                    "scope": args.scope,
                    "real_requests": 0,
                    "required_environment": "THERUNDOWN_API_KEY",
                    "command": "THERUNDOWN_API_KEY=... python3 scripts/therundown_diagnostic.py --scope top5 --max-requests 15",
                },
                sort_keys=True,
            )
        )
        return 0

    session = requests.Session()
    outputs: list[dict[str, object]] = []
    last_request_at: float | None = None
    task_datapoints_before: int | None = None
    task_datapoints_after: int | None = None
    per_request_datapoints = 0
    consecutive_429 = 0
    stop_reason: str | None = None

    def issue(
        label: str, path: str, params: dict[str, str] | None
    ) -> tuple[dict[str, object], object | None]:
        nonlocal last_request_at
        nonlocal task_datapoints_before, task_datapoints_after
        nonlocal per_request_datapoints, consecutive_429, stop_reason
        if last_request_at is not None:
            time.sleep(
                max(
                    0.0,
                    MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - last_request_at),
                )
            )
        output, payload = _request(
            session, key, path, params=params, timeout=args.timeout
        )
        last_request_at = time.monotonic()
        output["request_label"] = label
        outputs.append(output)
        quota = output.get("quota")
        if isinstance(quota, Mapping):
            request_cost = quota.get("datapoints")
            if isinstance(request_cost, int):
                per_request_datapoints += request_cost
            used = quota.get("datapoints_used")
            if isinstance(used, int):
                if task_datapoints_before is None:
                    task_datapoints_before = used
                task_datapoints_after = used
        if output.get("status") == 429:
            consecutive_429 += 1
        else:
            consecutive_429 = 0
        if consecutive_429 >= 2:
            stop_reason = "repeated_429"
        if (
            task_datapoints_before is not None
            and task_datapoints_after is not None
            and task_datapoints_after - task_datapoints_before > 1500
        ) or per_request_datapoints > 1500:
            stop_reason = "datapoint_budget_exceeded"
        return output, payload

    catalog_payload: object | None = None
    affiliate_payload: object | None = None
    if args.max_requests >= 1:
        _, catalog_payload = issue("sports_catalog", f"{BASE_URL}/sports", None)
    if args.max_requests >= 2 and stop_reason is None:
        _, affiliate_payload = issue(
            "affiliate_catalog", f"{BASE_URL}/affiliates", None
        )
    affiliate_names = _affiliate_names(affiliate_payload)
    verified = _verified_league_ids(catalog_payload) if args.scope == "top5" else {}
    if args.scope == "ucl":
        verified = {
            "UEFA Champions League": {
                "id": str(SPORT_ID),
                "name": "UEFA Champions League",
                "abbreviation": "UEFA.CHAMP",
            }
        }

    league_reports: list[dict[str, object]] = []
    completed_targets: set[str] = set()
    targets = list(TOP5_TARGETS) if args.scope == "top5" else ["UEFA Champions League"]
    for target in targets:
        league = verified.get(target)
        if (
            league is None
            or stop_reason is not None
            or len(outputs) >= args.max_requests
        ):
            league_reports.append(_league_result(target, league, None, None))
            completed_targets.add(target)
            continue
        league_id = str(league["id"])
        date_output, date_payload = issue(
            f"{target}:dates", f"{BASE_URL}/sports/{league_id}/dates", None
        )
        selected_date = _date_from_payload(date_payload, requested_date)
        event_output: dict[str, object] | None = None
        if (
            date_output.get("status") == 200
            and stop_reason is None
            and len(outputs) < args.max_requests
        ):
            event_output, event_payload = issue(
                f"{target}:events",
                f"{BASE_URL}/sports/{league_id}/events/{selected_date}",
                {
                    "market_ids": "1",
                    "main_line": "true",
                    "hide_closed": "true",
                    "hide_no_markets": "true",
                },
            )
            if event_output.get("status") == 200:
                event_output["events"] = _event_details(event_payload, affiliate_names)
        report = _league_result(target, league, date_output, event_output)
        report["selected_date"] = selected_date
        league_reports.append(report)
        completed_targets.add(target)
        if stop_reason is not None:
            break

    for target in targets:
        if target not in completed_targets:
            league_reports.append(
                _league_result(target, verified.get(target), None, None)
            )
    if args.scope == "top5":
        league_reports.sort(key=lambda item: targets.index(str(item["league"])))
    if task_datapoints_before is not None and task_datapoints_after is not None:
        datapoints_consumed: int | None = max(
            0, task_datapoints_after - task_datapoints_before
        )
    else:
        datapoints_consumed = per_request_datapoints or None
    print(
        json.dumps(
            {
                "status": "REAL_TEST_EXECUTED",
                "scope": args.scope,
                "real_requests": len(outputs),
                "request_limit": args.max_requests,
                "datapoints_before": task_datapoints_before,
                "datapoints_after": task_datapoints_after,
                "datapoints_consumed": datapoints_consumed,
                "account_snapshot": _account_snapshot(outputs),
                "leagues": league_reports,
                "responses": outputs,
                "stopped_reason": stop_reason,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded, redacted TheRundown account/coverage diagnostic.

This is intentionally separate from the active provider cascade.  It makes no
requests unless THERUNDOWN_API_KEY is already present in the process
environment, never prints response bodies or credentials, and makes at most
five sequential requests without retries.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

import requests

BASE_URL = "https://therundown.io/api/v2"
SPORT_ID = 16
DEFAULT_MAX_REQUESTS = 5


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
    if isinstance(payload, dict) and isinstance(payload.get("dates"), list):
        for value in payload["dates"]:
            text = str(value).strip()
            try:
                return (
                    datetime.fromisoformat(text.replace("Z", "+00:00"))
                    .date()
                    .isoformat()
                )
            except ValueError:
                continue
    return fallback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    if not 0 <= args.max_requests <= DEFAULT_MAX_REQUESTS:
        parser.error("--max-requests must be between 0 and 5")
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
                    "real_requests": 0,
                    "required_environment": "THERUNDOWN_API_KEY",
                    "command": "THERUNDOWN_API_KEY=... python3 scripts/therundown_diagnostic.py",
                },
                sort_keys=True,
            )
        )
        return 0

    paths: list[tuple[str, dict[str, str] | None]] = [
        (f"{BASE_URL}/sports", None),
        (f"{BASE_URL}/affiliates", None),
        (f"{BASE_URL}/sports/{SPORT_ID}/dates", None),
    ]
    session = requests.Session()
    outputs: list[dict[str, object]] = []
    datapoints_consumed = 0
    selected_date = requested_date
    for index in range(min(args.max_requests, len(paths))):
        path, params = paths[index]
        output, payload = _request(
            session, key, path, params=params, timeout=args.timeout
        )
        outputs.append(output)
        value = output.get("quota", {})
        if isinstance(value, dict):
            datapoints_consumed += int(value.get("datapoints") or 0)
        if index == 2:
            selected_date = _date_from_payload(payload, requested_date)

    if len(outputs) < args.max_requests:
        extra: list[tuple[str, dict[str, str] | None]] = [
            (
                f"{BASE_URL}/sports/{SPORT_ID}/events/{selected_date}",
                {
                    "market_ids": "1",
                    "main_line": "true",
                    "hide_closed": "true",
                    "hide_no_markets": "true",
                },
            ),
            (f"{BASE_URL}/sports/{SPORT_ID}", None),
        ]
        for path, params in extra[: args.max_requests - len(outputs)]:
            output, _ = _request(
                session, key, path, params=params, timeout=args.timeout
            )
            outputs.append(output)
            value = output.get("quota", {})
            if isinstance(value, dict):
                datapoints_consumed += int(value.get("datapoints") or 0)

    print(
        json.dumps(
            {
                "status": "REAL_TEST_EXECUTED",
                "real_requests": len(outputs),
                "datapoints_consumed": datapoints_consumed,
                "selected_date": selected_date,
                "responses": outputs,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

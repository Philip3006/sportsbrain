#!/usr/bin/env python3
"""Run a bounded, read-only API-Football Champions League diagnostic.

The diagnostic is deliberately separate from SportsBrain runtime paths. It
uses one date-scoped fixture request and at most four fixture-scoped odds
requests, for a hard maximum of five API requests. It does not write provider
or runtime state, issue qualification evidence, publish, bet, or activate a
signal path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.football.experimental.api_football import (
    API_FOOTBALL_PAGE_SIZE,
    ApiFootballCallResult,
    ApiFootballDiagnosticBudget,
    ApiFootballExperimentalAdapter,
    ApiFootballFailure,
    summarize_odds_payload,
)
from src.football.provider_cascade.contracts import ProviderState


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded API-Football UCL fixture/1X2 diagnostic (max 5 requests)."
    )
    parser.add_argument(
        "--date",
        dest="match_date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="UTC match date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="API-Football season year (default: year from --date)",
    )
    parser.add_argument(
        "--max-odds-requests",
        type=int,
        default=4,
        help="Maximum fixture odds requests; hard limited to four",
    )
    return parser


def _safe_result(result: ApiFootballCallResult) -> dict[str, object]:
    """Return only the adapter's redacted result contract."""

    return result.as_payload()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit("--date must be YYYY-MM-DD") from exc


def _coverage_summary(
    odds_results: list[ApiFootballCallResult], capture: datetime
) -> dict[str, object]:
    bookmakers: set[str] = set()
    winner_fixture_ids: set[str] = set()
    source_timestamps: set[str] = set()
    observations = 0
    failures: list[str] = []
    for result in odds_results:
        observations += result.accepted_observation_count
        if result.failure is not ApiFootballFailure.NONE:
            failures.append(ApiFootballFailure(result.failure).value)
        if isinstance(result.payload, dict):
            try:
                summary = summarize_odds_payload(result.payload)
            except ValueError:
                continue
            bookmakers.update(summary.bookmaker_names)
            winner_fixture_ids.update(summary.match_winner_fixture_ids)
            source_timestamps.update(
                timestamp.isoformat() for timestamp in summary.source_timestamps
            )
    ages = []
    for timestamp in source_timestamps:
        parsed = datetime.fromisoformat(timestamp)
        ages.append((capture - parsed).total_seconds())
    return {
        "bookmakers": sorted(bookmakers),
        "match_winner_fixture_ids": sorted(winner_fixture_ids),
        "source_timestamps": sorted(source_timestamps),
        "freshness_seconds": {
            "oldest": max(ages) if ages else None,
            "newest": min(ages) if ages else None,
        },
        "real_observations_validated": observations,
        "odds_failures": failures,
    }


def run_diagnostic(
    *,
    api_key: str | None,
    match_date: date,
    season: int,
    max_odds_requests: int,
) -> dict[str, object]:
    if max_odds_requests < 0 or max_odds_requests > 4:
        raise SystemExit("--max-odds-requests must be between 0 and 4")
    budget = ApiFootballDiagnosticBudget(maximum_requests=1 + max_odds_requests)
    adapter = ApiFootballExperimentalAdapter()
    capture = datetime.now(timezone.utc)
    results: list[ApiFootballCallResult] = []

    if not budget.reserve():
        raise SystemExit("diagnostic budget cannot reserve fixture request")
    fixture_result = adapter.fetch_real_fixtures(
        api_key=api_key,
        season=season,
        match_date=match_date,
        request_identity=f"app-b4-diagnostic:fixtures:{match_date.isoformat()}",
        capture_timestamp=capture,
    )
    results.append(fixture_result)
    if fixture_result.state is not ProviderState.AVAILABLE:
        return {
            "status": "BLOCKED",
            "requests_executed": budget.requests_used,
            "request_budget": budget.maximum_requests,
            "fixture_count": 0,
            "odds_requests_executed": 0,
            "failure": ApiFootballFailure(fixture_result.failure).value,
            "results": [_safe_result(item) for item in results],
        }

    candidates = fixture_result.fixture_candidates
    odds_results: list[ApiFootballCallResult] = []
    for index, candidate in enumerate(candidates[:max_odds_requests]):
        if not budget.reserve():
            break
        odds_results.append(
            adapter.fetch_real_odds(
                candidate,
                api_key=api_key,
                request_identity=(
                    f"app-b4-diagnostic:odds:{candidate.provider_fixture_id}:{index}"
                ),
                provider_fixture_id=candidate.provider_fixture_id,
                capture_timestamp=capture,
            )
        )
    results.extend(odds_results)
    coverage = _coverage_summary(odds_results, capture)
    return {
        "status": "REAL_DIAGNOSTIC_COMPLETE",
        "requests_executed": budget.requests_used,
        "request_budget": budget.maximum_requests,
        "fixture_count": len(candidates),
        "odds_requests_executed": len(odds_results),
        "odds_requests_omitted_by_budget": max(0, len(candidates) - len(odds_results)),
        "estimated_normal_matchday_requests": 1 + len(candidates),
        "estimated_odds_page_size": API_FOOTBALL_PAGE_SIZE,
        "coverage": coverage,
        "results": [_safe_result(item) for item in results],
        "authority": "unchanged; api_football is experimental and non-authoritative",
    }


def main() -> int:
    args = _parser().parse_args()
    match_date = _parse_date(args.match_date)
    season = args.season or match_date.year
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip() or None
    if api_key is None:
        print(
            json.dumps(
                {
                    "status": "REAL_TEST_READY",
                    "requests_executed": 0,
                    "request_budget": 5,
                    "reason": "API_FOOTBALL_KEY is not configured",
                    "command": (
                        "API_FOOTBALL_KEY=<secret> python3 "
                        "scripts/api_football_diagnostic.py"
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    report = run_diagnostic(
        api_key=api_key,
        match_date=match_date,
        season=season,
        max_odds_requests=args.max_odds_requests,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

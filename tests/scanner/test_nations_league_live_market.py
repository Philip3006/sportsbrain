from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.scanner.nations_league_live_market import acquire_live_market_snapshots


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _manifest(*, kickoff: str = "2026-10-02T18:45:00Z", rows: list[dict] | None = None) -> dict:
    fixtures = rows or [
        {
            "fixture_id": "uefa-nl:future-test",
            "status": "VERIFIED",
            "home_team": "Denmark",
            "away_team": "Portugal",
            "kickoff_utc": kickoff,
        }
    ]
    body = {"schema": "nations-league-future-fixture-manifest-v1", "competition": "UEFA Nations League", "fixtures": fixtures}
    return {**body, "manifest_digest": _digest(body)}


def _event(*, event_id: str = "odds-event-1", odds: dict | None = None) -> dict:
    prices = odds or {"home": 2.4, "draw": 3.2, "away": 2.9}
    return {
        "id": event_id,
        "sport_key": "soccer_uefa_nations_league",
        "sport_title": "UEFA Nations League",
        "commence_time": "2026-10-02T18:45:00Z",
        "home_team": "Denmark",
        "away_team": "Portugal",
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Denmark", "price": prices["home"]},
                            {"name": "Draw", "price": prices["draw"]},
                            {"name": "Portugal", "price": prices["away"]},
                        ],
                    }
                ],
            }
        ],
    }


def test_no_due_fixture_does_not_call_provider():
    called = False

    def forbidden_fetcher():
        nonlocal called
        called = True
        raise AssertionError("provider must not be called when no fixture is due")

    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T00:00:00Z",
        fetcher=forbidden_fetcher,
    )

    assert called is False
    assert batch["status"] == "NO_MARKET_SNAPSHOT"
    assert batch["request_count"] == 0
    assert batch["snapshots"] == []


def test_due_provider_response_becomes_canonical_snapshot_without_duplicate_devig():
    def fetcher():
        return [_event()], 1, 0, {
            "method": "GET",
            "url": "https://api.the-odds-api.com/v4/sports/soccer_uefa_nations_league/odds",
            "query": {"regions": "eu", "markets": "h2h", "oddsFormat": "decimal"},
        }

    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
        fetcher=fetcher,
        now=__import__("datetime").datetime.fromisoformat("2026-10-01T18:45:01+00:00"),
    )

    assert batch["status"] == "READY"
    assert batch["request_count"] == 1
    assert batch["retry_count"] == 0
    snapshot = batch["snapshots"][0]
    assert snapshot["schema"] == "nations-league-live-market-snapshot-v1"
    assert snapshot["provider"] == "the_odds_api"
    assert snapshot["bookmaker"] == "pinnacle"
    assert snapshot["fixture_id"] == "uefa-nl:future-test"
    assert snapshot["provider_match_id"] == "odds-event-1"
    assert set(snapshot["odds_decimal"]) == {"home", "draw", "away"}
    assert abs(sum(snapshot["margin_free_probabilities"].values()) - 1.0) < 1e-6
    assert len(snapshot["snapshot_digest"]) == 64


def test_provider_failure_is_redacted_and_model_safe():
    def failing_fetcher():
        raise RuntimeError("credential=must-not-escape")

    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
        fetcher=failing_fetcher,
    )

    assert batch["status"] == "NO_MARKET_SNAPSHOT"
    assert batch["snapshots"] == []
    assert "credential" not in json.dumps(batch)
    assert list(batch["failure_reasons"].values()) == ["provider_error:RuntimeError"]


def test_ambiguous_provider_identity_fails_closed_for_that_fixture():
    def fetcher():
        return [_event(), _event(event_id="odds-event-duplicate")], 1, 0, {"method": "GET"}

    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
        fetcher=fetcher,
    )

    assert batch["status"] == "MARKET_INVALID"
    assert batch["snapshots"] == []
    assert batch["failure_reasons"] == {
        "uefa-nl:future-test": "ambiguous_provider_fixture_identity"
    }


def test_incomplete_1x2_market_is_not_materialized():
    bad_event = _event()
    bad_event["bookmakers"][0]["markets"][0]["outcomes"] = [
        {"name": "Denmark", "price": 2.4},
        {"name": "Portugal", "price": 2.9},
    ]

    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
        fetcher=lambda: ([bad_event], 1, 0, {"method": "GET"}),
    )

    assert batch["status"] == "MARKET_INVALID"
    assert batch["snapshots"] == []
    assert batch["failure_reasons"]["uefa-nl:future-test"] == "malformed_h2h_odds"


def test_unexpected_request_budget_or_retry_contract_fails_closed():
    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
        fetcher=lambda: ([_event()], 2, 1, {"method": "GET"}),
    )

    assert batch["status"] == "MARKET_INVALID"
    assert batch["snapshots"] == []
    assert batch["request_count"] == 0
    assert batch["retry_count"] == 0


def test_workflow_materializes_snapshots_before_the_existing_cycle():
    workflow = Path(".github/workflows/nations_league_live_cycle.yml").read_text()

    assert "workflow_dispatch:" in workflow
    assert "cron: '*/15 * * * *'" in workflow
    assert "scripts/acquire_nations_league_live_market.py" in workflow
    assert "ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}" in workflow
    assert "--market-snapshots /tmp/nations-league-live-market-snapshots.json" in workflow
    assert workflow.count("CYCLE_AS_OF=") == 2
    assert "--execute-offline" in workflow
    assert "scripts/resolve_nations_league_source_release.py" in workflow
    assert "if: ${{ secrets." not in workflow
    assert "scripts/_bot_commit_push.sh" in workflow

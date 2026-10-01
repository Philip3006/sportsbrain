from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import src.scanner.nations_league_live_market as market_module
from src.scanner.nations_league_live_market import (
    acquire_live_market_snapshots,
    prepare_market_preflight,
)


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
    body = {
        "schema": "nations-league-future-fixture-manifest-v1",
        "competition": "UEFA Nations League",
        "fixtures": fixtures,
    }
    return {**body, "manifest_digest": _digest(body)}


def _event(
    *,
    event_id: str = "odds-event-1",
    odds: dict | None = None,
    home: str = "Denmark",
    away: str = "Portugal",
    kickoff: str = "2026-10-02T18:45:00Z",
) -> dict:
    prices = odds or {"home": 2.4, "draw": 3.2, "away": 2.9}
    return {
        "id": event_id,
        "sport_key": "soccer_uefa_nations_league",
        "sport_title": "UEFA Nations League",
        "commence_time": kickoff,
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": home, "price": prices["home"]},
                            {"name": "Draw", "price": prices["draw"]},
                            {"name": away, "price": prices["away"]},
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
        now=datetime.fromisoformat("2026-10-01T18:45:01+00:00"),
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


def test_captured_initial_phase_suppresses_provider_request():
    called = False

    def forbidden_fetcher():
        nonlocal called
        called = True
        raise AssertionError("captured phase must not call provider")

    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
        existing_records=[{"fixture_id": "uefa-nl:future-test", "phase": "initial"}],
        fetcher=forbidden_fetcher,
    )

    assert called is False
    assert batch["needs_provider"] is False
    assert batch["request_count"] == 0
    assert batch["retry_count"] == 0


def test_uncaptured_refinement_phase_calls_provider_once():
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        return [_event()], 1, 0, {"method": "GET"}

    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-02T16:45:00Z",
        fetcher=fetcher,
        now=datetime.fromisoformat("2026-10-02T16:45:01+00:00"),
    )

    assert calls == 1
    assert batch["status"] == "READY"
    assert batch["snapshots"][0]["fixture_id"] == "uefa-nl:future-test"


def test_captured_refinement_phase_suppresses_provider_request():
    called = False

    def forbidden_fetcher():
        nonlocal called
        called = True
        raise AssertionError("captured refinement must not call provider")

    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-02T16:45:00Z",
        existing_records=[{"fixture_id": "uefa-nl:future-test", "phase": "refinement"}],
        fetcher=forbidden_fetcher,
    )

    assert called is False
    assert batch["request_count"] == 0


def test_repeated_execution_after_first_capture_has_no_backfill_request():
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        return [_event()], 1, 0, {"method": "GET"}

    first = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
        fetcher=fetcher,
        now=datetime.fromisoformat("2026-10-01T18:45:01+00:00"),
    )
    second = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T19:00:00Z",
        existing_records=[{"fixture_id": "uefa-nl:future-test", "phase": "initial"}],
        fetcher=fetcher,
    )

    assert first["status"] == "READY"
    assert calls == 1
    assert second["snapshots"] == []
    assert second["request_count"] == 0


def test_multiple_pending_fixtures_share_one_provider_request():
    rows = [
        {
            "fixture_id": "uefa-nl:future-test",
            "status": "VERIFIED",
            "home_team": "Denmark",
            "away_team": "Portugal",
            "kickoff_utc": "2026-10-02T18:45:00Z",
        },
        {
            "fixture_id": "uefa-nl:future-test-2",
            "status": "VERIFIED",
            "home_team": "Greece",
            "away_team": "Netherlands",
            "kickoff_utc": "2026-10-02T18:45:00Z",
        },
    ]
    calls = 0

    def fetcher():
        nonlocal calls
        calls += 1
        return [
            _event(),
            _event(
                event_id="odds-event-2",
                home="Greece",
                away="Netherlands",
            ),
        ], 1, 0, {"method": "GET"}

    batch = acquire_live_market_snapshots(
        _manifest(rows=rows),
        as_of="2026-10-01T18:45:00Z",
        fetcher=fetcher,
        now=datetime.fromisoformat("2026-10-01T18:45:01+00:00"),
    )

    assert calls == 1
    assert batch["request_count"] == 1
    assert {row["fixture_id"] for row in batch["snapshots"]} == {
        "uefa-nl:future-test",
        "uefa-nl:future-test-2",
    }


def test_mixed_captured_and_pending_fixtures_only_materialize_pending():
    rows = [
        {
            "fixture_id": "uefa-nl:future-test",
            "status": "VERIFIED",
            "home_team": "Denmark",
            "away_team": "Portugal",
            "kickoff_utc": "2026-10-02T18:45:00Z",
        },
        {
            "fixture_id": "uefa-nl:future-test-2",
            "status": "VERIFIED",
            "home_team": "Greece",
            "away_team": "Netherlands",
            "kickoff_utc": "2026-10-02T18:45:00Z",
        },
    ]
    batch = acquire_live_market_snapshots(
        _manifest(rows=rows),
        as_of="2026-10-01T18:45:00Z",
        existing_records=[{"fixture_id": "uefa-nl:future-test", "phase": "initial"}],
        fetcher=lambda: (
            [_event(event_id="odds-event-2", home="Greece", away="Netherlands")],
            1,
            0,
            {"method": "GET"},
        ),
        now=datetime.fromisoformat("2026-10-01T18:45:01+00:00"),
    )

    assert batch["request_count"] == 1
    assert [row["fixture_id"] for row in batch["snapshots"]] == [
        "uefa-nl:future-test-2"
    ]
    assert batch["due_fixtures"] == [
        {"fixture_id": "uefa-nl:future-test-2", "phase": "initial", "due_state": "INITIAL_DUE"}
    ]


def test_malformed_store_fails_before_provider_call():
    called = False

    def forbidden_fetcher():
        nonlocal called
        called = True
        raise AssertionError("malformed store must block before provider")

    with pytest.raises(ValueError, match="stored phase"):
        acquire_live_market_snapshots(
            _manifest(),
            as_of="2026-10-01T18:45:00Z",
            existing_records=[{"fixture_id": "uefa-nl:future-test"}],
            fetcher=forbidden_fetcher,
        )
    assert called is False


def test_default_provider_fetcher_is_not_even_loaded_when_no_pending_phase(monkeypatch):
    called = False

    def forbidden_fetcher():
        nonlocal called
        called = True
        raise AssertionError("default provider loader must not run")

    monkeypatch.setattr(market_module, "fetch_provider_events", forbidden_fetcher)
    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
        existing_records=[{"fixture_id": "uefa-nl:future-test", "phase": "initial"}],
    )

    assert called is False
    assert batch["request_count"] == 0


def test_capture_window_boundary_is_not_backdated_into_prediction():
    kickoff = datetime.fromisoformat("2026-10-02T18:45:00+00:00")
    selection = kickoff - timedelta(hours=22)
    capture = selection + timedelta(seconds=1)
    batch = acquire_live_market_snapshots(
        _manifest(kickoff="2026-10-02T18:45:00Z"),
        as_of=selection.isoformat().replace("+00:00", "Z"),
        fetcher=lambda: ([_event()], 1, 0, {"method": "GET"}),
        now=capture,
    )

    assert batch["snapshots"] == []
    assert batch["failure_reasons"] == {
        "uefa-nl:future-test": "capture_window_changed"
    }
    assert batch["captured_at"] == "2026-10-01T20:45:01Z"
    assert batch["captured_at"] != "2026-10-01T20:45:00Z"


def test_capture_inside_same_phase_is_causal_and_not_future_dated():
    capture = datetime.fromisoformat("2026-10-01T18:45:01+00:00")
    batch = acquire_live_market_snapshots(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
        fetcher=lambda: ([_event()], 1, 0, {"method": "GET"}),
        now=capture,
    )

    assert batch["status"] == "READY"
    assert batch["snapshots"][0]["captured_at"] <= batch["captured_at"]


def test_preflight_reports_provider_need_without_network():
    batch = prepare_market_preflight(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
    )

    assert batch["needs_provider"] is True
    assert batch["request_count"] == 0
    assert batch["snapshots"] == []


def test_preflight_reports_no_provider_for_all_captured_due_phases():
    batch = prepare_market_preflight(
        _manifest(),
        as_of="2026-10-01T18:45:00Z",
        existing_records=[{"fixture_id": "uefa-nl:future-test", "phase": "initial"}],
    )

    assert batch["needs_provider"] is False
    assert batch["status"] == "NO_MARKET_SNAPSHOT"


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
    preflight = workflow.split("- name: Prepare due market preflight", 1)[1].split(
        "- name: Acquire due current 1X2 market snapshots", 1
    )[0]
    provider_step = workflow.split("- name: Acquire due current 1X2 market snapshots", 1)[1]
    assert "ODDS_API_KEY" not in preflight
    assert "--preflight" in preflight
    assert "needs_provider" in preflight
    assert "if: ${{ steps.market_preflight.outputs.needs_provider == 'true' }}" in provider_step
    assert provider_step.count("ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}") == 1
    assert "--store results/research/nations_league_v1_1_live_prediction_store.jsonl" in workflow
    assert "--market-snapshots /tmp/nations-league-live-market-snapshots.json" in workflow
    assert "CYCLE_AS_OF=\"$(python3 -c" in workflow
    assert "--execute-offline" in workflow
    assert "scripts/resolve_nations_league_source_release.py" in workflow
    assert "if: ${{ secrets." not in workflow
    assert "scripts/_bot_commit_push.sh" in workflow

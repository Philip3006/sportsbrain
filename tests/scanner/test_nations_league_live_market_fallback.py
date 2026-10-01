from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import src.scanner.nations_league_live_market as market_module
from src.scanner.nations_league_live_market import acquire_live_market_snapshots

CAPTURE = datetime(2026, 10, 1, 18, 45, 1, tzinfo=timezone.utc)
KICKOFF = "2026-10-02T18:45:00Z"
AS_OF = "2026-10-01T18:45:00Z"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _manifest(rows: list[dict] | None = None) -> dict:
    fixtures = rows or [
        {
            "fixture_id": "uefa-nl:future-test",
            "status": "VERIFIED",
            "home_team": "Denmark",
            "away_team": "Portugal",
            "kickoff_utc": KICKOFF,
        }
    ]
    body = {
        "schema": "nations-league-future-fixture-manifest-v1",
        "competition": "UEFA Nations League",
        "fixtures": fixtures,
    }
    return {**body, "manifest_digest": _digest(body)}


def _schedule(
    match_id: str = "nl-1",
    *,
    home: str = "Denmark",
    away: str = "Portugal",
    kickoff: str = KICKOFF,
) -> dict:
    return {
        "matchId": match_id,
        "leagueId": "146819",
        "leagueType": 2,
        "leagueName": "UEFA Nations League",
        "matchTime": int(
            datetime.fromisoformat(kickoff.replace("Z", "+00:00")).timestamp()
        ),
        "status": 0,
        "homeName": home,
        "awayName": away,
        "neutral": True,
    }


def _odds(
    match_id: str = "nl-1",
    *,
    home: str = "Denmark",
    away: str = "Portugal",
    kickoff: str = KICKOFF,
    change_time: str = "2026-10-01T18:40:00Z",
) -> dict:
    changed = int(
        datetime.fromisoformat(change_time.replace("Z", "+00:00")).timestamp()
    )
    return {
        "matchId": match_id,
        "matchTime": int(
            datetime.fromisoformat(kickoff.replace("Z", "+00:00")).timestamp()
        ),
        "leagueName": "UEFA Nations League",
        "homeName": home,
        "awayName": away,
        "odds": [
            {
                "changeTime": changed,
                "oddsDetail": ["100,Reviewed Bookmaker,2.4,3.1,3.2,2.4,3.1,3.2"],
            }
        ],
    }


def _operation(
    kind: str, ordinal: int, payload: list[dict], query: dict
) -> SimpleNamespace:
    return SimpleNamespace(
        payload=payload,
        manifest_entry={
            "ordinal": ordinal,
            "operation": kind,
            "method": "GET",
            "path": (
                "/sport/football/schedule/basic"
                if kind == "schedule"
                else "/sport/football/odds/european/all"
            ),
            "query": query,
            "status_code": 200,
            "started_at": "2026-10-01T18:00:00+00:00",
            "completed_at": "2026-10-01T18:00:01+00:00",
            "response_sha256": "a" * 64,
        },
        safe_rate_headers={"x-ratelimit-remaining": "73"},
    )


def _patch_budget(
    monkeypatch,
    *,
    primary: dict | None = None,
    isports: dict | None = None,
    remaining: int = 5,
):
    from src.signals import provider_budget

    monkeypatch.setattr(
        provider_budget,
        "get_budget_snapshot",
        lambda: {
            **({"the_odds_api": primary} if primary is not None else {}),
            **({"isports_api": isports} if isports is not None else {}),
        },
    )
    monkeypatch.setattr(
        provider_budget,
        "odds_api_quota_state",
        lambda: {"requests_remaining": remaining},
    )
    monkeypatch.setattr(
        provider_budget, "record_success", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(provider_budget, "record_error", lambda *_args, **_kwargs: None)


def _patch_isports(monkeypatch, *, schedule_rows, odds_rows):
    calls: list[tuple[str, dict]] = []

    def fake_request_once(*, operation_kind, ordinal, query, **_kwargs):
        calls.append((operation_kind, dict(query)))
        payload = schedule_rows if operation_kind == "schedule" else odds_rows
        return _operation(operation_kind, ordinal, payload, dict(query))

    monkeypatch.setattr(market_module, "load_isports_api_key", lambda: "synthetic-key")
    monkeypatch.setattr(market_module, "request_once", fake_request_once)
    return calls


def _run_fallback(monkeypatch, *, schedule_rows=None, odds_rows=None, now=CAPTURE):
    _patch_budget(monkeypatch, remaining=0)
    calls = _patch_isports(
        monkeypatch,
        schedule_rows=[_schedule()] if schedule_rows is None else schedule_rows,
        odds_rows=[_odds()] if odds_rows is None else odds_rows,
    )
    monkeypatch.setattr(
        market_module,
        "fetch_provider_events",
        lambda: pytest.fail("The Odds API must not be called while exhausted"),
    )
    batch = acquire_live_market_snapshots(_manifest(), as_of=AS_OF, now=now)
    return batch, calls


def test_healthy_primary_selects_the_odds_api_without_isports(monkeypatch):
    _patch_budget(monkeypatch, remaining=5)
    called = {"isports": False}
    monkeypatch.setattr(
        market_module,
        "fetch_provider_events",
        lambda: (
            [
                {
                    "id": "toa-1",
                    "sport_key": "soccer_uefa_nations_league",
                    "sport_title": "UEFA Nations League",
                    "commence_time": KICKOFF,
                    "home_team": "Denmark",
                    "away_team": "Portugal",
                    "bookmakers": [
                        {
                            "key": "reviewed-bookmaker",
                            "markets": [
                                {
                                    "key": "h2h",
                                    "outcomes": [
                                        {"name": "Denmark", "price": 2.4},
                                        {"name": "Draw", "price": 3.1},
                                        {"name": "Portugal", "price": 3.2},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
            1,
            0,
            {"method": "GET"},
        ),
    )

    def forbidden_isports(*_args, **_kwargs):
        called["isports"] = True
        raise AssertionError("iSports must not be called while primary is healthy")

    monkeypatch.setattr(
        market_module, "_fetch_isports_market_snapshots", forbidden_isports
    )
    batch = acquire_live_market_snapshots(_manifest(), as_of=AS_OF, now=CAPTURE)

    assert batch["status"] == "READY"
    assert batch["selected_provider"] == "the_odds_api"
    assert batch["fallback_depth"] == 0
    assert batch["primary_provider_state"] == "AVAILABLE"
    assert batch["provider_request_counts"] == {"the_odds_api": 1, "isports_api": 0}
    assert called["isports"] is False


def test_quota_exhaustion_routes_directly_to_isports_without_primary_request(
    monkeypatch,
):
    batch, calls = _run_fallback(monkeypatch)

    assert batch["status"] == "READY"
    assert batch["provider"] == "isports_api"
    assert batch["selected_provider"] == "isports_api"
    assert batch["fallback_depth"] == 1
    assert batch["primary_provider_state"] == "QUOTA_EXHAUSTED"
    assert batch["provider_attempt_count"] == 1
    assert batch["provider_request_counts"] == {"the_odds_api": 0, "isports_api": 2}
    assert batch["total_network_request_count"] == 2
    assert batch["retry_count"] == 0
    assert [kind for kind, _query in calls] == ["schedule", "odds"]
    assert batch["request"]["provider_operation_manifest"][1]["query"] == {
        "matchId": "nl-1"
    }
    assert batch["provider_trace"]["quote_updated_at"] == {
        "uefa-nl:future-test": "2026-10-01T18:40:00Z"
    }
    assert "synthetic-key" not in json.dumps(batch)


def test_open_primary_circuit_routes_to_isports_and_keeps_states_separate(monkeypatch):
    _patch_budget(monkeypatch, primary={"circuit_open": True}, remaining=5)
    calls = _patch_isports(
        monkeypatch, schedule_rows=[_schedule()], odds_rows=[_odds()]
    )
    monkeypatch.setattr(
        market_module,
        "fetch_provider_events",
        lambda: pytest.fail("open primary circuit must skip The Odds API"),
    )

    batch = acquire_live_market_snapshots(_manifest(), as_of=AS_OF, now=CAPTURE)

    assert batch["status"] == "READY"
    assert batch["primary_provider_state"] == "CIRCUIT_OPEN"
    assert batch["provider_request_counts"] == {"the_odds_api": 0, "isports_api": 2}
    assert len(calls) == 2


def test_primary_transport_failure_falls_back_once_without_retrying_the_primary(
    monkeypatch,
):
    _patch_budget(monkeypatch, remaining=5)
    calls = _patch_isports(
        monkeypatch, schedule_rows=[_schedule()], odds_rows=[_odds()]
    )
    primary_calls = {"count": 0}

    def failed_primary():
        primary_calls["count"] += 1
        raise RuntimeError("transport unavailable")

    monkeypatch.setattr(market_module, "fetch_provider_events", failed_primary)
    batch = acquire_live_market_snapshots(_manifest(), as_of=AS_OF, now=CAPTURE)

    assert batch["status"] == "READY"
    assert primary_calls["count"] == 1
    assert batch["primary_provider_state"] == "UNAVAILABLE"
    assert batch["fallback_depth"] == 1
    assert batch["provider_request_counts"] == {"the_odds_api": 1, "isports_api": 2}
    assert batch["retry_count"] == 0
    assert len(calls) == 2


def test_two_due_fixtures_share_one_bounded_isports_schedule_and_odds_batch(
    monkeypatch,
):
    rows = [
        {
            "fixture_id": "uefa-nl:one",
            "status": "VERIFIED",
            "home_team": "Denmark",
            "away_team": "Portugal",
            "kickoff_utc": KICKOFF,
        },
        {
            "fixture_id": "uefa-nl:two",
            "status": "VERIFIED",
            "home_team": "Spain",
            "away_team": "Italy",
            "kickoff_utc": KICKOFF,
        },
    ]
    _patch_budget(monkeypatch, remaining=0)
    calls = _patch_isports(
        monkeypatch,
        schedule_rows=[_schedule(), _schedule("nl-2", home="Spain", away="Italy")],
        odds_rows=[_odds(), _odds("nl-2", home="Spain", away="Italy")],
    )
    monkeypatch.setattr(
        market_module, "fetch_provider_events", lambda: pytest.fail("no TOA")
    )

    batch = acquire_live_market_snapshots(_manifest(rows), as_of=AS_OF, now=CAPTURE)

    assert batch["status"] == "READY"
    assert len(batch["snapshots"]) == 2
    assert len(calls) == 2
    assert calls[1][1] == {"matchId": "nl-1,nl-2"}


def test_ambiguous_isports_identity_is_rejected_before_bulk_odds(monkeypatch):
    batch, calls = _run_fallback(
        monkeypatch,
        schedule_rows=[_schedule("nl-1"), _schedule("nl-duplicate")],
        odds_rows=[],
    )

    assert batch["status"] == "MARKET_INVALID"
    assert batch["snapshots"] == []
    assert batch["failure_reasons"] == {
        "uefa-nl:future-test": "ambiguous_provider_fixture_identity"
    }
    assert calls == [("schedule", {"leagueId": "146819"})]


def test_incomplete_isports_1x2_market_is_rejected(monkeypatch):
    batch, calls = _run_fallback(monkeypatch, odds_rows=[])

    assert batch["status"] == "MARKET_INVALID"
    assert batch["snapshots"] == []
    assert batch["failure_reasons"] == {"uefa-nl:future-test": "incomplete_1x2_market"}
    assert len(calls) == 2


def test_quote_after_capture_is_rejected_without_backdating(monkeypatch):
    batch, _calls = _run_fallback(
        monkeypatch, odds_rows=[_odds(change_time="2026-10-01T19:00:00Z")]
    )

    assert batch["status"] == "MARKET_INVALID"
    assert batch["snapshots"] == []
    assert batch["failure_reasons"] == {"uefa-nl:future-test": "quote_after_capture"}


def test_stale_isports_quote_is_rejected(monkeypatch):
    batch, _calls = _run_fallback(
        monkeypatch, odds_rows=[_odds(change_time="2026-10-01T17:00:00Z")]
    )

    assert batch["status"] == "MARKET_INVALID"
    assert batch["snapshots"] == []
    assert batch["failure_reasons"] == {"uefa-nl:future-test": "stale_quote"}


def test_window_change_discards_isports_market(monkeypatch):
    batch, _calls = _run_fallback(
        monkeypatch,
        now=datetime(2026, 10, 1, 21, 45, 1, tzinfo=timezone.utc),
    )

    assert batch["status"] == "NO_MARKET_SNAPSHOT"
    assert batch["snapshots"] == []
    assert batch["failure_reasons"] == {"uefa-nl:future-test": "capture_window_changed"}


def test_both_providers_unavailable_is_safe_and_makes_no_network_request(monkeypatch):
    _patch_budget(
        monkeypatch,
        primary={"circuit_open": True},
        isports={"circuit_open": True},
        remaining=0,
    )
    monkeypatch.setattr(
        market_module,
        "fetch_provider_events",
        lambda: pytest.fail("The Odds API must remain untouched"),
    )
    monkeypatch.setattr(
        market_module,
        "_fetch_isports_market_snapshots",
        lambda *_args, **_kwargs: pytest.fail("iSports circuit must remain untouched"),
    )

    batch = acquire_live_market_snapshots(_manifest(), as_of=AS_OF, now=CAPTURE)

    assert batch["status"] == "NO_MARKET_SNAPSHOT"
    assert batch["selected_provider"] is None
    assert batch["primary_provider_state"] == "QUOTA_EXHAUSTED"
    assert batch["request_count"] == 0
    assert batch["retry_count"] == 0
    assert batch["provider_request_counts"] == {"the_odds_api": 0, "isports_api": 0}
    assert batch["no_bet"] is True
    assert batch["betting_enabled"] is False
    assert batch["ledger_mutation"] is False


def test_preflight_plans_isports_fallback_without_network(monkeypatch):
    _patch_budget(monkeypatch, remaining=0)
    monkeypatch.setattr(
        market_module,
        "fetch_provider_events",
        lambda: pytest.fail("preflight must not call a provider"),
    )
    batch = market_module.prepare_market_preflight(_manifest(), as_of=AS_OF)

    assert batch["needs_provider"] is True
    assert batch["selected_provider"] == "isports_api"
    assert batch["fallback_depth"] == 1
    assert batch["primary_provider_state"] == "QUOTA_EXHAUSTED"
    assert batch["total_network_request_count"] == 0

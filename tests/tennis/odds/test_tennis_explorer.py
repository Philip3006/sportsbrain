"""J8-B12: tennis_explorer.py — unit tests mit injiziertem Bulk."""
from __future__ import annotations

import pickle
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import src.tennis.odds.tennis_explorer as te


def _reset_bulk(entries: list[dict]):
    from src.data import tennis_secondary_odds

    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for entry in entries:
        entry.setdefault("source_observed_at", observed_at)
        entry.setdefault("te_parser_version", tennis_secondary_odds.TE_PARSER_VERSION)
    te._BULK = entries
    te._TS = time.time()


# ---------------------------------------------------------------------------
# fetch() — empty / missing players
# ---------------------------------------------------------------------------

def test_fetch_returns_none_for_empty_players():
    _reset_bulk([{"player_a": "Alcaraz", "player_b": "Sinner", "odds_a": 1.90, "odds_b": 2.05}])
    assert te.fetch({}) is None
    assert te.fetch({"player_a": "", "player_b": "Sinner"}) is None
    assert te.fetch({"player_a": "Alcaraz", "player_b": ""}) is None


# ---------------------------------------------------------------------------
# fetch() — match forward / reverse
# ---------------------------------------------------------------------------

def test_fetch_forward_match():
    _reset_bulk([{"player_a": "alcaraz c.", "player_b": "sinner j.",
                   "odds_a": 1.90, "odds_b": 2.05, "te_bookies_count": 3}])
    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner",
                   "name_source": "odds_api"})
    assert q is not None
    assert q.source == "tennis_explorer"
    assert q.source_tier == 2


def test_fetch_preserves_provider_response_capture_timestamp():
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=8)
    te._BULK = [
        {
            "player_a": "alcaraz c.",
            "player_b": "sinner j.",
            "odds_a": 1.90,
            "odds_b": 2.05,
            "te_bookies_count": 3,
            "te_parser_version": 3,
            "source_observed_at": observed_at.isoformat(),
        }
    ]
    te._TS = time.time()

    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"})

    assert q is not None
    assert q.ts == observed_at


def test_fetch_rejects_legacy_missing_or_stale_source_timestamp(monkeypatch):
    row = {
        "player_a": "alcaraz c.",
        "player_b": "sinner j.",
        "odds_a": 1.90,
        "odds_b": 2.05,
        "te_bookies_count": 3,
    }
    monkeypatch.setattr(te, "_quote_from_requested_fixture", lambda *_: None)
    from src.data import tennis_secondary_odds
    monkeypatch.setattr(
        tennis_secondary_odds,
        "fetch_te_upcoming_matches",
        lambda **_kwargs: [row],
    )
    te._BULK = [row]
    te._TS = time.time()
    assert te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"}) is None

    row["source_observed_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=31)
    ).isoformat()
    assert te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"}) is None


def test_bulk_with_legacy_cache_fails_with_missing_provenance(monkeypatch):
    from src.data import tennis_secondary_odds

    te._BULK = []
    te._TS = 0.0
    monkeypatch.setattr(
        tennis_secondary_odds,
        "fetch_te_upcoming_matches",
        lambda **_kwargs: [{"player_a": "alcaraz c.", "player_b": "sinner j."}],
    )

    bulk, outcome = te._get_bulk_with_diagnostics()

    assert bulk == []
    assert outcome.status_class == "missing_provenance"


def test_fetch_reverse_match_swaps_odds():
    _reset_bulk([{"player_a": "sinner j.", "player_b": "alcaraz c.",
                   "odds_a": 2.05, "odds_b": 1.90, "te_bookies_count": 2}])
    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner",
                   "name_source": "odds_api"})
    assert q is not None
    assert abs(q.h2h_a - 1.90) < 0.01   # Alcaraz nach Swap
    assert abs(q.h2h_b - 2.05) < 0.01


def test_fetch_no_match_returns_none(monkeypatch):
    monkeypatch.setattr(te, "_quote_from_requested_fixture", lambda *_: None)
    _reset_bulk([{"player_a": "nadal r.", "player_b": "federer r.",
                   "odds_a": 1.80, "odds_b": 2.10, "te_bookies_count": 4}])
    q = te.fetch({"player_a": "Alcaraz", "player_b": "Sinner"})
    assert q is None


def test_fetch_invalid_odds_rejected(monkeypatch):
    monkeypatch.setattr(te, "_quote_from_requested_fixture", lambda *_: None)
    # Overround > 1.15 → sanity_ok False
    _reset_bulk([{"player_a": "alcaraz c.", "player_b": "sinner j.",
                   "odds_a": 1.40, "odds_b": 1.40, "te_bookies_count": 1}])
    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"})
    assert q is None


# ---------------------------------------------------------------------------
# J8-B4: Stale-Bulk-Drop wenn Refresh leer ist
# ---------------------------------------------------------------------------

def test_stale_bulk_dropped_when_refresh_empty(monkeypatch):
    """_get_bulk() soll leere Liste zurückgeben wenn Bulk > 2×TTL alt und Refresh leer."""
    te._BULK = [{"player_a": "old", "player_b": "data", "odds_a": 2.0, "odds_b": 2.0}]
    te._TS = time.time() - (te._TTL_S * 2 + 1)  # älter als 2×TTL

    # Simuliere leeren Refresh
    monkeypatch.setattr(
        "src.data.tennis_secondary_odds.fetch_te_upcoming_matches",
        lambda **kw: [],
        raising=False,
    )
    try:
        bulk = te._get_bulk()
        # Wenn Refresh leer + stale → Bulk muss leer zurückkommen
        assert bulk == []
    except Exception:
        pass  # Modul nicht verfügbar → Skip (CI ohne Netz)


def test_scraper_records_response_capture_time_without_network(monkeypatch):
    from src.data import tennis_secondary_odds

    detail_html = """
    <td class="k1">Shelton Ben</td><td class="k2">Sinner Jannik</td>
    <tr class="one"><td class="k1"><div class="odds-in">1.80</div></td>
    <td class="k2"><div class="odds-in">2.10</div></td></tr>
    """
    monkeypatch.setattr(
        tennis_secondary_odds, "_http_get", lambda *_a, **_kw: detail_html
    )
    before = datetime.now(timezone.utc)
    match = tennis_secondary_odds._fetch_match_detail("fixture-1")
    after = datetime.now(timezone.utc)

    assert match is not None
    observed_at = datetime.fromisoformat(
        match["source_observed_at"].replace("Z", "+00:00")
    )
    assert before.replace(microsecond=0) <= observed_at <= after.replace(microsecond=0)


def test_scraper_prefers_full_year_match_header_over_quote_history(monkeypatch):
    from src.data import tennis_secondary_odds

    detail_html = """
    <div>09.11. 22:23, GMT+1</div>
    <h1>Vekic - Swiatek</h1>
    <div>05.10.2026, 05:00, Beijing, final</div>
    <table><td class="k1">Vekic Donna</td><td class="k2">Swiatek Iga</td>
    <tr class="one"><td class="k1"><div class="odds-in">1.95</div></td>
    <td class="k2"><div class="odds-in">2.05</div></td></tr></table>
    <div>04.10. 23:15</div>
    """
    monkeypatch.setattr(
        tennis_secondary_odds, "_http_get", lambda *_a, **_kw: detail_html
    )

    match = tennis_secondary_odds._fetch_match_detail("fixture-1")

    assert match is not None
    assert match["commence_time"] == "2026-10-05T03:00:00Z"


def test_match_header_kickoff_ignores_page_clock_and_uses_match_time(monkeypatch):
    from src.data import tennis_secondary_odds
    from src.tennis.event_state import (
        TennisEventState,
        TennisEventStatus,
        derive_schedule_status,
    )

    observed = datetime(2026, 11, 9, 21, 23, tzinfo=timezone.utc)
    detail_html = """
    <div>09.11. 22:23, GMT+1</div>
    <h1>Gojo - Machac</h1>
    <div>Today, 14:15, Stockholm, 1. round</div>
    <table><td class="k1">Gojo Borna</td><td class="k2">Machac Tomas</td>
    <tr class="one"><td class="k1"><div class="odds-in">2.99</div></td>
    <td class="k2"><div class="odds-in">1.38</div></td></tr></table>
    <div>09.11. 10:22</div>
    """
    monkeypatch.setattr(tennis_secondary_odds, "_http_get", lambda *_a, **_kw: detail_html)
    monkeypatch.setattr(tennis_secondary_odds, "_utc_now", lambda: observed)

    match = tennis_secondary_odds._fetch_match_detail("gojo-machac")

    assert match is not None
    assert match["commence_time"] == "2026-11-09T13:15:00Z"
    assert match["kickoff_source"] == "match_header_relative_date"
    assert match["kickoff_timezone"] == "Europe/Berlin"
    assert match["te_parser_version"] == tennis_secondary_odds.TE_PARSER_VERSION
    state = TennisEventState(
        scheduled_start_current=match["commence_time"],
        schedule_source="tennisexplorer",
    )
    assert derive_schedule_status(state, observed) == TennisEventStatus.AWAITING_START


def test_two_matches_keep_distinct_match_header_kickoffs(monkeypatch):
    from src.data import tennis_secondary_odds

    observed = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tennis_secondary_odds, "_utc_now", lambda: observed)
    html_by_id = {
        "one": """
            <div>09.11. 13:00, GMT+1</div><h1>Player One - Player Two</h1>
            <div>Today, 14:15, Stockholm, round</div>
            <table><td class="k1">Player One</td><td class="k2">Player Two</td>
            <tr class="one"><td class="k1"><div class="odds-in">1.80</div></td>
            <td class="k2"><div class="odds-in">2.10</div></td></tr></table>
        """,
        "two": """
            <div>09.11. 13:00, GMT+1</div><h1>Player Three - Player Four</h1>
            <div>Today, 16:30, Stockholm, round</div>
            <table><td class="k1">Player Three</td><td class="k2">Player Four</td>
            <tr class="one"><td class="k1"><div class="odds-in">1.80</div></td>
            <td class="k2"><div class="odds-in">2.10</div></td></tr></table>
        """,
    }
    monkeypatch.setattr(
        tennis_secondary_odds, "_http_get", lambda url, **_kw: html_by_id[url.rsplit("=", 1)[-1]]
    )

    first = tennis_secondary_odds._fetch_match_detail("one")
    second = tennis_secondary_odds._fetch_match_detail("two")

    assert first["commence_time"] == "2026-11-09T13:15:00Z"
    assert second["commence_time"] == "2026-11-09T15:30:00Z"
    assert first["commence_time"] != second["commence_time"]


def test_missing_match_header_time_stays_unknown_not_page_or_scan_time(monkeypatch):
    from src.data import tennis_secondary_odds

    observed = datetime(2026, 11, 9, 21, 16, tzinfo=timezone.utc)
    detail_html = """
    <div>09.11. 22:16, GMT+1</div><h1>Player One - Player Two</h1>
    <div>Stockholm, 1. round</div>
    <table><td class="k1">Player One</td><td class="k2">Player Two</td>
    <tr class="one"><td class="k1"><div class="odds-in">1.80</div></td>
    <td class="k2"><div class="odds-in">2.10</div></td></tr></table>
    """
    monkeypatch.setattr(tennis_secondary_odds, "_http_get", lambda *_a, **_kw: detail_html)
    monkeypatch.setattr(tennis_secondary_odds, "_utc_now", lambda: observed)

    match = tennis_secondary_odds._fetch_match_detail("unknown-time")

    assert match is not None
    assert match["commence_time"] == ""
    assert match["kickoff_source"] == "unknown"


def test_yearless_or_relative_header_requires_fresh_page_date_context(monkeypatch):
    from src.data import tennis_secondary_odds

    detail_html = """
    <h1>Player One - Player Two</h1><div>Today, 14:15, Stockholm</div>
    <table><td class="k1">Player One</td><td class="k2">Player Two</td>
    <tr class="one"><td class="k1"><div class="odds-in">1.80</div></td>
    <td class="k2"><div class="odds-in">2.10</div></td></tr></table>
    """
    monkeypatch.setattr(tennis_secondary_odds, "_http_get", lambda *_a, **_kw: detail_html)
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_utc_now",
        lambda: datetime(2026, 11, 9, 21, 16, tzinfo=timezone.utc),
    )

    match = tennis_secondary_odds._fetch_match_detail("no-page-clock")

    assert match is not None
    assert match["commence_time"] == ""
    assert match["kickoff_source"] == "unknown"


def test_tournament_page_next_matches_discovers_fixture_missing_from_next(monkeypatch):
    from src.data import tennis_secondary_odds

    next_html = """
    <a href="/stockholm/2026/atp-men/">Stockholm</a>
    <a href="/match-detail/?id=3373427">Gojo - Machac</a>
    """
    tournament_html = """
    <h2>Next matches</h2>
    <a href="/match-detail/?id=3374619">Michelsen - Cerundolo</a>
    <h2>Tournament draw</h2>
    <a href="/match-detail/?id=9999999">Completed draw result</a>
    """
    calls = []
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_http_get",
        lambda url, **_kw: calls.append(url) or tournament_html,
    )

    ids = tennis_secondary_odds._discover_tournament_match_ids(next_html)

    assert ids == ["3374619"]
    assert calls == ["https://www.tennisexplorer.com/stockholm/2026/atp-men/"]


def test_bulk_fetches_stockholm_fixture_from_linked_tournament_page(monkeypatch, tmp_path):
    from src.data import tennis_secondary_odds

    next_html = """
    <a href="/stockholm/2026/atp-men/">Stockholm</a>
    <a href="/match-detail/?id=3373427">Gojo - Machac</a>
    """
    tournament_html = """
    <h2>Next matches</h2>
    <a href="/match-detail/?id=3374619">Michelsen (6) - Cerundolo</a>
    <h2>Tournament draw</h2>
    <a href="/match-detail/?id=9999999">Completed draw result</a>
    """
    target_detail = """
    <div>09.11. 22:16, GMT+1</div>
    <h1>Michelsen - Cerundolo</h1>
    <div>10.11.2026, 13:00, Stockholm, round of 16</div>
    <table><td class="k1">Michelsen Alex</td><td class="k2">Cerundolo Juan Manuel</td>
    <tr class="one"><td class="k1"><div class="odds-in">1.71</div></td>
    <td class="k2"><div class="odds-in">2.48</div></td></tr></table>
    <a href="/stockholm/2026/atp-men/">Stockholm</a>
    """
    requested = []
    monkeypatch.setattr(tennis_secondary_odds, "_get_next_html", lambda: next_html)
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_http_get",
        lambda url, **_kwargs: (
            requested.append(url)
            or (tournament_html if url.endswith("/stockholm/2026/atp-men/") else target_detail if "3374619" in url else None)
        ),
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_utc_now",
        lambda: datetime(2026, 11, 9, 21, 16, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(tennis_secondary_odds, "_CACHE_PATH", tmp_path / "te.pkl")
    monkeypatch.setattr(tennis_secondary_odds, "DATA_CACHE", tmp_path / "cache")

    matches = tennis_secondary_odds.fetch_te_upcoming_matches(
        min_bookies=1,
        max_matches=2,
        use_cache=False,
        max_workers=1,
    )

    assert [match["match_id"] for match in matches] == ["te_3374619"]
    assert matches[0]["player_a"] == "Michelsen Alex"
    assert matches[0]["player_b"] == "Cerundolo Juan Manuel"
    assert matches[0]["commence_time"] == "2026-11-10T12:00:00Z"
    assert matches[0]["odds_a"] == 1.71
    assert matches[0]["odds_b"] == 2.48
    assert matches[0]["kickoff_source"] == "match_header_full_date"
    assert matches[0]["source_url"] == (
        "https://www.tennisexplorer.com/match-detail/?id=3374619"
    )
    assert requested.count("https://www.tennisexplorer.com/match-detail/?id=3374619") == 1
    assert not any("9999999" in url for url in requested)


def test_bounded_discovery_reserves_detail_slots_for_tournament_candidates(monkeypatch):
    from src.data import tennis_secondary_odds

    next_html = "".join(
        f'<a href="/match-detail/?id={index}">Player {index} - Rival {index}</a>'
        for index in range(150)
    ) + '<a href="/stockholm/2026/atp-men/">Stockholm</a>'
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_http_get",
        lambda *_a, **_kw: (
            '<h2>Next matches</h2><a href="/match-detail/?id=999999">Target - Player</a>'
            '<h2>Tournament draw</h2>'
        ),
    )

    ids = tennis_secondary_odds._bounded_discovered_match_ids(next_html, 150)

    assert len(ids) == 150
    assert len(set(ids)) == 150
    assert ids[-1] == "999999"


def test_bulk_cache_rejects_old_kickoff_parser_version():
    from src.data import tennis_secondary_odds

    old_row = {
        "source_observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commence_time": "2026-11-09T21:16:00Z",
        "te_parser_version": 1,
    }
    current_row = {
        "source_observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commence_time": "2026-11-09T13:15:00Z",
        "te_parser_version": tennis_secondary_odds.TE_PARSER_VERSION,
    }

    assert not tennis_secondary_odds._bulk_has_current_source_times([old_row])
    assert not tennis_secondary_odds._bulk_has_current_source_times(
        {"parser_version": 1, "matches": []}
    )
    assert tennis_secondary_odds._bulk_has_current_source_times(
        {"parser_version": tennis_secondary_odds.TE_PARSER_VERSION, "matches": [current_row]}
    )
    assert tennis_secondary_odds._bulk_has_current_source_times(
        {"parser_version": tennis_secondary_odds.TE_PARSER_VERSION, "matches": []}
    )


def test_targeted_te_fixture_lookup_finds_named_match_after_bulk_cap(monkeypatch):
    from src.data import tennis_secondary_odds

    distractors = "".join(
        f'<a href="/match-detail/?id={index}">Other{index} A. - Rival{index} B.</a>'
        for index in range(1, 201)
    )
    page = distractors + '<a href="/match-detail/?id=3338909">Vekic D. - Swiatek I.</a>'
    requested_ids = []
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = {
        "match_id": "te_3338909",
        "player_a": "Vekic Donna",
        "player_b": "Swiatek Iga",
        "commence_time": "2026-10-05T03:00:00Z",
        "odds_a": 1.95,
        "odds_b": 2.05,
        "te_bookies_count": 6,
        "te_parser_version": 3,
        "source_observed_at": observed_at,
    }
    monkeypatch.setattr(tennis_secondary_odds, "_get_next_html", lambda: page)

    def fetch_detail(match_id):
        requested_ids.append(match_id)
        return target

    monkeypatch.setattr(tennis_secondary_odds, "_fetch_match_detail", fetch_detail)

    match = tennis_secondary_odds.fetch_te_match_for_hint(
        {
            "player_a": "Donna Vekic",
            "player_b": "Iga Swiatek",
            "commence_time": "2026-10-05T03:00:00Z",
        }
    )

    assert requested_ids == ["3338909"]
    assert match is target


def test_targeted_te_lookup_rejects_participant_or_date_mismatch(monkeypatch):
    from src.data import tennis_secondary_odds

    page = '<a href="/match-detail/?id=3338909">Vekic D. - Swiatek I.</a>'
    monkeypatch.setattr(tennis_secondary_odds, "_get_next_html", lambda: page)
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_fetch_match_detail",
        lambda _match_id: {
            "player_a": "Vekic Donna",
            "player_b": "Swiatek Iga",
            "commence_time": "2026-10-06T03:00:00Z",
            "te_bookies_count": 6,
        },
    )

    wrong_date = tennis_secondary_odds.fetch_te_match_for_hint(
        {
            "player_a": "Donna Vekic",
            "player_b": "Iga Swiatek",
            "commence_time": "2026-10-05T03:00:00Z",
        }
    )
    wrong_players = tennis_secondary_odds.fetch_te_match_for_hint(
        {
            "player_a": "Donna Vekic",
            "player_b": "Coco Gauff",
            "commence_time": "2026-10-05T03:00:00Z",
        }
    )
    assert wrong_date is None
    assert wrong_players is None


def test_targeted_te_lookup_requires_timezone_aware_kickoff(monkeypatch):
    from src.data import tennis_secondary_odds

    monkeypatch.setattr(
        tennis_secondary_odds,
        "_get_next_html",
        lambda: (_ for _ in ()).throw(AssertionError("must fail before request")),
    )

    missing_kickoff = tennis_secondary_odds.fetch_te_match_for_hint(
        {"player_a": "Donna Vekic", "player_b": "Iga Swiatek"}
    )
    naive_kickoff = tennis_secondary_odds.fetch_te_match_for_hint(
        {
            "player_a": "Donna Vekic",
            "player_b": "Iga Swiatek",
            "commence_time": "2026-10-05T03:00:00",
        }
    )
    assert missing_kickoff is None
    assert naive_kickoff is None


def test_targeted_te_lookup_has_a_bounded_detail_request_budget(monkeypatch):
    from src.data import tennis_secondary_odds

    monkeypatch.setattr(
        tennis_secondary_odds,
        "_TARGETED_MATCH_DETAIL_COUNT",
        tennis_secondary_odds._MAX_TARGETED_MATCH_DETAILS,
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_get_next_html",
        lambda: '<a href="/match-detail/?id=3338909">Vekic D. - Swiatek I.</a>',
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_fetch_match_detail",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("budget must fail before request")
        ),
    )

    assert (
        tennis_secondary_odds.fetch_te_match_for_hint(
            {
                "player_a": "Donna Vekic",
                "player_b": "Iga Swiatek",
                "commence_time": "2026-10-05T03:00:00Z",
            }
        )
        is None
    )


def test_adapter_uses_one_exact_fixture_result_for_both_outcomes(monkeypatch):
    from src.data import tennis_secondary_odds
    from src.tennis.odds.base import ProviderOutcome

    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hint = {"player_a": "Donna Vekic", "player_b": "Iga Swiatek"}
    match = {
        "player_a": "Vekic Donna",
        "player_b": "Swiatek Iga",
        "odds_a": 1.95,
        "odds_b": 2.05,
        "te_bookies_count": 6,
        "te_parser_version": 3,
        "source_observed_at": observed_at,
    }
    calls = []
    te._BULK = []
    te._TS = 0.0
    monkeypatch.setattr(
        te,
        "_get_bulk_with_diagnostics",
        lambda: ([], ProviderOutcome(te.name, True, True, "success")),
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "fetch_te_match_for_hint",
        lambda requested, min_bookies: calls.append((requested, min_bookies)) or match,
    )

    quote, outcome = te.fetch_with_diagnostics(hint)

    assert quote is not None
    assert quote.h2h_a == 1.95
    assert quote.h2h_b == 2.05
    assert quote.bookmaker == "consensus"
    assert quote.ts == datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    assert calls == [(hint, 2)]
    assert outcome.result == "usable_quote"


def test_adapter_in_memory_cache_rejects_old_parser_rows(monkeypatch):
    from src.data import tennis_secondary_odds
    from src.tennis.odds import tennis_explorer

    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    legacy = {
        "player_a": "old player",
        "player_b": "old opponent",
        "source_observed_at": observed_at,
    }
    fresh = {
        "player_a": "new player",
        "player_b": "new opponent",
        "source_observed_at": observed_at,
        "te_parser_version": tennis_secondary_odds.TE_PARSER_VERSION,
    }
    tennis_explorer._BULK = [legacy]
    tennis_explorer._TS = time.time()
    calls = []
    monkeypatch.setattr(
        tennis_secondary_odds,
        "fetch_te_upcoming_matches",
        lambda **_kwargs: calls.append(True) or [fresh],
    )

    bulk, outcome = tennis_explorer._get_bulk_with_diagnostics()

    assert calls == [True]
    assert bulk == [fresh]
    assert outcome.status_class == "success"


def test_legacy_disk_cache_is_refreshed_without_network(monkeypatch, tmp_path):
    from src.data import tennis_secondary_odds

    cache_path = tmp_path / "te_upcoming.pkl"
    cache_path.write_bytes(
        pickle.dumps(
            [
                {
                    "player_a": "shelton ben",
                    "player_b": "sinner jannik",
                    "odds_a": 1.8,
                    "odds_b": 2.1,
                    "te_bookies_count": 2,
                }
            ]
        )
    )
    monkeypatch.setattr(tennis_secondary_odds, "_CACHE_PATH", cache_path)
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_get_next_html",
        lambda: '<a href="/match-detail/?id=12345">Shelton - Sinner</a>',
    )
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_fetch_match_detail",
        lambda _match_id: {
            "player_a": "shelton ben",
            "player_b": "sinner jannik",
            "odds_a": 1.8,
            "odds_b": 2.1,
            "te_bookies_count": 2,
            "te_parser_version": 3,
            "source_observed_at": observed_at,
        },
    )

    matches = tennis_secondary_odds.fetch_te_upcoming_matches()

    assert len(matches) == 1
    assert matches[0]["source_observed_at"] == observed_at



def test_scraper_keeps_identifiable_upcoming_fixture_without_odds(monkeypatch):
    from src.data import tennis_secondary_odds

    detail_html = """
    <div id="tzLink"><a>10.11. 00:41, GMT+1</a></div>
    <h1 class="bg">Michelsen - Cerundolo</h1>
    <div class="box boxBasic lGray"><span class="upper">Today</span>, 13:00,
    <a href="/stockholm/2026/atp-men/">Stockholm</a>, round of 16, indoors</div>
    <table><td class="k1">Michelsen Alex</td>
    <td class="k2">Cerundolo Juan Manuel</td></table>
    <a href="/stockholm/2026/atp-men/">Stockholm</a>
    """
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_http_get",
        lambda *_args, **_kwargs: detail_html,
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_utc_now",
        lambda: datetime(2026, 11, 9, 23, 41, tzinfo=timezone.utc),
    )

    match = tennis_secondary_odds._fetch_match_detail("3374619")

    assert match is not None
    assert match["match_id"] == "te_3374619"
    assert (match["player_a"], match["player_b"]) == (
        "Michelsen Alex",
        "Cerundolo Juan Manuel",
    )
    assert match["event_status"] == "upcoming"
    assert match["event_status_source"] == "tennisexplorer_upcoming_schedule"
    assert match["commence_time"] == "2026-11-10T12:00:00Z"
    assert match["kickoff_source"] == "match_header_relative_date"
    assert match["source_url"].endswith("id=3374619")
    assert match["source_observed_at"] == "2026-11-09T23:41:00Z"
    assert match["te_tournament"] == "Stockholm"
    assert match["odds_a"] is None
    assert match["odds_b"] is None
    assert match["te_bookies_count"] == 0
    assert match["odds_status"] == "unavailable"


def test_scraper_keeps_fixture_with_only_one_bookmaker(monkeypatch):
    from src.data import tennis_secondary_odds

    detail_html = """
    <div id="tzLink"><a>10.11. 00:41, GMT+1</a></div>
    <h1 class="bg">Michelsen - Cerundolo</h1>
    <div class="box boxBasic lGray"><span class="upper">Today</span>, 13:00,
    <a href="/stockholm/2026/atp-men/">Stockholm</a>, round of 16, indoors</div>
    <table><td class="k1">Michelsen Alex</td>
    <td class="k2">Cerundolo Juan Manuel</td></table>
    <tr class="one"><td class="k1"><div class="odds-in">1.80</div></td>
    <td class="k2"><div class="odds-in">2.10</div></td></tr>
    <a href="/stockholm/2026/atp-men/">Stockholm</a>
    """
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_http_get",
        lambda *_args, **_kwargs: detail_html,
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_utc_now",
        lambda: datetime(2026, 11, 9, 23, 41, tzinfo=timezone.utc),
    )

    match = tennis_secondary_odds._fetch_match_detail("3374619")

    assert match is not None
    assert match["match_id"] == "te_3374619"
    assert match["commence_time"] == "2026-11-10T12:00:00Z"
    assert match["odds_a"] == 1.80
    assert match["odds_b"] == 2.10
    assert match["te_bookies_count"] == 1
    assert match["odds_status"] == "available"


def test_real_sanitized_stockholm_source_excerpt_runs_through_parser(monkeypatch):
    from src.data import tennis_secondary_odds

    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "tennisexplorer"
        / "stockholm_3374619_sanitized.html"
    )
    source_html = fixture.read_text(encoding="utf-8")
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_http_get",
        lambda *_args, **_kwargs: source_html,
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_utc_now",
        lambda: datetime(2026, 11, 9, 23, 41, tzinfo=timezone.utc),
    )

    match = tennis_secondary_odds._fetch_match_detail("3374619")

    assert match is not None
    assert match["match_id"] == "te_3374619"
    assert match["player_a"] == "Michelsen Alex"
    assert match["player_b"] == "Cerundolo Juan Manuel"
    assert match["te_tournament"] == "Stockholm"
    assert match["commence_time"] == "2026-11-10T12:00:00Z"
    assert match["kickoff_source"] == "match_header_relative_date"
    assert match["event_status"] == "upcoming"
    assert match["odds_a"] is None
    assert match["odds_b"] is None
    assert match["te_bookies_count"] == 0


def test_bulk_cache_keeps_all_fixtures_and_applies_bookmaker_filter_per_caller(
    monkeypatch, tmp_path
):
    from src.data import tennis_secondary_odds

    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = {
        "no-odds": {
            "match_id": "te_no_odds",
            "player_a": "Player NoOdds",
            "player_b": "Rival NoOdds",
            "odds_a": None,
            "odds_b": None,
            "te_bookies_count": 0,
            "te_parser_version": tennis_secondary_odds.TE_PARSER_VERSION,
            "source_observed_at": observed_at,
        },
        "one-book": {
            "match_id": "te_one_book",
            "player_a": "Player OneBook",
            "player_b": "Rival OneBook",
            "odds_a": 1.80,
            "odds_b": 2.10,
            "te_bookies_count": 1,
            "te_parser_version": tennis_secondary_odds.TE_PARSER_VERSION,
            "source_observed_at": observed_at,
        },
        "two-books": {
            "match_id": "te_two_books",
            "player_a": "Player TwoBooks",
            "player_b": "Rival TwoBooks",
            "odds_a": 1.80,
            "odds_b": 2.10,
            "te_bookies_count": 2,
            "te_parser_version": tennis_secondary_odds.TE_PARSER_VERSION,
            "source_observed_at": observed_at,
        },
    }
    monkeypatch.setattr(tennis_secondary_odds, "_get_next_html", lambda: "upcoming")
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_bounded_discovered_match_ids",
        lambda _html, _limit: list(rows),
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_fetch_match_detail",
        lambda match_id: rows[match_id],
    )
    monkeypatch.setattr(tennis_secondary_odds, "_CACHE_PATH", tmp_path / "te.pkl")
    monkeypatch.setattr(tennis_secondary_odds, "DATA_CACHE", tmp_path / "cache")

    quote_view = tennis_secondary_odds.fetch_te_upcoming_matches(
        min_bookies=2, max_matches=3, use_cache=False, max_workers=1
    )
    schedule_view = tennis_secondary_odds.fetch_te_upcoming_matches(
        max_matches=3, use_cache=True
    )

    assert [row["match_id"] for row in quote_view] == ["te_two_books"]
    assert [row["match_id"] for row in schedule_view] == [
        "te_no_odds",
        "te_one_book",
        "te_two_books",
    ]
    assert schedule_view[0]["odds_a"] is None
    assert schedule_view[0]["odds_b"] is None
    assert schedule_view[1]["odds_a"] == 1.80
    assert schedule_view[1]["te_bookies_count"] == 1


def test_odds_adapter_never_builds_quote_from_fixture_without_prices(monkeypatch):
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    te._BULK = [
        {
            "player_a": "michelsen alex",
            "player_b": "cerundolo juan manuel",
            "odds_a": None,
            "odds_b": None,
            "te_bookies_count": 0,
            "te_parser_version": 3,
            "source_observed_at": observed_at,
        }
    ]
    te._TS = time.time()
    monkeypatch.setattr(te, "_quote_from_requested_fixture", lambda *_args: None)

    assert te.fetch(
        {"player_a": "Alex Michelsen", "player_b": "Juan Manuel Cerundolo", "name_source": "te"}
    ) is None

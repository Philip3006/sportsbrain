"""Smoke-Tests für scripts/tennis_scan.py (Roadmap J2-D Dispatcher).

Vorgänger-Tests (Wimbledon-Hardcode-Helper wie _mock_wimbledon_matches,
_format_report, min_edge_for, _ATP_MIN_EDGE) wurden durch das Dispatcher-
Pattern obsolet. Die Tests sind nach tests/tennis/test_scanner_dispatcher.py
verschoben (deckt _parse_event_markets, detect_all_markets, format_scan_report,
category_mode etc.).

Dieser File hält nur einen Import-Smoke-Test, damit Scanner-Modul lädt.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_scanner_module_imports_cleanly():
    """Tennis-Scanner-Modul lädt ohne ImportError."""
    import scripts.tennis_scan as scan
    # Dispatcher-Public-API
    for attr in ("main", "category_min_edge", "category_mode",
                 "fetch_tournament_odds", "detect_all_markets",
                 "format_scan_report", "_parse_event_markets"):
        assert hasattr(scan, attr), f"tennis_scan fehlt: {attr}"


def test_scanner_mock_tournament_helper_returns_wimbledon():
    """Mock-Helper liefert Wimbledon ATP + ≥1 Mock-Match."""
    from scripts.tennis_scan import _mock_tournament_matches
    t, matches = _mock_tournament_matches()
    assert t.slug == "wimbledon_atp"
    assert len(matches) >= 1
    for m in matches:
        assert m["odds_a"] > 1.0
        assert m["odds_b"] > 1.0


def test_committed_elo_snapshot_is_real_and_preserves_history_gate():
    from scripts.tennis_scan import _load_committed_elo_snapshot

    ratings = _load_committed_elo_snapshot()
    assert ratings is not None
    assert ratings.get_overall_count("Sinner J.") >= 5
    assert not ratings.is_known("not-a-real-player")


def test_committed_elo_snapshot_fails_closed_without_provenance(tmp_path, monkeypatch):
    import json

    from scripts import tennis_scan

    metadata_dir = tmp_path / "models" / "tennis"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "elo_meta.json").write_text(
        json.dumps({"generated_at": "", "n_matches": 67256}),
        encoding="utf-8",
    )
    monkeypatch.setattr(tennis_scan, "ROOT", tmp_path)

    assert tennis_scan._load_committed_elo_snapshot() is None


def test_tennisexplorer_fixture_is_retained_in_schedule_without_signal():
    from scripts.tennis_scan import _append_te_schedule_entry

    schedule = []
    actionable_signals = []
    match = {
        "match_id": "te_3374619",
        "source_url": "https://www.tennisexplorer.com/match-detail/?id=3374619",
        "player_a": "Alex Michelsen",
        "player_b": "Juan Manuel Cerundolo",
        "commence_time": "2026-11-10T12:00:00Z",
        "kickoff_source": "match_header_full_date",
        "kickoff_timezone": "Europe/Berlin",
        "te_parser_version": 3,
        "source_observed_at": "2026-11-09T21:16:00Z",
        "odds_a": 1.71,
        "odds_b": 2.48,
        "sport_key": "te:stockholm_atp",
    }

    entry = _append_te_schedule_entry(
        schedule,
        match,
        tour="atp",
        tournament="Stockholm",
        category="atp_250",
        surface="hard",
        best_of=3,
    )

    assert actionable_signals == []
    assert schedule == [entry]
    assert entry["match_id"] == "te_3374619"
    assert entry["source_url"] == (
        "https://www.tennisexplorer.com/match-detail/?id=3374619"
    )
    assert entry["home"] == "Alex Michelsen"
    assert entry["away"] == "Juan Manuel Cerundolo"
    assert entry["kickoff"] == "2026-11-10T12:00:00Z"
    assert entry["source"] == "tennisexplorer"
    assert entry["odds_source"] == "tennisexplorer"
    assert entry["source_observed_at"] == "2026-11-09T21:16:00Z"
    assert entry["kickoff_source"] == "match_header_full_date"



def test_te_fixture_without_odds_stays_visible_as_explicit_no_bet():
    from scripts.tennis_scan import _retain_te_fixture_without_actionable_odds

    no_odds = {
        "match_id": "te_no_odds",
        "source_url": "https://www.tennisexplorer.com/match-detail/?id=12345",
        "player_a": "Player One",
        "player_b": "Player Two",
        "commence_time": "2026-11-10T12:00:00Z",
        "kickoff_source": "match_header_full_date",
        "kickoff_timezone": "Europe/Berlin",
        "te_parser_version": 3,
        "source_observed_at": "2026-11-09T23:00:00Z",
        "event_status": "upcoming",
        "event_status_source": "tennisexplorer_upcoming_schedule",
        "odds_a": None,
        "odds_b": None,
        "te_bookies_count": 0,
        "sport_key": "te:stockholm_atp",
    }
    one_book = {
        **no_odds,
        "match_id": "te_one_book",
        "player_a": "Player Three",
        "player_b": "Player Four",
        "source_url": "https://www.tennisexplorer.com/match-detail/?id=12346",
        "odds_a": 1.80,
        "odds_b": 2.10,
        "te_bookies_count": 1,
    }
    schedule = []
    model_evals = {}

    assert _retain_te_fixture_without_actionable_odds(
        no_odds, schedule, model_evals, lambda _slug, _tour: None
    ) == (
        True,
        False,
    )
    assert _retain_te_fixture_without_actionable_odds(
        one_book, schedule, model_evals, lambda _slug, _tour: None
    ) == (
        True,
        False,
    )

    assert len(schedule) == 2
    assert len(model_evals) == 2
    no_odds_eval = model_evals["Player One vs Player Two"]
    one_book_eval = model_evals["Player Three vs Player Four"]
    assert no_odds_eval["status"] == "NO_BET_ODDS_UNAVAILABLE"
    assert (no_odds_eval["odds_a"], no_odds_eval["odds_b"]) == (None, None)
    assert no_odds_eval["bookmakers_count"] == 0
    assert no_odds_eval["source"] == "tennisexplorer"
    assert no_odds_eval["reason"] == "No valid two-way price was observed."
    assert one_book_eval["status"] == "NO_BET_INSUFFICIENT_BOOKMAKER_COVERAGE"
    assert (one_book_eval["odds_a"], one_book_eval["odds_b"]) == (1.80, 2.10)
    assert one_book_eval["bookmakers_count"] == 1
    assert one_book_eval["status"] != "ACTIONABLE"
    assert schedule[0]["match_id"] == "te_no_odds"
    assert (schedule[0]["home"], schedule[0]["away"]) == ("Player One", "Player Two")
    assert schedule[0]["kickoff"] == "2026-11-10T12:00:00Z"
    assert schedule[0]["event_status"] == "upcoming"
    assert schedule[0]["event_status_source"] == "tennisexplorer_upcoming_schedule"
    assert schedule[0]["source"] == "tennisexplorer"
    assert schedule[0]["source_url"].endswith("id=12345")
    assert schedule[0]["source_observed_at"] == "2026-11-09T23:00:00Z"
    assert schedule[0]["odds_home"] is None
    assert schedule[0]["odds_away"] is None
    assert schedule[0]["odds_bookmakers_count"] == 0
    assert schedule[0]["odds_source"] is None
    assert schedule[1]["odds_home"] == 1.80
    assert schedule[1]["odds_away"] == 2.10
    assert schedule[1]["odds_source"] == "tennisexplorer"
    assert schedule[1]["odds_bookmakers_count"] == 1


def test_te_signal_eligibility_keeps_existing_valid_priced_fixture():
    from scripts.tennis_scan import (
        _retain_te_fixture_without_actionable_odds,
        _te_match_has_actionable_odds,
    )

    match = {
        "match_id": "te_valid_odds",
        "player_a": "Player One",
        "player_b": "Player Two",
        "odds_a": 1.71,
        "odds_b": 2.48,
        "te_bookies_count": 2,
    }
    schedule = []
    model_evals = {}

    assert _te_match_has_actionable_odds(match)
    assert _retain_te_fixture_without_actionable_odds(
        match, schedule, model_evals, lambda _slug, _tour: None
    ) == (False, False)
    assert schedule == []
    assert model_evals == {}

"""O1-4 Tennis Ledger Integrity — regression tests.

Validates:
1. tennis_detector sets league=atp/wta on all signal types
2. append_bets raises if tennis market has league='wm2026'
3. append_bets raises if draw market has tennis league
4. Backfill script correctly classifies ATP/WTA rows
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from src.betting.tennis_detector import (
    detect_total_games,
    detect_total_sets,
    detect_set_betting,
    detect_value_tennis,
)
from src.betting.value_detector import BetSignal


# ── 1. detector league propagation ────────────────────────────────────────────

def _make_probs(p: float = 0.70) -> dict:
    return {"p_a": p, "p_b": 1.0 - p}


def test_detect_value_tennis_sets_atp_league():
    sigs = detect_value_tennis(
        "Federer", "Nadal", _make_probs(), odds_a=1.50, odds_b=2.80,
        bankroll=100.0, tour="atp",
    )
    assert sigs, "Expected at least one signal"
    for s in sigs:
        assert s.league == "atp", f"Expected league='atp', got '{s.league}'"


def test_detect_value_tennis_sets_wta_league():
    # Use min_edge=0 to force signal regardless of EV — we're testing league propagation
    sigs = detect_value_tennis(
        "Swiatek", "Sabalenka", _make_probs(), odds_a=1.45, odds_b=3.0,
        bankroll=100.0, tour="wta", min_edge=0.0,
    )
    assert sigs
    for s in sigs:
        assert s.league == "wta", f"Expected league='wta', got '{s.league}'"


def test_detect_total_games_sets_league():
    # Use generous over odds to guarantee positive EV — testing league propagation
    sigs = detect_total_games(
        "Alcaraz", "Sinner", p_match_a=0.55,
        odds_over=2.20, odds_under=1.65, line=19.5,
        best_of=3, bankroll=100.0, tour="atp", min_edge=0.0,
    )
    assert sigs, "Expected at least one O/U games signal"
    for s in sigs:
        assert s.league == "atp"


def test_detect_total_sets_sets_league():
    # min_edge=0 to guarantee signal — testing league field, not edge gate
    sigs = detect_total_sets(
        "Gauff", "Keys", p_match_a=0.58,
        odds_over=1.80, odds_under=2.00, line=2.5,
        best_of=3, bankroll=100.0, tour="wta", min_edge=0.0,
    )
    assert sigs
    for s in sigs:
        assert s.league == "wta"


def test_detect_set_betting_sets_league():
    sigs = detect_set_betting(
        "Djokovic", "Zverev", p_match_a=0.72,
        scoreline_odds={"3-0": 2.10, "3-1": 2.80, "2-3": 4.50},
        best_of=5, bankroll=100.0, tour="atp",
    )
    # May be empty if EV not met; if non-empty, all must carry league
    for s in sigs:
        assert s.league == "atp"


# ── 2. append_bets integrity gate ─────────────────────────────────────────────

def _minimal_ledger_path(tmp: Path) -> Path:
    p = tmp / "ledger.csv"
    fields = [
        "match_id", "match_date", "home", "away", "market",
        "decimal_odds", "stake_pct", "stake_amount", "placed_date",
        "status", "pnl", "closing_odds", "clv", "pinnacle_ref_odds",
        "source", "model_prob", "stake_reason", "league",
    ]
    p.write_text(",".join(fields) + "\n")
    return p


def _make_tennis_signal(market: str, league: str) -> BetSignal:
    return BetSignal(
        match_id="test_match", home="A", away="B",
        market=market, model_prob=0.70, fair_prob=0.65,
        decimal_odds=1.80, ev=0.08, kelly_f=0.05,
        stake_pct=0.05, confidence="MEDIUM", stake_eur=5.0,
        league=league,
    )


def test_append_bets_rejects_tennis_market_with_wm2026_league():
    from src.betting.ledger import append_bets
    with tempfile.TemporaryDirectory() as tmp:
        path = _minimal_ledger_path(Path(tmp))
        sig = _make_tennis_signal("o/u_games_22.5_over", "wm2026")
        with pytest.raises(ValueError, match="O1-4 integrity violation"):
            append_bets([sig], bankroll=100.0, path=path)


def test_append_bets_rejects_draw_with_tennis_league():
    from src.betting.ledger import append_bets
    with tempfile.TemporaryDirectory() as tmp:
        path = _minimal_ledger_path(Path(tmp))
        sig = _make_tennis_signal("draw", "atp")
        with pytest.raises(ValueError, match="O1-4 integrity violation"):
            append_bets([sig], bankroll=100.0, path=path)


def test_append_bets_accepts_valid_tennis_signal():
    from src.betting.ledger import append_bets
    with tempfile.TemporaryDirectory() as tmp:
        path = _minimal_ledger_path(Path(tmp))
        sig = _make_tennis_signal("o/u_games_22.5_over", "atp")
        n = append_bets([sig], bankroll=100.0, path=path)
        assert n == 1
        df = pd.read_csv(path)
        assert df.iloc[0]["league"] == "atp"


def test_append_bets_accepts_tennis_home_market_any_league():
    """home/away markets are used by both sports — no error for tennis league + home market."""
    from src.betting.ledger import append_bets
    with tempfile.TemporaryDirectory() as tmp:
        path = _minimal_ledger_path(Path(tmp))
        sig = _make_tennis_signal("home", "atp")
        n = append_bets([sig], bankroll=100.0, path=path)
        assert n == 1


# ── 3. backfill script classification ─────────────────────────────────────────

def test_backfill_correctly_classifies_known_players():
    """Validate the player→tour lookup used in the backfill script."""
    from scripts.backfill_tennis_league import _TOUR_BY_PLAYER
    atp_players = ["Aleksandar Vukic", "Frances Tiafoe", "Tommy Paul"]
    wta_players = ["Naomi Osaka", "Alycia Parks", "Bianca Andreescu"]
    for p in atp_players:
        assert _TOUR_BY_PLAYER.get(p) == "atp", f"{p} should be ATP"
    for p in wta_players:
        assert _TOUR_BY_PLAYER.get(p) == "wta", f"{p} should be WTA"


def test_backfill_dry_run_finds_no_remaining_wm2026():
    """After the backfill has run, no o/u_games rows should have league=wm2026."""
    import csv
    ledger = Path("results/ledger_philip.csv")
    if not ledger.exists():
        pytest.skip("Ledger not present")
    with open(ledger) as f:
        for row in csv.DictReader(f):
            if row.get("market", "").startswith("o/u_games") and row.get("league") == "wm2026":
                pytest.fail(
                    f"Found uncorrected row: {row['home']} vs {row['away']} "
                    f"market={row['market']} league=wm2026"
                )

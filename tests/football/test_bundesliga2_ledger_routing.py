"""Ledger-Guards: league-Feld persistiert, settle routet per league,
BetSignal.league default backfill für Legacy-Zeilen."""
import tempfile
from pathlib import Path

import pandas as pd

from src.betting.ledger import _load, _save, append_bets, _FIELDS
from src.betting.value_detector import BetSignal


def _mk_signal(match_id="test1", league="bl2", market="home") -> BetSignal:
    return BetSignal(
        match_id=match_id, home="Hamburg", away="Schalke 04", market=market,
        model_prob=0.55, fair_prob=0.50, decimal_odds=2.10, ev=0.15, kelly_f=0.08,
        stake_pct=0.05, confidence="MEDIUM", stake_eur=5.0, league=league,
    )


def test_league_field_in_schema():
    assert "league" in _FIELDS


def test_append_bets_persists_league():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ledger.csv"
        n = append_bets([_mk_signal(league="bl2")], bankroll=100.0, path=p, match_date="2026-08-15")
        assert n == 1
        df = _load(p)
        assert len(df) == 1
        assert df.iloc[0]["league"] == "bl2"


def test_legacy_ledger_backfills_league_wm2026():
    """Zeilen ohne league-Kolumne (alte Ledger) werden auf wm2026 gemapped."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "legacy.csv"
        # Simuliere alten Ledger ohne league-Kolumne
        legacy_fields = [f for f in _FIELDS if f != "league"]
        legacy = pd.DataFrame([{
            **{k: "" for k in legacy_fields},
            "match_id": "old1", "home": "Germany", "away": "France",
            "market": "home", "decimal_odds": "2.0", "stake_amount": "5.0",
            "status": "open",
        }])
        legacy.to_csv(p, index=False)
        df = _load(p)
        assert "league" in df.columns
        assert df.iloc[0]["league"] == "wm2026"


def test_signal_default_league_empty_string():
    """BetSignal.league default = '' → append_bets ersetzt durch 'wm2026' (legacy compat)."""
    s = BetSignal(
        match_id="x", home="A", away="B", market="home",
        model_prob=0.5, fair_prob=0.5, decimal_odds=2.0,
        ev=0.1, kelly_f=0.05, stake_pct=0.03, confidence="MEDIUM", stake_eur=3.0,
    )
    assert s.league == ""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "l.csv"
        append_bets([s], bankroll=100.0, path=p)
        df = _load(p)
        assert df.iloc[0]["league"] == "wm2026"  # backfill default

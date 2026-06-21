"""Tests for closing-odds resolver and CLV backfill.

The CLV-backfill bug history: leere CSV-Felder werden von pandas als NaN geladen,
und `str(NaN).strip()` ist truthy ("nan") — was den naiven `if str(clv).strip()`-Check
in jeder Zeile triggert und damit den kompletten Backfill stillschweigend skipt.
"""
from __future__ import annotations

import pandas as pd
import pytest

from scripts.update_closing_odds import _resolve_closing_odds, _backfill_clv
from src.betting.ledger import _save


@pytest.fixture
def synthetic_match() -> dict:
    return {
        "home_odds": 1.85, "draw_odds": 3.50, "away_odds": 4.20,
        "over_odds": 2.10, "under_odds": 1.78,
        "over15_odds": 1.35, "under15_odds": 3.00,
        "over35_odds": 3.40, "under35_odds": 1.32,
        "ah_home_odds": 1.80, "ah_away_odds": 2.05,
        "ah1_home_odds": 2.30, "ah1_away_odds": 1.65,
        "ah15_home_odds": 2.90, "ah15_away_odds": 1.42,
        "btts_yes_odds": 1.72, "btts_no_odds": 2.05,
        "dc_1x_odds": 1.25, "dc_x2_odds": 1.40, "dc_12_odds": 1.18,
        "totals_lines": {
            3.0: {"over": 2.55, "under": 1.55},
            2.5: {"over": 2.10, "under": 1.78},
        },
        "spreads": {
            -0.5: {"home": 1.80, "away": 2.05},
            0.5:  {"home": 1.30, "away": 3.40},
            1.5:  {"home": 1.10, "away": 6.50},
            -1.5: {"home": 2.90, "away": 1.42},
        },
    }


@pytest.mark.parametrize("market,expected", [
    ("home", 1.85), ("draw", 3.50), ("away", 4.20),
    ("o/u2.5_over", 2.10), ("o/u2.5_under", 1.78),
    ("o/u1.5_over", 1.35), ("o/u3.5_under", 1.32),
    ("o/u3.0_over", 2.55), ("o/u3.0_under", 1.55),
    ("ah-0.5_home", 1.80), ("ah+0.5_away", 2.05),
    ("ah-1.5_home", 2.90), ("ah+1.5_away", 1.42),
    ("ah+1.5_home", 1.10),
    ("ah+0.5_home", 1.30), ("ah+0.5", 1.30),
    ("btts_yes", 1.72), ("btts_no", 2.05),
    ("dc_1x", 1.25),
])
def test_resolver_known_markets(synthetic_match, market, expected):
    got = _resolve_closing_odds(synthetic_match, market)
    assert got == pytest.approx(expected)


@pytest.mark.parametrize("market", [
    "scorer_Harry Kane", "scorer_Julián Álvarez", "made_up_market", "ftts_home",
])
def test_resolver_unknown_markets(synthetic_match, market):
    assert _resolve_closing_odds(synthetic_match, market) is None


def test_resolver_skips_invalid_odds(synthetic_match):
    # Quote ≤ 1.0 wird verworfen (Sentinel-Wert wenn Bookie kein Markt anbietet)
    synthetic_match["home_odds"] = 0.0
    assert _resolve_closing_odds(synthetic_match, "home") is None
    synthetic_match["home_odds"] = 1.0
    assert _resolve_closing_odds(synthetic_match, "home") is None


def test_backfill_handles_nan_clv_from_pandas_load(tmp_path, monkeypatch):
    """Regression: leere CSV-Felder werden als NaN geladen.

    Vorheriger Bug: `str(NaN).strip()` → "nan" → truthy → continue → 0 Bets befüllt.
    Fix prüft via `pd.isna(v) or not str(v).strip()`.
    """
    ledger_path = tmp_path / "ledger.csv"
    df = pd.DataFrame([
        # settled, valid closing_odds, NaN clv → SOLLTE befüllt werden
        {"match_id": "m1", "match_date": "2026-06-15", "home": "A", "away": "B",
         "market": "home", "decimal_odds": "2.00", "stake_pct": "0.05", "stake_amount": "5",
         "placed_date": "2026-06-15", "status": "won", "pnl": "5", "closing_odds": "1.80",
         "clv": "", "pinnacle_ref_odds": "", "source": "test", "model_prob": "0.55"},
        # settled, NaN closing_odds → skip
        {"match_id": "m2", "match_date": "2026-06-15", "home": "C", "away": "D",
         "market": "draw", "decimal_odds": "3.50", "stake_pct": "0.03", "stake_amount": "3",
         "placed_date": "2026-06-15", "status": "lost", "pnl": "-3", "closing_odds": "",
         "clv": "", "pinnacle_ref_odds": "", "source": "test", "model_prob": "0.30"},
        # void status — muss auch berücksichtigt werden
        {"match_id": "m3", "match_date": "2026-06-15", "home": "E", "away": "F",
         "market": "away", "decimal_odds": "3.00", "stake_pct": "0.04", "stake_amount": "4",
         "placed_date": "2026-06-15", "status": "void", "pnl": "0", "closing_odds": "2.50",
         "clv": "", "pinnacle_ref_odds": "", "source": "test", "model_prob": "0.40"},
        # CLV bereits gesetzt → nicht überschreiben
        {"match_id": "m4", "match_date": "2026-06-15", "home": "G", "away": "H",
         "market": "home", "decimal_odds": "2.20", "stake_pct": "0.05", "stake_amount": "5",
         "placed_date": "2026-06-15", "status": "won", "pnl": "6", "closing_odds": "1.90",
         "clv": "0.1579", "pinnacle_ref_odds": "", "source": "test", "model_prob": "0.50"},
        # Pathologisch: closing 3× über bet_odds (Daten-Korruption) → skip
        {"match_id": "m5", "match_date": "2026-06-15", "home": "I", "away": "J",
         "market": "home", "decimal_odds": "1.80", "stake_pct": "0.05", "stake_amount": "5",
         "placed_date": "2026-06-15", "status": "lost", "pnl": "-5", "closing_odds": "23.00",
         "clv": "", "pinnacle_ref_odds": "", "source": "test", "model_prob": "0.55"},
    ])
    _save(df, ledger_path)

    # Reload simuliert produktiven Pfad: leere Felder werden NaN
    from src.betting.ledger import _load
    df_loaded = _load(ledger_path)
    assert pd.isna(df_loaded.at[0, "clv"]), "Sanity: leere CSV-Felder müssen als NaN geladen werden"

    # Monkeypatch _save um in tmp zu schreiben statt produktivem LEDGER_PATH
    import scripts.update_closing_odds as mod
    monkeypatch.setattr(mod, "_save", lambda d, _path=None: _save(d, ledger_path))

    _backfill_clv(df_loaded)

    df_after = _load(ledger_path)
    # m1: 2.00 / 1.80 - 1 = 0.1111
    assert df_after.at[0, "clv"] == "0.1111"
    # m2: keine closing_odds → bleibt leer
    assert pd.isna(df_after.at[1, "clv"]) or df_after.at[1, "clv"] == ""
    # m3: void mit closing_odds 2.50, bet 3.00 → CLV = 3/2.5 - 1 = 0.20
    assert df_after.at[2, "clv"] == "0.2000"
    # m4: bestehender CLV bleibt
    assert df_after.at[3, "clv"] == "0.1579"
    # m5: pathologisch → bleibt leer
    assert pd.isna(df_after.at[4, "clv"]) or df_after.at[4, "clv"] == ""

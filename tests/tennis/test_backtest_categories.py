"""Tests für Phase J2-B: categorize_series + Verdict-Gate + Report-Output."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.data.tennis_odds import categorize_series
from scripts.tennis_backtest import _category_verdict, write_j2_report


# ---- categorize_series --------------------------------------------------

@pytest.mark.parametrize("series,tour,expected", [
    ("Grand Slam", "ATP", "grand_slam"),
    ("Grand Slam", "WTA", "grand_slam"),
    ("Masters 1000", "ATP", "m1000"),
    ("ATP Finals", "ATP", "tour_final"),
    ("Masters Cup", "ATP", "tour_final"),
    ("ATP500", "ATP", "atp500"),
    ("International Gold", "ATP", "atp500"),
    ("ATP250", "ATP", "atp250"),
    ("International", "ATP", "atp250"),
    ("International", "WTA", "wta250"),    # Disambiguierung
    ("Premier Mandatory", "WTA", "wta1000"),
    ("Premier 5", "WTA", "wta1000"),
    ("Premier", "WTA", "wta500"),
    ("WTA1000", "WTA", "wta1000"),
    ("WTA250", "WTA", "wta250"),
    ("Tour Championships", "WTA", "tour_final"),
    ("WTA Finals", "WTA", "tour_final"),
])
def test_categorize_series_known(series, tour, expected):
    assert categorize_series(series, tour) == expected


def test_categorize_series_unknown_atp_default_atp250():
    assert categorize_series("Some Weird String", "ATP") == "atp250"


def test_categorize_series_unknown_wta_default_wta250():
    assert categorize_series("Some Weird String", "WTA") == "wta250"


def test_categorize_series_nan_handled():
    assert categorize_series(None, "ATP") == "atp250"  # type: ignore[arg-type]
    assert categorize_series(float("nan"), "WTA") == "wta250"  # type: ignore[arg-type]


# ---- _category_verdict --------------------------------------------------

def test_verdict_live_n_high_roi_high():
    assert _category_verdict(60, 0.04) == "LIVE"


def test_verdict_live_n_low_roi_high():
    assert _category_verdict(30, 0.06) == "LIVE"


def test_verdict_shadow_n_too_low():
    assert _category_verdict(20, 0.10) == "SHADOW"


def test_verdict_shadow_roi_too_low():
    assert _category_verdict(100, 0.01) == "SHADOW"


def test_verdict_blacklist_very_negative():
    assert _category_verdict(60, -0.08) == "BLACKLIST"


def test_verdict_blacklist_threshold_boundary():
    """ROI exakt -5% → BLACKLIST."""
    assert _category_verdict(100, -0.05) == "BLACKLIST"


def test_verdict_shadow_slightly_negative():
    assert _category_verdict(60, -0.02) == "SHADOW"


# ---- write_j2_report ----------------------------------------------------

def _fake_bets(n: int, category: str, tour: str, roi: float) -> pd.DataFrame:
    """Erzeugt synthetische Bet-Rows mit gewünschtem ROI."""
    stake = 10.0
    pnl_per = stake * roi  # vereinfacht
    return pd.DataFrame([
        {
            "category": category, "tour": tour, "surface": "hard",
            "stake": stake, "pnl": pnl_per, "won": 1 if pnl_per > 0 else 0,
            "model_prob": 0.55, "ev": 0.05,
        }
        for _ in range(n)
    ])


def test_write_j2_report_creates_file(tmp_path):
    df = pd.concat([
        _fake_bets(60, "grand_slam", "ATP", 0.04),
        _fake_bets(40, "m1000", "ATP", 0.06),
        _fake_bets(30, "atp250", "ATP", -0.07),
    ], ignore_index=True)
    out = tmp_path / "tennis_j2_report.md"
    write_j2_report(df, out)
    assert out.exists()
    text = out.read_text()
    # Verdict-Tabelle muss vorkommen
    assert "Gate-Verdict pro Kategorie" in text
    assert "grand_slam" in text
    assert "m1000" in text
    assert "atp250" in text
    # Verdict-Marker präsent
    assert "LIVE" in text or "SHADOW" in text or "BLACKLIST" in text
    # Surface × Kategorie auch
    assert "Surface × Kategorie" in text


def test_write_j2_report_handles_missing_category_column(tmp_path):
    """Slam-only-Backtest hat keine category-Spalte → Default 'grand_slam'."""
    df = pd.DataFrame([
        {"tour": "WTA", "surface": "grass", "stake": 10.0, "pnl": 0.5,
         "won": 1, "model_prob": 0.55, "ev": 0.05} for _ in range(30)
    ])
    out = tmp_path / "report.md"
    write_j2_report(df, out)
    text = out.read_text()
    assert "grand_slam" in text

"""Smoke-Tests für scripts/tennis_full_backtest.py (Roadmap J2-G)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.tennis_full_backtest import (
    _aggregate_calibration,
    _aggregate_match_winner,
    _parse_years,
    write_report,
)


def test_parse_years_range():
    r = _parse_years("2019-2025")
    assert list(r) == list(range(2019, 2026))


def test_parse_years_single():
    r = _parse_years("2024")
    assert list(r) == [2024]


def test_aggregate_match_winner_empty():
    assert _aggregate_match_winner(pd.DataFrame()) == []


def test_aggregate_match_winner_basic():
    df = pd.DataFrame({
        "category": ["grand_slam"] * 60,
        "tour": ["ATP"] * 60,
        "surface": ["grass"] * 60,
        "stake": [10.0] * 60,
        "pnl": [0.5] * 60,
        "won": [1] * 30 + [0] * 30,
        "model_prob": [0.55] * 60,
    })
    out = _aggregate_match_winner(df)
    assert len(out) == 1
    assert out[0]["n"] == 60
    assert out[0]["verdict"] in ("LIVE", "SHADOW", "BLACKLIST")


def test_aggregate_calibration_empty():
    assert _aggregate_calibration(pd.DataFrame()) == []


def test_aggregate_calibration_set_markets():
    df = pd.DataFrame({
        "category": ["grand_slam"] * 100,
        "tour": ["ATP"] * 100,
        "surface": ["grass"] * 100,
        "market": ["o_u_sets_2.5_over"] * 100,
        "model_p": [0.6] * 100,
        "actual": [1] * 60 + [0] * 40,
        "brier_term": [0.16] * 100,
    })
    out = _aggregate_calibration(df)
    assert len(out) == 1
    assert out[0]["n"] == 100
    assert out[0]["hit"] == pytest.approx(0.6, abs=1e-3)


def test_write_report_produces_all_sections(tmp_path: Path):
    mw = [{"category": "grand_slam", "tour": "ATP", "surface": "grass",
           "n": 50, "hit": 0.5, "roi": 0.04, "brier": 0.24, "verdict": "LIVE"}]
    set_agg = [{"category": "grand_slam", "tour": "ATP", "surface": "grass",
                "market": "o_u_sets_2.5_over", "n": 100, "hit": 0.55,
                "mean_p": 0.5, "brier": 0.24, "kalibriert": True}]
    game_agg = []
    out = tmp_path / "report.md"
    write_report(mw, set_agg, game_agg, out)
    text = out.read_text()
    assert "Sektion".lower() not in text  # german headings
    assert "## 1." in text and "## 2." in text and "## 3." in text and "## 4." in text
    assert "Match Winner" in text
    assert "grand_slam" in text
    assert "Keine Game-Score-Daten" in text


def test_write_report_with_game_data(tmp_path: Path):
    game_agg = [{"category": "grand_slam", "tour": "WTA", "surface": "hard",
                 "market": "o_u_games_21.5_over", "n": 40, "hit": 0.5,
                 "mean_p": 0.5, "brier": 0.25, "kalibriert": False}]
    out = tmp_path / "report.md"
    write_report([], [], game_agg, out)
    text = out.read_text()
    assert "o_u_games_21.5_over" in text


import pytest  # noqa: E402  (used by approx above)

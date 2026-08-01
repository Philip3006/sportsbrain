"""Tests for src/data/tennis_stats.py (J2-M)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.data.tennis_stats import (
    MatchStat,
    ServeAggregate,
    _empty_aggregate,
    _parse_row,
    _to_ta_slug,
    aggregate,
    fetch_aggregate,
    fetch_match_stats,
)


# ---------------------------------------------------------------------------
# _to_ta_slug
# ---------------------------------------------------------------------------

def test_slug_basic():
    assert _to_ta_slug("Carlos Alcaraz") == "CarlosAlcaraz"


def test_slug_strips_accents():
    assert _to_ta_slug("Stéfanos Tsitsipás") == "StefanosTsitsipas"


def test_slug_removes_special_chars():
    assert _to_ta_slug("J.J. Wolf") == "JJWolf"


def test_slug_empty_input():
    assert _to_ta_slug("") == ""


# ---------------------------------------------------------------------------
# _parse_row
# ---------------------------------------------------------------------------

def _make_row(**overrides) -> list:
    """Build a valid matchmx row (48 cols) with defaults for a Alcaraz W 6-3 6-0."""
    row: list = ["20260101", "Barcelona", "Clay", "A", "W", "2", "1", "", "R32",
                 "6-3 6-0", "3", "Alexander Bublik", "20", "", "", "R", "19970617",
                 "188", "KAZ", "0",
                 63, 3, 2, 44, 25, 17, 11, 7, 1, 2, 2, 1,
                 53, 33, 13, 9, 8, 4, 5, 2,
                 "", "", "", "2026-0101-x", "", "1", "1", "999"]
    assert len(row) == 48
    for k, v in overrides.items():
        idx = {"date": 0, "surface": 2, "result": 4, "score": 9, "opp": 11,
               "tpw_p": 20, "aces_p": 21, "dfs_p": 22,
               "tpw_o": 32, "aces_o": 33, "dfs_o": 34}[k]
        row[idx] = v
    return row


def test_parse_row_valid():
    row = _make_row()
    ms = _parse_row(row)
    assert ms is not None
    assert ms.date == "20260101"
    assert ms.surface == "clay"
    assert ms.result == "W"
    assert ms.opponent == "Alexander Bublik"
    assert ms.tpw_p == 63
    assert ms.aces_p == 3
    assert ms.dfs_p == 2
    assert ms.tpw_o == 53


def test_parse_row_skips_walkover():
    row = _make_row(score="W/O")
    assert _parse_row(row) is None


def test_parse_row_skips_short_row():
    assert _parse_row([1, 2, 3]) is None


def test_parse_row_skips_zero_points():
    row = _make_row(tpw_p=0)
    assert _parse_row(row) is None


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------

def _stat(date="20260101", surface="hard", result="W",
          tpw_p=60, tpw_o=40, aces=5, dfs=2) -> MatchStat:
    return MatchStat(date=date, surface=surface, result=result, opponent="X",
                     tpw_p=tpw_p, aces_p=aces, dfs_p=dfs,
                     tpw_o=tpw_o, aces_o=3, dfs_o=1)


def test_aggregate_empty_returns_neutral():
    agg = aggregate([])
    assert agg == _empty_aggregate()
    assert agg.dominance_rate == 0.5


def test_aggregate_computes_ratios():
    stats = [_stat(tpw_p=60, tpw_o=40, aces=5, dfs=2)] * 3
    agg = aggregate(stats)
    assert agg.n_matches == 3
    assert agg.dominance_rate == pytest.approx(60 / 100)
    assert agg.ace_rate == pytest.approx(5 / 60)
    assert agg.df_rate == pytest.approx(2 / 60)
    assert agg.win_rate == 1.0


def test_aggregate_surface_filter():
    stats = [
        _stat(surface="hard", tpw_p=60, tpw_o=40),
        _stat(surface="clay", tpw_p=30, tpw_o=70, result="L"),
    ]
    agg_hard = aggregate(stats, surface="hard")
    assert agg_hard.n_matches == 1
    assert agg_hard.dominance_rate == pytest.approx(0.6)

    agg_clay = aggregate(stats, surface="clay")
    assert agg_clay.n_matches == 1
    assert agg_clay.win_rate == 0.0


def test_aggregate_before_date_no_leakage():
    stats = [
        _stat(date="20260601", tpw_p=60, tpw_o=40),  # future
        _stat(date="20260101", tpw_p=50, tpw_o=50),  # past
    ]
    agg = aggregate(stats, before_date="20260301")
    assert agg.n_matches == 1
    assert agg.dominance_rate == pytest.approx(0.5)


def test_aggregate_last_n_window():
    stats = [_stat(tpw_p=100, tpw_o=0)] * 3 + [_stat(tpw_p=0, tpw_o=100, result="L")] * 3
    agg = aggregate(stats, last_n=3)
    # Only first 3 (100% dominance)
    assert agg.n_matches == 3
    assert agg.dominance_rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# fetch_match_stats / fetch_aggregate — with cache patched
# ---------------------------------------------------------------------------

def test_fetch_match_stats_returns_empty_on_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data.tennis_stats.CACHE_DIR", tmp_path)
    monkeypatch.setattr("src.data.tennis_stats._cache_path", lambda s: tmp_path / f"{s}.json")
    with patch("src.data.tennis_stats.requests.get") as mock_get:
        mock_get.return_value.status_code = 404
        stats = fetch_match_stats("Never Existed")
    assert stats == []


def test_fetch_aggregate_returns_neutral_on_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data.tennis_stats.CACHE_DIR", tmp_path)
    monkeypatch.setattr("src.data.tennis_stats._cache_path", lambda s: tmp_path / f"{s}.json")
    with patch("src.data.tennis_stats.requests.get") as mock_get:
        mock_get.return_value.status_code = 404
        agg = fetch_aggregate("Never Existed")
    assert agg.n_matches == 0
    assert agg.dominance_rate == 0.5

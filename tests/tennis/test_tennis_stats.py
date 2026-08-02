"""Tests for src/data/tennis_stats.py (J2-M).

Column-mapping (verified 2026-08-03 via TA `matchhead` array):
[20]=time, [21]=aces, [22]=dfs, [23]=svpts, [24]=firsts_in, [25]=firsts_won,
[26]=seconds_won, [27]=sv_games, [28]=bp_saved, [29]=bp_faced,
[30]=oaces, [31]=odfs, [32]=opts, [33]=ofirsts, [34]=ofwon, [35]=oswon,
[37]=osaved, [38]=ochances.
"""
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


def test_slug_basic():
    assert _to_ta_slug("Carlos Alcaraz") == "CarlosAlcaraz"


def test_slug_strips_accents():
    assert _to_ta_slug("Stéfanos Tsitsipás") == "StefanosTsitsipas"


def test_slug_removes_special_chars():
    assert _to_ta_slug("J.J. Wolf") == "JJWolf"


def test_slug_empty_input():
    assert _to_ta_slug("") == ""


def _make_row(**overrides) -> list:
    """Build a valid matchmx row (48 cols) — Alcaraz W 6-3 6-0 sample."""
    row: list = [
        "20260101", "Barcelona", "Clay", "A", "W", "2", "1", "", "R32",
        "6-3 6-0", "3", "Alexander Bublik", "20", "", "", "R", "19970617",
        "188", "KAZ", "0",
        90,      # [20] time
        3, 2,    # [21] aces, [22] dfs
        63,      # [23] svpts (serve pts served)
        44,      # [24] firsts_in
        30,      # [25] firsts_won
        12,      # [26] seconds_won
        9,       # [27] sv_games
        5, 7,    # [28] bp_saved, [29] bp_faced
        1, 3,    # [30] oaces, [31] odfs
        53,      # [32] opts (opp serve pts)
        33, 20, 10,  # [33] ofirsts, [34] ofwon, [35] oswon
        9,       # [36] ogames
        2, 5,    # [37] osaved, [38] ochances
        "2",     # [39] obackhand
        "", "", "", "2026-0101-x", "", "1", "1", "999",
    ]
    assert len(row) == 48
    idx_map = {"date": 0, "surface": 2, "result": 4, "score": 9, "opp": 11,
               "time_min": 20, "aces": 21, "dfs": 22, "svpts": 23,
               "firsts_in": 24, "firsts_won": 25, "seconds_won": 26,
               "bp_saved": 28, "bp_faced": 29,
               "o_svpts": 32, "o_bp_faced": 38}
    for k, v in overrides.items():
        row[idx_map[k]] = v
    return row


def test_parse_row_valid():
    ms = _parse_row(_make_row())
    assert ms is not None
    assert ms.date == "20260101"
    assert ms.surface == "clay"
    assert ms.result == "W"
    assert ms.opponent == "Alexander Bublik"
    assert ms.time_min == 90
    assert ms.svpts == 63
    assert ms.aces == 3
    assert ms.dfs == 2
    assert ms.firsts_in == 44
    assert ms.firsts_won == 30
    assert ms.seconds_won == 12
    assert ms.bp_saved == 5
    assert ms.bp_faced == 7
    assert ms.o_svpts == 53
    assert ms.o_firsts_won == 20
    assert ms.o_bp_faced == 5


def test_parse_row_skips_walkover():
    assert _parse_row(_make_row(score="W/O")) is None


def test_parse_row_skips_short_row():
    assert _parse_row([1, 2, 3]) is None


def test_parse_row_skips_zero_svpts():
    assert _parse_row(_make_row(svpts=0)) is None


def _stat(date="20260101", surface="hard", result="W", **kw) -> MatchStat:
    defaults = dict(
        time_min=90, svpts=60, firsts_in=40, firsts_won=28, seconds_won=12,
        aces=5, dfs=2, sv_games=10, bp_saved=3, bp_faced=5,
        o_aces=3, o_dfs=1, o_svpts=40, o_firsts_in=25, o_firsts_won=15,
        o_seconds_won=8, o_bp_saved=2, o_bp_faced=6,
    )
    defaults.update(kw)
    return MatchStat(date=date, surface=surface, result=result, opponent="X", **defaults)


def test_aggregate_empty_returns_neutral():
    agg = aggregate([])
    assert agg == _empty_aggregate()
    assert agg.dominance_rate == 0.5


def test_aggregate_computes_ratios():
    # svpts=60, o_svpts=40, firsts_won=28, seconds_won=12, o_firsts_won=15, o_seconds_won=8
    # tpw = 28+12+(40-15-8)=57; total=100 → dom=0.57
    stats = [_stat()] * 3
    agg = aggregate(stats)
    assert agg.n_matches == 3
    assert agg.dominance_rate == pytest.approx(0.57)
    assert agg.ace_rate == pytest.approx(5 / 60)
    assert agg.df_rate == pytest.approx(2 / 60)
    assert agg.first_serve_pct == pytest.approx(40 / 60)
    assert agg.first_serve_win_pct == pytest.approx(28 / 40)
    # seconds_in = 60 - 40 - 2 = 18 → 12/18
    assert agg.second_serve_win_pct == pytest.approx(12 / 18)
    assert agg.bp_save_pct == pytest.approx(3 / 5)
    assert agg.bp_conv_pct == pytest.approx(1 - 2 / 6)
    assert agg.win_rate == 1.0


def test_aggregate_surface_filter():
    stats = [_stat(surface="hard"), _stat(surface="clay", result="L")]
    assert aggregate(stats, surface="hard").n_matches == 1
    agg_clay = aggregate(stats, surface="clay")
    assert agg_clay.n_matches == 1 and agg_clay.win_rate == 0.0


def test_aggregate_before_date_no_leakage():
    stats = [_stat(date="20260601"), _stat(date="20260101")]
    assert aggregate(stats, before_date="20260301").n_matches == 1


def test_aggregate_last_n_window():
    stats = [_stat()] * 3 + [_stat(result="L")] * 3
    agg = aggregate(stats, last_n=3)
    assert agg.n_matches == 3 and agg.win_rate == 1.0


def test_fetch_match_stats_returns_empty_on_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data.tennis_stats.CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        "src.data.tennis_stats._cache_path",
        lambda s, tour: tmp_path / f"{s}_{tour}.json",
    )
    with patch("src.data.tennis_stats.retry_request", return_value=None):
        stats = fetch_match_stats("Never Existed")
    assert stats == []


def test_fetch_aggregate_returns_neutral_on_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data.tennis_stats.CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        "src.data.tennis_stats._cache_path",
        lambda s, tour: tmp_path / f"{s}_{tour}.json",
    )
    with patch("src.data.tennis_stats.retry_request", return_value=None):
        agg = fetch_aggregate("Never Existed")
    assert agg.n_matches == 0
    assert agg.dominance_rate == 0.5

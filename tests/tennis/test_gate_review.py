"""Tests für scripts/tennis_gate_review.py (Roadmap J2-F)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from scripts.tennis_gate_review import (
    _recommend,
    build_review,
    parse_backtest_md,
    write_review_md,
)


# ---- _recommend ------------------------------------------------------

def test_recommend_keep_if_too_few_bets():
    r = _recommend("atp250", "shadow", n=10, live_roi=0.10, bt={})
    assert "KEEP" in r and "n<30" in r


def test_recommend_blacklist_negative_roi():
    r = _recommend("atp250", "shadow", n=50, live_roi=-0.08, bt={"roi": 0.04})
    assert "BLACKLIST" in r


def test_recommend_promote_shadow_to_live():
    r = _recommend("m1000", "shadow", n=50, live_roi=0.05, bt={"roi": 0.04})
    assert "PROMOTE" in r


def test_recommend_keep_shadow_when_live_negative():
    r = _recommend("atp500", "shadow", n=50, live_roi=-0.02, bt={"roi": 0.04})
    assert "KEEP shadow" in r


def test_recommend_demote_live_to_shadow():
    r = _recommend("grand_slam", "live", n=50, live_roi=-0.02, bt={"roi": 0.06})
    assert "DEMOTE" in r


def test_recommend_keep_live_when_aligned():
    r = _recommend("grand_slam", "live", n=50, live_roi=0.05, bt={"roi": 0.04})
    assert "KEEP live" in r


def test_recommend_no_backtest_keeps_status():
    r = _recommend("atp250", "shadow", n=50, live_roi=0.05, bt={})
    assert "kein Backtest" in r


# ---- parse_backtest_md ----------------------------------------------

def test_parse_backtest_md_extracts_table(tmp_path):
    md = """# Tennis J2 Backtest

## Gate-Verdict pro Kategorie

| Kategorie | Tour | N | Hit% | ROI | Brier | Verdict |
|---|---|---:|---:|---:|---:|---|
| grand_slam | WTA | 216 | 52.1% | +8.5% | 0.247 | ✅ LIVE |
| m1000 | ATP | 80 | 48.0% | -2.1% | 0.255 | ⚠️ SHADOW |
| atp250 | ATP | 120 | 45.0% | -6.5% | 0.260 | 🚫 BLACKLIST |

## Surface × Kategorie
"""
    p = tmp_path / "bt.md"
    p.write_text(md)
    parsed = parse_backtest_md(p)
    assert "grand_slam" in parsed
    assert parsed["grand_slam"]["n"] == 216
    assert pytest.approx(parsed["grand_slam"]["roi"], abs=1e-4) == 0.085
    assert "LIVE" in parsed["grand_slam"]["verdict"]
    assert pytest.approx(parsed["m1000"]["roi"], abs=1e-4) == -0.021
    assert "BLACKLIST" in parsed["atp250"]["verdict"]


def test_parse_backtest_md_missing_file_returns_empty(tmp_path):
    parsed = parse_backtest_md(tmp_path / "nichtda.md")
    assert parsed == {}


# ---- build_review + write_review_md ---------------------------------

def test_write_review_md_creates_table(tmp_path):
    live = pd.DataFrame({
        "category": ["grand_slam"] * 35,
        "stake": [10.0] * 35,
        "pnl": [0.5] * 35,
        "status": ["won"] * 35,
        "market": ["home"] * 35,
    })
    review = build_review(live, {"grand_slam": {"roi": 0.04, "n": 100}})
    out = tmp_path / "review.md"
    write_review_md(review, out)
    assert out.exists()
    text = out.read_text()
    assert "Gate-Review" in text
    assert "grand_slam" in text
    assert "Empfehlung" in text


def test_build_review_handles_no_category_column():
    live = pd.DataFrame({
        "stake": [10.0] * 5,
        "pnl": [1.0] * 5,
        "status": ["won"] * 5,
        "market": ["home"] * 5,
    })
    review = build_review(live, {})
    # ohne category-Spalte: ein 'all'-Bucket
    assert "all" in review
    assert review["all"]["n"] == 5

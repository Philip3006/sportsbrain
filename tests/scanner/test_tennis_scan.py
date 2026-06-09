"""Unit tests for tennis scanner utilities."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from scripts.tennis_scan import (
    _mock_wimbledon_matches,
    _tennis_market_label,
    _format_report,
    min_edge_for,
    _ATP_MIN_EDGE,
)
from src.config import MIN_EDGE


# --- _mock_wimbledon_matches ---

def test_mock_matches_returns_three():
    matches = _mock_wimbledon_matches()
    assert len(matches) == 3


def test_mock_matches_all_have_required_keys():
    required = {"match_id", "player_a", "player_b", "odds_a", "odds_b", "tour"}
    for m in _mock_wimbledon_matches():
        assert required.issubset(m.keys()), f"Missing keys in {m['match_id']}"


def test_mock_matches_has_wta_entry():
    tours = {m["tour"] for m in _mock_wimbledon_matches()}
    assert "wta" in tours, "At least one WTA match expected in mock data"


def test_mock_matches_wta_has_bo3_calibrated_ah_odds():
    """WTA ah-1.5 odds must be ≥3.0 — BO3 straight-set win is ~23%, not ~47% as in ATP."""
    wta = [m for m in _mock_wimbledon_matches() if m["tour"] == "wta"]
    for m in wta:
        ah_a = m.get("ah_odds_a", 0.0)
        if ah_a > 1.0:
            assert ah_a >= 3.0, (
                f"WTA ah-1.5_a odds {ah_a} are ATP-calibrated (should be ≥3.0 for BO3)"
            )


def test_mock_matches_odds_are_valid():
    for m in _mock_wimbledon_matches():
        assert m["odds_a"] > 1.0, "odds_a must be > 1.0"
        assert m["odds_b"] > 1.0, "odds_b must be > 1.0"


# --- min_edge_for ---

def test_min_edge_for_atp_returns_high_bar():
    assert min_edge_for("atp") == _ATP_MIN_EDGE
    assert min_edge_for("atp") == 0.10


def test_min_edge_for_wta_returns_standard_bar():
    assert min_edge_for("wta") == MIN_EDGE


def test_min_edge_for_case_insensitive():
    assert min_edge_for("ATP") == min_edge_for("atp")
    assert min_edge_for("WTA") == min_edge_for("wta")


def test_min_edge_atp_strictly_higher_than_wta():
    assert min_edge_for("atp") > min_edge_for("wta")


# --- _tennis_market_label ---

def test_market_label_home():
    label = _tennis_market_label("home", "Alcaraz", "Djokovic")
    assert "Alcaraz" in label
    assert "Match Winner" in label or "Alcaraz" in label


def test_market_label_away():
    label = _tennis_market_label("away", "Alcaraz", "Djokovic")
    assert "Djokovic" in label


def test_market_label_ah_minus():
    label = _tennis_market_label("ah-1.5_a", "Swiatek", "Sabalenka")
    assert "Swiatek" in label
    assert "-1.5" in label or "3:0" in label or "2:0" in label or "AH" in label


def test_market_label_first_set():
    label = _tennis_market_label("first_set_b", "Swiatek", "Sabalenka")
    assert "Sabalenka" in label


def test_market_label_unknown_returns_key():
    label = _tennis_market_label("unknown_market", "A", "B")
    assert label == "unknown_market"


# --- _format_report ---

def test_format_report_no_signals():
    report = _format_report([], "2026-07-01", "grass", [])
    assert "No value bets" in report or "Keine" in report or "no value" in report.lower()


def test_format_report_with_signals_contains_match_info(tmp_path):
    from src.betting.value_detector import BetSignal
    sig = BetSignal(
        match_id="test",
        home="Alcaraz",
        away="Djokovic",
        market="home",
        model_prob=0.70,
        fair_prob=0.60,
        decimal_odds=1.80,
        ev=0.12,
        kelly_f=0.05,
        stake_pct=0.10,
        confidence="HIGH",
        stake_eur=12.0,
    )
    report = _format_report([sig], "2026-07-01", "grass", [("Alcaraz", 1700)])
    assert "Alcaraz" in report
    assert "Djokovic" in report
    assert "1.80" in report


def test_format_report_includes_top_elo():
    report = _format_report([], "2026-07-01", "grass", [("Alcaraz", 1695), ("Sinner", 1669)])
    assert "1695" in report or "Alcaraz" in report
    assert "Sinner" in report

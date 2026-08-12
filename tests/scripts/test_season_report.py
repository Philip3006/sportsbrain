"""O1-5 / O1-6 Season Report — unit tests.

Validates:
1. Tier separation: production / manual / shadow
2. New metrics: log_loss, ECE, max_drawdown, avg_odds, clv_coverage, clv_hit_rate
3. Calibration epoch split (pre/post 2026-08-12)
4. Rows without model_prob excluded from calibration metrics only
5. Shadow rows (challenger_atp) never appear in production metrics
6. Surface segmentation via signal_history map
7. CLI smoke: script exits 0 against real ledger
"""
from __future__ import annotations

import csv
import math
import tempfile
from pathlib import Path
from datetime import date

import pytest

# Direct imports from the module under test
from scripts.generate_season_report import (
    _bet_tier,
    _build_report,
    _compute_metrics,
    _ece,
    _logloss,
    _max_drawdown,
    _parse_date,
    _is_tennis_row,
    CALIBRATION_EPOCH,
)


# ── helpers ────────────────────────────────────────────────────────────────

def _row(status="won", source="value", league="atp", market="o/u_games_22.5_over",
         pnl=5.0, stake=10.0, model_prob=0.70, odds=1.80, clv="0.03",
         placed="2026-07-01", match_date="2026-07-02") -> dict:
    return {
        "status": status, "source": source, "league": league, "market": market,
        "pnl": str(pnl), "stake_amount": str(stake), "model_prob": str(model_prob),
        "decimal_odds": str(odds), "clv": clv,
        "placed_date": placed, "match_date": match_date,
        "home": "A", "away": "B", "stake_reason": "",
    }


# ── 1. Tier separation ─────────────────────────────────────────────────────

def test_bet_tier_value():
    assert _bet_tier(_row(source="value", league="atp")) == "value"


def test_bet_tier_manual():
    assert _bet_tier(_row(source="manual", league="atp")) == "manual"


def test_bet_tier_shadow_challenger():
    assert _bet_tier(_row(source="value", league="challenger_atp")) == "shadow"


def test_shadow_excluded_from_production():
    rows = [
        _row(source="value", league="atp", pnl=10.0, status="won"),
        _row(source="value", league="challenger_atp", pnl=50.0, status="won"),  # shadow
    ]
    report = _build_report(rows, since=None, until=None, sport="all", surface_map={})
    assert report["production"]["n_total"] == 1
    assert report["production"]["total_pnl"] == pytest.approx(10.0)
    assert report["shadow"]["n_total"] == 1


def test_manual_excluded_from_production():
    rows = [
        _row(source="value", pnl=10.0, status="won"),
        _row(source="manual", pnl=99.0, status="won"),
    ]
    report = _build_report(rows, since=None, until=None, sport="all", surface_map={})
    assert report["production"]["n_total"] == 1
    assert report["manual"]["n_total"] == 1
    assert report["production"]["total_pnl"] == pytest.approx(10.0)


# ── 2. New metrics ─────────────────────────────────────────────────────────

def test_log_loss_perfect():
    """Perfect prediction → near-zero log loss."""
    ll = _logloss(0.9999, 1.0)
    assert ll < 0.001


def test_log_loss_random():
    """Coin-flip probability → log(2) ≈ 0.693."""
    ll = _logloss(0.5, 1.0)
    assert abs(ll - math.log(2)) < 1e-5


def test_ece_perfectly_calibrated():
    """Perfectly calibrated predictions → ECE = 0."""
    pairs = [(0.3, 0.0), (0.3, 0.0), (0.3, 1.0), (0.7, 1.0), (0.7, 1.0), (0.7, 0.0)]
    ece = _ece(pairs)
    assert ece is not None and ece < 0.05


def test_ece_returns_none_on_empty():
    assert _ece([]) is None


def test_max_drawdown_simple():
    """P&L sequence: +5, +5, -15, +10 → peak=10, trough=-5 → DD=15."""
    assert _max_drawdown([5.0, 5.0, -15.0, 10.0]) == pytest.approx(15.0)


def test_max_drawdown_no_drawdown():
    """Monotonically increasing → drawdown = 0."""
    assert _max_drawdown([1.0, 2.0, 3.0]) == pytest.approx(0.0)


def test_compute_metrics_avg_odds():
    rows = [
        _row(status="won", odds=1.80, stake=10.0, pnl=8.0),
        _row(status="lost", odds=2.20, stake=10.0, pnl=-10.0),
    ]
    m = _compute_metrics(rows)
    assert m["avg_odds"] == pytest.approx(2.00, abs=0.01)


def test_compute_metrics_clv_coverage():
    rows = [
        _row(status="won", clv="0.03"),
        _row(status="lost", clv=""),       # no CLV
        _row(status="won", clv="0.0"),     # default = no reading
        _row(status="lost", clv="-0.02"),
    ]
    m = _compute_metrics(rows)
    assert m["clv_coverage_pct"] == pytest.approx(50.0)  # 2 of 4 have real CLV
    assert m["clv_hit_rate_pct"] == pytest.approx(50.0)  # 1 of 2 is positive


def test_compute_metrics_clv_hit_rate_all_positive():
    rows = [_row(status="won", clv="0.05"), _row(status="lost", clv="0.02")]
    m = _compute_metrics(rows)
    assert m["clv_hit_rate_pct"] == pytest.approx(100.0)


def test_compute_metrics_brier_and_logloss():
    rows = [
        _row(status="won", model_prob=0.70),
        _row(status="lost", model_prob=0.70),
    ]
    m = _compute_metrics(rows)
    assert m["brier_score"] is not None
    assert m["log_loss"] is not None
    # Brier for p=0.7 won: (0.7-1)^2=0.09; lost: (0.7-0)^2=0.49; avg=0.29
    assert m["brier_score"] == pytest.approx(0.29, abs=0.01)


def test_compute_metrics_empty():
    m = _compute_metrics([])
    assert m["n_total"] == 0


# ── 3. Calibration epoch split ─────────────────────────────────────────────

def test_epoch_boundary():
    assert CALIBRATION_EPOCH == date(2026, 8, 12)


def test_epoch_split_assigns_rows_correctly():
    rows = [
        _row(status="won", placed="2026-08-11", pnl=10.0),  # pre-epoch
        _row(status="won", placed="2026-08-12", pnl=20.0),  # post-epoch (epoch day included)
    ]
    report = _build_report(rows, since=None, until=None, sport="all", surface_map={})
    assert report["pre_epoch"]["n_total"] == 1
    assert report["pre_epoch"]["total_pnl"] == pytest.approx(10.0)
    assert report["post_epoch"]["n_total"] == 1
    assert report["post_epoch"]["total_pnl"] == pytest.approx(20.0)


def test_epoch_split_empty_post_epoch():
    rows = [_row(status="won", placed="2026-07-01")]
    report = _build_report(rows, since=None, until=None, sport="all", surface_map={})
    assert report["post_epoch"]["n_total"] == 0


# ── 4. Historical exclusions (no model_prob → excluded from calibration only) ──

def test_rows_without_model_prob_excluded_from_calibration():
    rows = [
        _row(status="won", model_prob=0.70, pnl=10.0),
        _row(status="won", model_prob=0.0, pnl=5.0),   # model_prob=0 → _f returns 0.0 → excluded
    ]
    rows[1]["model_prob"] = ""  # empty string
    m = _compute_metrics(rows)
    assert m["n_cal_readings"] == 1  # only the valid row
    assert m["n_total"] == 2  # both rows still count for P&L
    assert m["total_pnl"] == pytest.approx(15.0)


def test_n_no_model_prob_field_in_report():
    rows = [
        _row(status="won", model_prob=0.70),
        _row(status="lost", model_prob=0.0),
    ]
    rows[1]["model_prob"] = ""
    report = _build_report(rows, since=None, until=None, sport="all", surface_map={})
    assert report["n_no_model_prob"] == 1


# ── 5. Tennis row detection ────────────────────────────────────────────────

def test_is_tennis_row_by_market():
    assert _is_tennis_row({"market": "o/u_games_22.5_over", "source": "value", "league": "wm2026"})


def test_is_tennis_row_by_league():
    assert _is_tennis_row({"market": "home", "source": "value", "league": "atp"})


def test_is_not_tennis_row_football():
    assert not _is_tennis_row({"market": "draw", "source": "value", "league": "wm2026"})


# ── 6. Surface segmentation ────────────────────────────────────────────────

def test_surface_segmentation_uses_signal_history():
    surface_map = {"A|B|2026-07-01": "clay", "C|D|2026-07-01": "hard"}
    rows = [
        {**_row(status="won", pnl=5.0, league="atp"), "home": "A", "away": "B", "match_date": "2026-07-01"},
        {**_row(status="lost", pnl=-10.0, league="atp"), "home": "C", "away": "D", "match_date": "2026-07-01"},
        {**_row(status="won", pnl=3.0, league="atp"), "home": "E", "away": "F", "match_date": "2026-07-01"},  # unknown surface
    ]
    report = _build_report(rows, since=None, until=None, sport="all", surface_map=surface_map)
    by_surf = report["by_surface"]
    assert "clay" in by_surf
    assert "hard" in by_surf
    assert "unknown" in by_surf
    assert by_surf["clay"]["n"] == 1
    assert by_surf["hard"]["n"] == 1


# ── 7. CLI smoke ───────────────────────────────────────────────────────────

def test_cli_smoke_real_ledger():
    """Script should run to completion against the real ledger without raising."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "scripts/generate_season_report.py", "--sport", "all", "--season", "2026"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent.parent,
    )
    assert result.returncode == 0, f"Script failed:\n{result.stderr}"
    assert "production bets" in result.stdout.lower() or "production" in result.stdout.lower()


def test_cli_production_mode_smoke():
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "scripts/generate_season_report.py",
         "--sport", "tennis", "--output-mode", "production"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent.parent,
    )
    assert result.returncode == 0, f"Script failed:\n{result.stderr}"

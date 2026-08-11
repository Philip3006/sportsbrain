"""
Regression tests for src/betting/metrics.py.

Each test uses hand-computed expected values to catch regressions.
Tests cover: Brier Score, Log Loss, ROI/Yield, Drawdown, CLV,
missing fields, empty samples, segmentation, and invalid markets.
"""
from __future__ import annotations

import math

import pytest

from src.betting.metrics import (
    avg_odds,
    brier_score,
    calibration_buckets,
    clv_avg,
    clv_coverage,
    clv_hit_rate,
    compute_segment,
    log_loss,
    market_group,
    max_drawdown,
    roi,
    yield_metric,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _rec(
    status: str = "won",
    model_prob: float | None = 0.6,
    decimal_odds: float = 2.0,
    stake_amount: float = 10.0,
    pnl: float = 10.0,
    closing_odds: float | None = None,
    clv: float | None = None,
    market: str = "home",
    match_date: str = "2026-06-01",
) -> dict:
    return {
        "status": status,
        "model_prob": model_prob,
        "decimal_odds": decimal_odds,
        "stake_amount": stake_amount,
        "pnl": pnl,
        "closing_odds": closing_odds,
        "clv": clv,
        "market": market,
        "match_date": match_date,
    }


# ---------------------------------------------------------------------------
# Brier Score
# ---------------------------------------------------------------------------

class TestBrierScore:
    def test_known_example(self):
        # p=0.7, won → (0.7-1)^2 = 0.09
        # p=0.4, lost → (0.4-0)^2 = 0.16
        # mean = (0.09 + 0.16) / 2 = 0.125
        records = [
            _rec(status="won", model_prob=0.7, pnl=10.0),
            _rec(status="lost", model_prob=0.4, pnl=-10.0),
        ]
        result = brier_score(records)
        assert result.n == 2
        assert result.value == pytest.approx(0.125, abs=1e-6)

    def test_perfect_predictor(self):
        # p=1.0 is excluded (boundary), p=0.999 won → (0.001)^2 ≈ 0
        records = [_rec(status="won", model_prob=0.999, pnl=10.0)]
        result = brier_score(records)
        assert result.value == pytest.approx(0.000001, abs=1e-5)

    def test_push_excluded(self):
        records = [
            _rec(status="push", model_prob=0.6, pnl=0.0),
            _rec(status="void", model_prob=0.5, pnl=0.0),
        ]
        result = brier_score(records)
        assert result.value is None
        assert result.n == 0

    def test_missing_model_prob_excluded(self):
        records = [
            _rec(status="won", model_prob=None, pnl=10.0),
            _rec(status="lost", model_prob=0.5, pnl=-10.0),
        ]
        result = brier_score(records)
        assert result.n == 1  # only the lost record has valid prob

    def test_empty_sample(self):
        result = brier_score([])
        assert result.value is None
        assert result.n == 0
        assert result.note

    def test_boundary_prob_excluded(self):
        # model_prob=0.0 and 1.0 must be excluded (undefined Brier in log form
        # and unusual — signals a data quality issue)
        records = [
            _rec(status="won", model_prob=0.0, pnl=10.0),
            _rec(status="won", model_prob=1.0, pnl=10.0),
            _rec(status="lost", model_prob=0.5, pnl=-10.0),
        ]
        result = brier_score(records)
        assert result.n == 1


# ---------------------------------------------------------------------------
# Log Loss
# ---------------------------------------------------------------------------

class TestLogLoss:
    def test_known_example(self):
        # p=0.7, won:  -log(0.7)    = 0.35667...
        # p=0.3, lost: -log(1-0.3)  = -log(0.7) = 0.35667...
        # mean = 0.35667
        records = [
            _rec(status="won", model_prob=0.7, pnl=10.0),
            _rec(status="lost", model_prob=0.3, pnl=-10.0),
        ]
        result = log_loss(records)
        assert result.n == 2
        expected = -math.log(0.7)
        assert result.value == pytest.approx(expected, abs=1e-4)

    def test_push_excluded(self):
        records = [_rec(status="push", model_prob=0.5, pnl=0.0)]
        assert log_loss(records).value is None

    def test_empty_sample(self):
        assert log_loss([]).value is None

    def test_missing_prob_excluded(self):
        records = [
            _rec(status="won", model_prob=None, pnl=10.0),
            _rec(status="lost", model_prob=0.4, pnl=-10.0),
        ]
        result = log_loss(records)
        assert result.n == 1


# ---------------------------------------------------------------------------
# ROI / Yield
# ---------------------------------------------------------------------------

class TestROIYield:
    def test_known_roi(self):
        # 10 staked, pnl=2 → ROI = 2/10*100 = 20%
        records = [_rec(status="won", stake_amount=10.0, pnl=2.0)]
        result = roi(records)
        assert result.value == pytest.approx(20.0, abs=1e-4)
        assert result.n == 1

    def test_negative_roi(self):
        records = [
            _rec(status="won", stake_amount=10.0, pnl=5.0),
            _rec(status="lost", stake_amount=10.0, pnl=-10.0),
        ]
        result = roi(records)
        # staked=20, pnl=-5 → ROI=-25%
        assert result.value == pytest.approx(-25.0, abs=1e-4)

    def test_yield_equals_roi(self):
        records = [_rec(status="won", stake_amount=10.0, pnl=3.0)]
        assert roi(records).value == yield_metric(records).value

    def test_push_excluded_from_roi(self):
        # Push records excluded from ROI denominator (stake returned)
        records = [
            _rec(status="push", stake_amount=10.0, pnl=0.0),
            _rec(status="won", stake_amount=10.0, pnl=5.0),
        ]
        result = roi(records)
        assert result.n == 1
        assert result.value == pytest.approx(50.0, abs=1e-4)

    def test_zero_staked(self):
        records = [_rec(status="won", stake_amount=0.0, pnl=0.0)]
        result = roi(records)
        assert result.value is None
        assert "staked=0" in result.note

    def test_empty_sample(self):
        assert roi([]).value is None


# ---------------------------------------------------------------------------
# Max Drawdown
# ---------------------------------------------------------------------------

class TestMaxDrawdown:
    def test_known_drawdown(self):
        # sequence: +10, -20, +5 → peak=10, trough=-10 → max_dd=20
        records = [
            _rec(status="won", pnl=10.0, match_date="2026-06-01"),
            _rec(status="lost", pnl=-20.0, match_date="2026-06-02"),
            _rec(status="won", pnl=5.0, match_date="2026-06-03"),
        ]
        result = max_drawdown(records)
        assert result.value == pytest.approx(20.0, abs=1e-4)
        assert result.n == 3

    def test_no_drawdown(self):
        # Monotonically increasing → max_dd=0
        records = [
            _rec(status="won", pnl=5.0, match_date="2026-06-01"),
            _rec(status="won", pnl=5.0, match_date="2026-06-02"),
        ]
        result = max_drawdown(records)
        assert result.value == pytest.approx(0.0, abs=1e-4)

    def test_empty_sample(self):
        result = max_drawdown([])
        assert result.value is None

    def test_push_included_as_zero_pnl(self):
        records = [
            _rec(status="won", pnl=10.0, match_date="2026-06-01"),
            _rec(status="push", pnl=0.0, match_date="2026-06-02"),
            _rec(status="lost", pnl=-15.0, match_date="2026-06-03"),
        ]
        result = max_drawdown(records)
        assert result.value == pytest.approx(15.0, abs=1e-4)


# ---------------------------------------------------------------------------
# CLV metrics
# ---------------------------------------------------------------------------

class TestCLVMetrics:
    def test_clv_avg_known(self):
        # CLV = entry_odds - closing_odds (positive = beat close)
        records = [
            _rec(clv=0.10, closing_odds=1.90, status="won"),
            _rec(clv=-0.05, closing_odds=2.05, status="lost"),
            _rec(clv=0.20, closing_odds=1.80, status="won"),
        ]
        result = clv_avg(records)
        assert result.n == 3
        assert result.value == pytest.approx((0.10 - 0.05 + 0.20) / 3, abs=1e-6)

    def test_clv_hit_rate_known(self):
        records = [
            _rec(clv=0.10, status="won"),   # beat close
            _rec(clv=-0.05, status="lost"),  # missed close
            _rec(clv=0.20, status="won"),    # beat close
        ]
        result = clv_hit_rate(records)
        assert result.n == 3
        assert result.value == pytest.approx(2 / 3, abs=1e-6)

    def test_clv_missing(self):
        records = [_rec(clv=None, closing_odds=None, status="won")]
        r_avg = clv_avg(records)
        r_hit = clv_hit_rate(records)
        assert r_avg.value is None
        assert "metric unavailable" in r_avg.note
        assert r_hit.value is None
        assert "metric unavailable" in r_hit.note

    def test_clv_coverage(self):
        records = [
            _rec(closing_odds=1.90, clv=0.10, status="won"),
            _rec(closing_odds=None, clv=None, status="lost"),
            _rec(closing_odds=2.10, clv=-0.10, status="push"),
        ]
        result = clv_coverage(records)
        assert result.n == 3
        assert result.value == pytest.approx(2 / 3, abs=1e-6)

    def test_clv_coverage_empty(self):
        result = clv_coverage([])
        assert result.value is None


# ---------------------------------------------------------------------------
# Market grouping
# ---------------------------------------------------------------------------

class TestMarketGroup:
    @pytest.mark.parametrize("market,expected", [
        ("home", "match_winner"),
        ("away", "match_winner"),
        ("draw", "match_winner"),
        ("btts_yes", "btts"),
        ("btts_no", "btts"),
        ("o/u2.5_over", "ou_goals"),
        ("o/u3.0_under", "ou_goals"),
        ("o/u_games_22.5_over", "ou_games"),
        ("o/u_games_21.5_under", "ou_games"),
        ("ah+1.5_b", "asian_handicap"),
        ("ah+0.5_away", "asian_handicap"),
        ("scorer_Harry_Kane", "goalscorer"),
        ("goals_2_4", "goals_range"),
        ("unknown_market", "other"),
    ])
    def test_market_groups(self, market, expected):
        assert market_group(market) == expected


# ---------------------------------------------------------------------------
# compute_segment integration
# ---------------------------------------------------------------------------

class TestComputeSegment:
    def test_empty_segment(self):
        seg = compute_segment([])
        assert seg["n"] == 0
        assert seg["brier_score"]["value"] is None
        assert seg["roi_pct"]["value"] is None
        assert seg["total_pnl"] is None

    def test_basic_segment(self):
        records = [
            _rec(status="won", model_prob=0.7, stake_amount=10.0, pnl=10.0, decimal_odds=2.0),
            _rec(status="lost", model_prob=0.4, stake_amount=10.0, pnl=-10.0, decimal_odds=2.0),
        ]
        seg = compute_segment(records)
        assert seg["n"] == 2
        assert seg["n_binary"] == 2
        assert seg["brier_score"]["value"] == pytest.approx(0.125, abs=1e-4)
        assert seg["roi_pct"]["value"] == pytest.approx(0.0, abs=1e-4)
        assert seg["total_pnl"] == pytest.approx(0.0, abs=1e-4)

    def test_void_excluded_from_metrics(self):
        records = [
            _rec(status="void", model_prob=0.6, stake_amount=10.0, pnl=0.0),
        ]
        seg = compute_segment(records)
        # void is not settled so not in binary/settled groups
        assert seg["n_binary"] == 0
        assert seg["brier_score"]["value"] is None

    def test_non_comparable_market_does_not_break(self):
        # A market where model_prob is None must not crash — returns None metric
        records = [
            _rec(status="won", model_prob=None, stake_amount=10.0, pnl=10.0),
        ]
        seg = compute_segment(records)
        assert seg["brier_score"]["value"] is None
        assert seg["roi_pct"]["value"] is not None  # ROI still works without model_prob

    def test_segmentation_field_presence(self):
        records = [_rec(status="won", model_prob=0.6, stake_amount=10.0, pnl=10.0)]
        seg = compute_segment(records)
        required_keys = {
            "n", "n_settled", "n_binary", "total_pnl", "total_staked",
            "brier_score", "log_loss", "roi_pct", "yield_pct",
            "avg_odds", "max_drawdown", "clv_avg", "clv_hit_rate",
            "clv_coverage", "calibration",
        }
        assert required_keys.issubset(seg.keys())

    def test_report_with_clv(self):
        records = [
            _rec(status="won", model_prob=0.7, stake_amount=10.0, pnl=10.0,
                 closing_odds=1.85, clv=0.15),
            _rec(status="lost", model_prob=0.4, stake_amount=10.0, pnl=-10.0,
                 closing_odds=2.10, clv=-0.10),
        ]
        seg = compute_segment(records)
        assert seg["clv_avg"]["value"] is not None
        assert seg["clv_avg"]["n"] == 2
        assert seg["clv_hit_rate"]["value"] == pytest.approx(0.5, abs=1e-6)


# ---------------------------------------------------------------------------
# Calibration buckets
# ---------------------------------------------------------------------------

class TestCalibrationBuckets:
    def test_known_calibration(self):
        # All records in [0.6, 0.8) bucket, 2 won out of 3
        records = [
            _rec(status="won", model_prob=0.65),
            _rec(status="won", model_prob=0.70),
            _rec(status="lost", model_prob=0.72),
        ]
        buckets = calibration_buckets(records, n_buckets=5)
        assert len(buckets) >= 1
        # The bucket containing 0.65-0.72 should show ~2/3 observed freq
        b = buckets[0]
        assert b["n"] == 3
        assert b["observed_freq"] == pytest.approx(2 / 3, abs=1e-4)

    def test_empty_returns_empty(self):
        assert calibration_buckets([]) == []

    def test_push_excluded(self):
        records = [_rec(status="push", model_prob=0.6)]
        assert calibration_buckets(records) == []


# ---------------------------------------------------------------------------
# Avg odds
# ---------------------------------------------------------------------------

class TestAvgOdds:
    def test_known_avg(self):
        records = [
            _rec(status="won", decimal_odds=2.0),
            _rec(status="lost", decimal_odds=3.0),
        ]
        result = avg_odds(records)
        assert result.value == pytest.approx(2.5, abs=1e-4)
        assert result.n == 2

    def test_invalid_odds_excluded(self):
        records = [
            _rec(status="won", decimal_odds=0.0),
            _rec(status="won", decimal_odds=2.5),
        ]
        result = avg_odds(records)
        assert result.n == 1
        assert result.value == pytest.approx(2.5, abs=1e-4)

    def test_empty(self):
        assert avg_odds([]).value is None

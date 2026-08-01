"""J8-B12: sanity_ok — Overround + Odd-Bounds."""
from __future__ import annotations

from src.tennis.odds.base import sanity_ok


def test_normal_range_passes():
    assert sanity_ok(2.00, 2.00)  # overround 1.0
    assert sanity_ok(1.90, 2.10)


def test_overround_too_high_rejected():
    # implied = 1/1.5 + 1/1.5 = 1.33 > 1.15
    assert not sanity_ok(1.5, 1.5)


def test_overround_too_low_rejected():
    # 1/3 + 1/3 = 0.67 < 0.95 (arbitrage)
    assert not sanity_ok(3.0, 3.0)


def test_odd_bounds():
    assert not sanity_ok(1.00, 2.00)   # unter min_odd
    assert not sanity_ok(60.0, 1.02)   # über max_odd

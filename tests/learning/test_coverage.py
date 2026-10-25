from __future__ import annotations

from src.learning.coverage import build_coverage_report


def test_empty_coverage_is_safe_and_deterministic():
    first = build_coverage_report([], [])
    second = build_coverage_report([], [])
    assert first == second
    assert first["settlement_coverage_percent"] == 0.0
    assert first["median_settlement_lag_seconds"] is None

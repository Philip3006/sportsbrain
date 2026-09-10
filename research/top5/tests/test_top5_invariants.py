"""Top-5 — Generic invariant tests parameterized over registered leagues.

Ports the structural checks proven on BL1 v7 (569741b4a) to the parameterized
framework and runs them against every league that has a raw dataset built.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from research.top5 import config as league_cfg  # noqa: E402
from research.top5.core import canonical_market as market  # noqa: E402
from research.top5.core import partitions  # noqa: E402

TOP5 = ROOT / "research" / "top5"


def _available_leagues():
    """Leagues whose raw pickles exist on disk."""
    out = []
    for key, cfg in league_cfg.ALL.items():
        if cfg.raw_pkl(TOP5).exists() and cfg.full_pkl(TOP5).exists():
            out.append(key)
    return out


CLOSING_TOKENS = ("PSCH", "PSCD", "PSCA", "AvgCH", "AvgCD", "AvgCA",
                   "MaxCH", "MaxCD", "MaxCA", "B365CH", "B365CD", "B365CA",
                   "ps_close_home", "ps_close_draw", "ps_close_away")


@pytest.mark.parametrize("league_key", _available_leagues())
def test_calibration_no_outcomes_and_no_closing(league_key):
    cfg = league_cfg.get(league_key)
    df = partitions.load_calibration_predictions_only(cfg.raw_pkl(TOP5), cfg.calibration_season)
    outcome_leaks = [c for c in df.columns
                      if any(t in c.lower() for t in ("score", "goal", "result", "outcome", "pnl"))]
    assert not outcome_leaks, f"{league_key}: outcomes leaked in calibration loader: {outcome_leaks}"
    closing_leaks = [c for c in df.columns if c in CLOSING_TOKENS]
    assert not closing_leaks, f"{league_key}: closing leaked in calibration loader: {closing_leaks}"


@pytest.mark.parametrize("league_key", _available_leagues())
def test_holdout_no_outcomes_and_no_closing(league_key):
    cfg = league_cfg.get(league_key)
    df = partitions.load_holdout_schema_only(cfg.raw_pkl(TOP5), cfg.holdout_season)
    outcome_leaks = [c for c in df.columns
                      if any(t in c.lower() for t in ("score", "goal", "result", "outcome", "pnl"))]
    assert not outcome_leaks
    closing_leaks = [c for c in df.columns if c in CLOSING_TOKENS]
    assert not closing_leaks


@pytest.mark.parametrize("league_key", _available_leagues())
def test_coverage_helper_diagnostics_only(league_key):
    cfg = league_cfg.get(league_key)
    cov = partitions.holdout_closing_coverage_diagnostics(cfg.full_pkl(TOP5), cfg.holdout_season)
    allowed = {"source", "columns_present", "coverage", "n_covered", "n_missing", "n_total"}
    extra = set(cov.columns) - allowed
    assert not extra, f"{league_key}: coverage helper leaked columns: {extra}"
    for tok in CLOSING_TOKENS:
        assert tok not in cov.columns


@pytest.mark.parametrize("league_key", _available_leagues())
def test_missing_market_m5_m6_identical(league_key):
    """Deliberately blank canonical odds in a dev slice; assert M5 policy
    and M6-at-alpha-1 policy return identical rows and identical probabilities."""
    cfg = league_cfg.get(league_key)
    dev = partitions.load_development_with_market(
        cfg.raw_pkl(TOP5), cfg.full_pkl(TOP5), cfg.dev_seasons)
    slice_df = dev[dev["season"] == cfg.outer_folds[0]].sort_values(
        ["date", "home_team"], kind="stable").reset_index(drop=True).head(20).copy()
    for i in (3, 7, 12):
        slice_df.loc[i, "AvgH"] = np.nan
        slice_df.loc[i, "AvgD"] = np.nan
        slice_df.loc[i, "AvgA"] = np.nan
    kept_m5, p_m5 = market.apply_policy(slice_df)
    kept_m6, p_mkt = market.apply_policy(slice_df)
    dummy_p_elo = np.full_like(p_mkt, 1.0 / 3.0)
    p_m6_a1 = 1.0 * p_mkt + 0.0 * dummy_p_elo
    pd.testing.assert_frame_equal(
        kept_m5[["date", "home_team", "away_team"]].reset_index(drop=True),
        kept_m6[["date", "home_team", "away_team"]].reset_index(drop=True),
    )
    assert len(kept_m5) == 17
    diff = float(np.max(np.abs(p_m5 - p_m6_a1)))
    assert diff < 1e-12


@pytest.mark.parametrize("league_key", _available_leagues())
def test_holdout_absent_from_all_outputs(league_key):
    cfg = league_cfg.get(league_key)
    res = cfg.results_dir(TOP5)
    if not res.exists():
        pytest.skip(f"{league_key}: no results yet")
    for csv in res.glob("*.csv"):
        try:
            df = pd.read_csv(csv, dtype=str, low_memory=False)
        except pd.errors.EmptyDataError:
            continue
        if "season" in df.columns:
            assert cfg.holdout_season not in df["season"].values, (
                f"{league_key}: {csv.name} contains holdout season"
            )


if __name__ == "__main__":
    import traceback
    tests = [f for name, f in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        for lk in _available_leagues():
            try:
                t(lk)
                print(f"PASS  {t.__name__}[{lk}]")
            except Exception as e:
                print(f"FAIL  {t.__name__}[{lk}]: {e}")
                traceback.print_exc()

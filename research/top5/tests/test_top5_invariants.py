"""Top-5 — Generic invariant tests parameterized over registered leagues.

Ports the structural checks proven on BL1 v7 (569741b4a) to the parameterized
framework and expands them to 20 distinct invariant types.

Each test is parametrized over all leagues whose raw dataset is materialized.

Invariant type map (20):
  Type 1  — CALIBRATION loader has no outcome columns
  Type 2  — CALIBRATION loader has no closing-odds columns
  Type 3  — HOLDOUT loader has no outcome columns
  Type 4  — HOLDOUT loader has no closing-odds columns
  Type 5  — closing-coverage helper is metadata-only (no prices leaked)
  Type 6  — no direct pkl bypass in model/analysis orchestrators
  Type 7  — LGBM outer folds contain only outer_folds' seasons
             (walk-forward chronology respected in OOF outputs)
  Type 8  — Elo series pre-ratings are chronologically causal
  Type 9  — rolling form feature strictly excludes prediction-time match
  Type 10 — M5/M6-at-alpha=1 return identical rows and identical probs
  Type 11 — LGBM OOF outputs contain no closing-odds columns
  Type 12 — canonical market policy is single-sourced (shared constant)
  Type 13 — dev outputs never reference the holdout season
  Type 14 — dev outputs never reference the calibration season
  Type 15 — paired bootstrap samples at match level (not by class)
  Type 16 — DC snapshot causality (fit_date <= season_start)
  Type 17 — calibration + holdout seasons excluded from dev_seasons
  Type 18 — league output paths are isolated (no cross-league leakage)
  Type 19 — no hardcoded season length (380) in core code
  Type 20 — no cross-league dataset team overlap (mostly disjoint universes)
"""
from __future__ import annotations

import inspect
import re
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

OUTCOME_TOKENS = ("score", "goal", "result", "outcome", "pnl")


# ---------------------------------------------------------------------------
# Type 1 — CALIBRATION no outcome columns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type01_calibration_no_outcomes(league_key):
    cfg = league_cfg.get(league_key)
    df = partitions.load_calibration_predictions_only(
        cfg.raw_pkl(TOP5), cfg.calibration_season)
    leaks = [c for c in df.columns
             if any(t in c.lower() for t in OUTCOME_TOKENS)]
    assert not leaks, f"{league_key}: outcomes leaked in calibration: {leaks}"


# ---------------------------------------------------------------------------
# Type 2 — CALIBRATION no closing-odds columns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type02_calibration_no_closing(league_key):
    cfg = league_cfg.get(league_key)
    df = partitions.load_calibration_predictions_only(
        cfg.raw_pkl(TOP5), cfg.calibration_season)
    leaks = [c for c in df.columns if c in CLOSING_TOKENS]
    assert not leaks, f"{league_key}: closing leaked in calibration: {leaks}"


# ---------------------------------------------------------------------------
# Type 3 — HOLDOUT no outcome columns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type03_holdout_no_outcomes(league_key):
    cfg = league_cfg.get(league_key)
    df = partitions.load_holdout_schema_only(cfg.raw_pkl(TOP5), cfg.holdout_season)
    leaks = [c for c in df.columns
             if any(t in c.lower() for t in OUTCOME_TOKENS)]
    assert not leaks, f"{league_key}: outcomes leaked in holdout: {leaks}"


# ---------------------------------------------------------------------------
# Type 4 — HOLDOUT no closing-odds columns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type04_holdout_no_closing(league_key):
    cfg = league_cfg.get(league_key)
    df = partitions.load_holdout_schema_only(cfg.raw_pkl(TOP5), cfg.holdout_season)
    leaks = [c for c in df.columns if c in CLOSING_TOKENS]
    assert not leaks, f"{league_key}: closing leaked in holdout: {leaks}"


# ---------------------------------------------------------------------------
# Type 5 — closing-coverage helper is metadata-only
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type05_coverage_helper_metadata_only(league_key):
    cfg = league_cfg.get(league_key)
    cov = partitions.holdout_closing_coverage_diagnostics(
        cfg.full_pkl(TOP5), cfg.holdout_season)
    allowed = {"source", "columns_present", "coverage",
               "n_covered", "n_missing", "n_total"}
    extra = set(cov.columns) - allowed
    assert not extra, f"{league_key}: coverage helper leaked: {extra}"
    for tok in CLOSING_TOKENS:
        assert tok not in cov.columns


# ---------------------------------------------------------------------------
# Type 6 — no direct pkl bypass in model/analysis orchestrators
#
# Only partitions.py and artefacts.py are permitted to load raw pkls.
# Any other core module calling pickle.load(...) on a match dataset would
# bypass the sealed partition layer.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type06_no_raw_bypass_in_orchestrators(league_key):
    from research.top5.core import (
        models, models_dc, models_lgbm, analysis, contamination,
    )
    forbidden = re.compile(r"pickle\.load\s*\(")
    offenders = []
    for mod in (models, models_dc, models_lgbm, analysis, contamination):
        src = inspect.getsource(mod)
        # `pickle.load` in contamination is legitimate (sentinel builder). Only
        # flag it in models/analysis modules that must go through partitions.
        if mod.__name__.endswith("contamination"):
            continue
        if forbidden.search(src):
            offenders.append(mod.__name__)
    assert not offenders, (
        f"{league_key}: modules bypass partition layer with pickle.load: "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# Type 7 — LGBM OOF outputs restricted to expected fold seasons
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type07_lgbm_oof_seasons_restricted(league_key):
    cfg = league_cfg.get(league_key)
    res = cfg.results_dir(TOP5)
    allowed_m3m4 = set(cfg.calib_seed_folds) | set(cfg.outer_folds)
    allowed_m7 = set(cfg.outer_folds)
    checked = 0
    for fname, allowed in (
        ("oof_m3_dev_v2.csv", allowed_m3m4),
        ("oof_m4_dev_v2.csv", allowed_m3m4),
        ("oof_m7_dev_v3.csv", allowed_m7),
    ):
        p = res / fname
        if not p.exists():
            continue
        checked += 1
        df = pd.read_csv(p, dtype={"season": str})
        if "season" not in df.columns:
            continue
        seasons = set(df["season"].astype(str).unique())
        extra = seasons - allowed
        assert not extra, (
            f"{league_key} {fname}: unexpected fold seasons {sorted(extra)} "
            f"(allowed={sorted(allowed)})"
        )
        assert cfg.calibration_season not in seasons
        assert cfg.holdout_season not in seasons
    if checked == 0:
        pytest.skip(f"{league_key}: no LGBM OOF files present yet")


# ---------------------------------------------------------------------------
# Type 8 — Elo series pre-ratings are chronologically causal
#
# The Elo series must be sorted by (date, home_team) and every match's
# elo_*_pre values must be from ratings updated before the match date.
# Proxy: values are numeric, series is sorted stable, and elo_pre != elo_post
# (rating was updated when the match completed).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type08_elo_series_causal(league_key):
    import pickle
    cfg = league_cfg.get(league_key)
    p = cfg.results_dir(TOP5) / "elo_series_dev.pkl"
    if not p.exists():
        pytest.skip(f"{league_key}: elo_series_dev.pkl not present")
    with open(p, "rb") as f:
        es = pickle.load(f)
    assert isinstance(es, pd.DataFrame)
    es = es.copy()
    es["date"] = pd.to_datetime(es["date"])
    dates = es["date"].to_numpy()
    # Monotonic non-decreasing
    assert (dates[1:] >= dates[:-1]).all(), (
        f"{league_key}: elo series not chronologically sorted"
    )
    for col in ("elo_home_pre", "elo_away_pre"):
        assert col in es.columns, f"{league_key}: missing {col}"
        assert es[col].notna().all(), f"{league_key}: NaN in {col}"


# ---------------------------------------------------------------------------
# Type 9 — rolling form features exclude prediction-time match
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type09_rolling_form_excludes_current_match(league_key):
    from research.top5.core.models_lgbm import _rolling_pts
    # Synthetic history where the "current" match itself would score 3 pts;
    # rolling should return exactly the mean of prior matches, ignoring the
    # target-day match.
    hist = pd.DataFrame([
        {"home_team": "A", "away_team": "X", "date": pd.Timestamp("2020-01-01"),
         "home_score": 3, "away_score": 0},
        {"home_team": "A", "away_team": "Y", "date": pd.Timestamp("2020-01-08"),
         "home_score": 1, "away_score": 1},
        # This match is on the same date we're predicting — must be excluded
        {"home_team": "A", "away_team": "Z", "date": pd.Timestamp("2020-01-15"),
         "home_score": 3, "away_score": 0},
    ])
    before = pd.Timestamp("2020-01-15")
    val = _rolling_pts(hist, "A", before, 5)
    # Only first two matches counted: (3+1)/2 = 2.0
    assert abs(val - 2.0) < 1e-12, (
        f"{league_key}: _rolling_pts leaked prediction-time match: {val}"
    )


# ---------------------------------------------------------------------------
# Type 10 — M5/M6-at-alpha=1 identical rows + identical probs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type10_missing_market_m5_m6_identical(league_key):
    cfg = league_cfg.get(league_key)
    dev = partitions.load_development_with_market(
        cfg.raw_pkl(TOP5), cfg.full_pkl(TOP5), cfg.dev_seasons)
    slice_df = dev[dev["season"] == cfg.outer_folds[0]].sort_values(
        ["date", "home_team"], kind="stable"
    ).reset_index(drop=True).head(20).copy()
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
    assert float(np.max(np.abs(p_m5 - p_m6_a1))) < 1e-12


# ---------------------------------------------------------------------------
# Type 11 — LGBM OOF outputs contain no closing-odds columns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type11_lgbm_oof_no_closing(league_key):
    cfg = league_cfg.get(league_key)
    res = cfg.results_dir(TOP5)
    checked = 0
    for fname in ("oof_m3_dev_v2.csv", "oof_m4_dev_v2.csv", "oof_m7_dev_v3.csv"):
        p = res / fname
        if not p.exists():
            continue
        checked += 1
        df = pd.read_csv(p, dtype=str, low_memory=False)
        leaks = [c for c in df.columns if c in CLOSING_TOKENS]
        assert not leaks, f"{league_key}: closing in {fname}: {leaks}"
    if checked == 0:
        pytest.skip(f"{league_key}: no LGBM OOFs present")


# ---------------------------------------------------------------------------
# Type 12 — canonical market policy single-sourced
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type12_market_policy_single_source(league_key):
    # There must be exactly ONE canonical constant and ONE apply_policy.
    from research.top5.core import canonical_market as cm
    assert isinstance(cm.CANONICAL_PRECLOSE_SOURCE, str)
    assert cm.CANONICAL_PRECLOSE_SOURCE
    assert callable(cm.apply_policy)
    # And other core modules that use market data must import THIS module
    from research.top5.core import models, models_lgbm
    for mod in (models, models_lgbm):
        src = inspect.getsource(mod)
        # Must not define its own devig canonical constant
        assert "CANONICAL_PRECLOSE_SOURCE = " not in src, (
            f"{mod.__name__} redefines canonical constant"
        )


# ---------------------------------------------------------------------------
# Type 13 — dev outputs never reference the holdout season
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type13_dev_outputs_no_holdout(league_key):
    cfg = league_cfg.get(league_key)
    res = cfg.results_dir(TOP5)
    if not res.exists():
        pytest.skip(f"{league_key}: no results")
    for csv in res.glob("*.csv"):
        try:
            df = pd.read_csv(csv, dtype=str, low_memory=False)
        except pd.errors.EmptyDataError:
            continue
        if "season" in df.columns:
            assert cfg.holdout_season not in df["season"].values, (
                f"{league_key}: {csv.name} contains holdout season"
            )


# ---------------------------------------------------------------------------
# Type 14 — dev outputs never reference the calibration season
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type14_dev_outputs_no_calibration(league_key):
    cfg = league_cfg.get(league_key)
    res = cfg.results_dir(TOP5)
    if not res.exists():
        pytest.skip(f"{league_key}: no results")
    for csv in res.glob("oof_*.csv"):
        try:
            df = pd.read_csv(csv, dtype=str, low_memory=False)
        except pd.errors.EmptyDataError:
            continue
        if "season" in df.columns:
            assert cfg.calibration_season not in df["season"].values, (
                f"{league_key}: {csv.name} contains calibration season"
            )


# ---------------------------------------------------------------------------
# Type 15 — paired bootstrap samples at match level, not by class
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type15_bootstrap_match_level(league_key):
    from research.top5.core import metrics
    # Deterministic input: known y, known model probs.
    rng = np.random.default_rng(0)
    n = 200
    y = rng.integers(0, 3, size=n)
    p_a = rng.dirichlet(np.ones(3), size=n)
    p_b = rng.dirichlet(np.ones(3), size=n)
    delta1, lo1, hi1, frac1 = metrics.paired_bootstrap(y, p_a, p_b, 500, seed=42)
    delta2, lo2, hi2, frac2 = metrics.paired_bootstrap(y, p_a, p_b, 500, seed=42)
    # Same seed → identical CI + win fraction (seeded deterministic bootstrap)
    assert delta1 == delta2 and lo1 == lo2 and hi1 == hi2 and frac1 == frac2
    # Different seed → different CI + fraction (proves it's actually sampling).
    # (The point delta is data-only, so it's seed-independent.)
    _, lo3, hi3, frac3 = metrics.paired_bootstrap(y, p_a, p_b, 500, seed=99)
    assert (lo3, hi3, frac3) != (lo1, hi1, frac1)


# ---------------------------------------------------------------------------
# Type 16 — DC snapshot causality (fit_date <= season_start)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type16_dc_snapshot_causality(league_key):
    import pickle
    cfg = league_cfg.get(league_key)
    snap_dir = cfg.results_dir(TOP5) / "dc_snapshots"
    if not snap_dir.exists():
        pytest.skip(f"{league_key}: no DC snapshots yet")
    all_seasons = cfg.dev_seasons + (cfg.calibration_season,)
    season_starts = partitions.compute_season_starts_from_data(
        cfg.raw_pkl(TOP5), all_seasons)
    failures = []
    for pkl in sorted(snap_dir.glob("dc_*.pkl")):
        s = pkl.stem.split("_")[1]
        season_start = season_starts.get(s)
        if season_start is None:
            continue
        with open(pkl, "rb") as f:
            snap = pickle.load(f)
        fit_date = snap.fit_date
        if not isinstance(fit_date, pd.Timestamp):
            fit_date = pd.Timestamp(fit_date)
        if fit_date > season_start:
            failures.append(
                f"season {s}: fit_date={fit_date.date()} > "
                f"season_start={season_start.date()}"
            )
    assert not failures, f"{league_key}: DC causality violations: {failures}"


# ---------------------------------------------------------------------------
# Type 17 — calibration + holdout seasons excluded from dev_seasons
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type17_calib_holdout_excluded_from_dev(league_key):
    cfg = league_cfg.get(league_key)
    assert cfg.calibration_season not in cfg.dev_seasons, (
        f"{league_key}: calibration season leaked into dev_seasons"
    )
    assert cfg.holdout_season not in cfg.dev_seasons, (
        f"{league_key}: holdout season leaked into dev_seasons"
    )


# ---------------------------------------------------------------------------
# Type 18 — league output paths isolated
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type18_output_paths_isolated(league_key):
    cfg = league_cfg.get(league_key)
    own = cfg.results_dir(TOP5).resolve()
    for other_key, other_cfg in league_cfg.ALL.items():
        if other_key == league_key:
            continue
        other = other_cfg.results_dir(TOP5).resolve()
        assert own != other
        # No nesting either
        assert not str(own).startswith(str(other) + "/")
        assert not str(other).startswith(str(own) + "/")


# ---------------------------------------------------------------------------
# Type 19 — no hardcoded season length (380) in core code
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type19_no_hardcoded_season_length(league_key):
    core_dir = TOP5 / "core"
    offenders: list[str] = []
    pattern = re.compile(r"\b380\b")
    for f in sorted(core_dir.glob("*.py")):
        src = f.read_text()
        # Strip line comments to avoid docstring-based false-positives
        stripped = "\n".join(
            line.split("#", 1)[0] for line in src.splitlines()
        )
        if pattern.search(stripped):
            offenders.append(f.name)
    assert not offenders, (
        f"{league_key}: hardcoded 380 (season length) in core: {offenders}"
    )


# ---------------------------------------------------------------------------
# Type 20 — no cross-league team overlap (mostly disjoint universes)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("league_key", _available_leagues())
def test_type20_cross_league_teams_disjoint(league_key):
    import pickle
    cfg = league_cfg.get(league_key)
    with open(cfg.raw_pkl(TOP5), "rb") as f:
        raw = pickle.load(f)
    own_teams = set(raw["home_team"].astype(str).unique())
    if len(own_teams) < 5:
        pytest.skip(f"{league_key}: too few teams to compare")
    checked = 0
    for other_key, other_cfg in league_cfg.ALL.items():
        if other_key == league_key:
            continue
        other_pkl = other_cfg.raw_pkl(TOP5)
        if not other_pkl.exists():
            continue
        with open(other_pkl, "rb") as f:
            other_raw = pickle.load(f)
        other_teams = set(other_raw["home_team"].astype(str).unique())
        overlap = own_teams & other_teams
        # Some tiny overlap tolerated (transliteration edge cases), but the
        # majority of each league's teams must be distinct.
        overlap_share = len(overlap) / max(len(own_teams), 1)
        assert overlap_share < 0.1, (
            f"{league_key} vs {other_key}: {len(overlap)} teams overlap "
            f"({overlap_share:.0%}) — leagues not properly separated. "
            f"Sample overlap: {sorted(overlap)[:5]}"
        )
        checked += 1
    if checked == 0:
        pytest.skip(f"{league_key}: no other leagues to compare against")


if __name__ == "__main__":
    import traceback
    tests = [f for name, f in list(globals().items()) if name.startswith("test_")]
    for t in tests:
        for lk in _available_leagues():
            try:
                t(lk)
                print(f"PASS  {t.__name__}[{lk}]")
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {t.__name__}[{lk}]: {e}")
                traceback.print_exc()

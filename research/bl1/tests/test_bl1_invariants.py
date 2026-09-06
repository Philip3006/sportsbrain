"""FLAGSHIP-BL1 invariant tests — 10 checks per CEO Correction J.

Run:
    python3 -m pytest research/bl1/tests/test_bl1_invariants.py -v

These tests verify the strict-chronology + holdout-integrity contracts of
the corrected v2 pipeline. Each test is a black-box check on the produced
artefacts; passing does not prove the code is bug-free, but it detects the
most common flagship-methodology regressions.
"""
from __future__ import annotations

import pickle
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

RES = ROOT / "research" / "bl1" / "results"
SNAP_DIR = RES / "dc_snapshots"
CALIB_TRAIN_FOLDS = ["1819", "1920"]
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]
DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
CALIB_SEASON = "2425"
HOLDOUT_SEASON = "2526"

SEASON_STARTS = {
    "1617": pd.Timestamp("2016-08-26"), "1718": pd.Timestamp("2017-08-18"),
    "1819": pd.Timestamp("2018-08-24"), "1920": pd.Timestamp("2019-08-16"),
    "2021": pd.Timestamp("2020-09-18"), "2122": pd.Timestamp("2021-08-13"),
    "2223": pd.Timestamp("2022-08-05"), "2324": pd.Timestamp("2023-08-18"),
    "2425": pd.Timestamp("2024-08-23"), "2526": pd.Timestamp("2025-08-22"),
}


def test_01_calibration_uses_no_future_seasons():
    """Invariant 1: for each outer fold F, calibrator sees only rows from folds
    with season < F. Verified by grep of the chronological calibration source
    for the correct comparison operator."""
    src = (ROOT / "research" / "bl1" / "scripts" / "22_calibration_chronological.py").read_text()
    # The pattern earlier + OUTER_FOLDS[:i] used to build the training set.
    assert "CALIB_TRAIN_FOLDS + OUTER_FOLDS[:i]" in src, \
        "chronological calibrator must slice OUTER_FOLDS[:i] (strictly earlier)"
    # And must NOT use the leaky `!=` filter from v1
    assert 'df["season"] != held_out' not in src, \
        "leaky non-chronological training mask must not appear"


def test_02_dc_snapshot_fit_date_precedes_prediction():
    """Invariant 2: DC_snapshot[S].fit_date <= start_of_season_S (i.e. no row
    in season S is predicted by DC data from >= that season)."""
    assert SNAP_DIR.exists(), "DC snapshots directory missing"
    for pkl in sorted(SNAP_DIR.glob("dc_*.pkl")):
        s = pkl.stem.split("_")[1]
        with open(pkl, "rb") as f:
            params = pickle.load(f)
        start = SEASON_STARTS[s]
        assert params.fit_date <= start, (
            f"DC snapshot for season {s} has fit_date {params.fit_date} > season start {start}"
        )


def test_03_inner_val_elo_from_precomputed_series():
    """Invariant 3: inner-validation Elo comes from the precomputed cumulative
    series (elo_series_dev.pkl). Verified by presence of the file and the
    dependency in LGBM v2 script."""
    elo_pkl = RES / "elo_series_dev.pkl"
    assert elo_pkl.exists(), "cumulative Elo series must be persisted"
    src = (ROOT / "research" / "bl1" / "scripts" / "51_lgbm_challengers_v2.py").read_text()
    assert 'elo_series_dev.pkl' in src, "LGBM v2 must load precomputed Elo series"
    # And the previous per-fold empty init must not remain
    assert 'elo_ratings_at_cutoff={}' not in src or "ratings_at_cutoff" not in src, \
        "empty-Elo-init pattern should be removed"


def test_04_rolling_features_use_strict_less_than():
    """Invariant 4: every rolling feature uses `date < before` (strict), not
    `<=`. Enforced by regex over the feature builder."""
    src = (ROOT / "research" / "bl1" / "scripts" / "51_lgbm_challengers_v2.py").read_text()
    for fn in ("_rolling_pts", "_rolling_goals", "_venue_pts", "_rest_days", "_h2h_wr",
                "_domestic_midweek_density"):
        # Extract function body
        m = re.search(rf"def {fn}\([^)]*\).*?(?=\ndef |\Z)", src, re.DOTALL)
        assert m, f"cannot locate {fn}"
        body = m.group(0)
        # Every 'date' comparison in the mask must use <, never <=
        # (Some functions use dates.dt inside; check via broader guard)
        # The masks reference either hist["date"] < before or dates < before
        assert "<= before" not in body, f"{fn} contains '<= before' — must be strict <"
        assert "hist[\"date\"] < before" in body or "dates < before" in body, \
            f"{fn} missing strict `date < before` filter"


def test_05_2425_outcomes_not_used_in_market_selection():
    """Invariant 5: dev-only market hierarchy script does not use 2425 for
    selection metric."""
    src = (ROOT / "research" / "bl1" / "scripts" / "61_market_hierarchy_dev.py").read_text()
    # The dev slice must be strictly DEV_SEASONS
    assert 'raw["season"].isin(DEV_SEASONS)' in src, "market hierarchy must select dev-only slice"
    # And 2425 must not appear as part of the selection loop
    assert 'calib_slice' not in src, "market hierarchy must not build a 2425 selection slice"


def test_06_2526_absent_from_all_outputs():
    """Invariant 6: no output CSV contains a 2526 row (holdout sealed)."""
    for csv in RES.glob("*.csv"):
        if "INVALID_" in str(csv):
            continue
        try:
            df = pd.read_csv(csv, dtype=str, low_memory=False)
        except pd.errors.EmptyDataError:
            continue
        if "season" in df.columns:
            assert HOLDOUT_SEASON not in df["season"].values, (
                f"file {csv.name} contains holdout season {HOLDOUT_SEASON}"
            )


def test_07_closing_odds_not_prediction_features():
    """Invariant 7: closing-odds column names never appear in any LGBM feature
    matrix. Verified by grep of the feature builder for `ps_close` / `PSC*`
    substring on the feature side."""
    src = (ROOT / "research" / "bl1" / "scripts" / "51_lgbm_challengers_v2.py").read_text()
    # Feature dictionary keys must not include closing-odds identifiers
    feat_section = src[src.index('def _build_features'):src.index('def _promoted_by_season')]
    assert "ps_close" not in feat_section, "closing-odds column must not enter LGBM features"
    assert "PSC" not in feat_section, "closing Pinnacle columns must not enter LGBM features"
    assert "AvgC" not in feat_section, "closing bookmaker-avg columns must not enter LGBM features"


def test_08_entry_odds_distinct_from_closing():
    """Invariant 8: the loader distinguishes ps_open_* (entry) from ps_close_*
    (closing). Verified against `src/data/football_data.py` rename map."""
    src = (ROOT / "src" / "data" / "football_data.py").read_text()
    assert '"PSH": "ps_open_home"' in src
    assert '"PSCH": "ps_close_home"' in src
    # These two must map to different names
    assert '"PSH": "ps_close_home"' not in src


def test_09_match_level_bootstrap_preserves_grouping():
    """Invariant 9: match-level bootstrap in edge sweep v2 samples match IDs
    (not individual signals). Verified by presence of the correct primitive."""
    src = (ROOT / "research" / "bl1" / "scripts" / "32_edge_sweep_chronological.py").read_text()
    assert 'def _bootstrap_match_level' in src, "match-level bootstrap function missing"
    assert 'per_match: dict[str, list[float]] = {}' in src, (
        "edge sweep must build per-match PnL lists"
    )
    # Positive check: unique match IDs are the resample unit
    assert 'unique_ids = np.array(list(per_match_pnl.keys()))' in src


def test_10_stable_sort_produces_identical_labels():
    """Invariant 10: stable sort — reloading OOF and joining to raw gives 0
    y-mismatch, confirming row order and labels agree deterministically."""
    oof = pd.read_csv(RES / "oof_dev_v2.csv", dtype={"season": str})
    oof["date"] = pd.to_datetime(oof["date"])
    with open(ROOT / "research/bl1/dataset/bl1_raw.pkl", "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    raw["date"] = pd.to_datetime(raw["date"])
    merged = oof.merge(
        raw[["date", "home_team", "away_team", "home_score", "away_score"]],
        on=["date", "home_team", "away_team"], how="left", suffixes=("_oof", ""),
    )
    y_check = merged.apply(
        lambda r: 2 if r["home_score"] > r["away_score"]
        else (1 if r["home_score"] == r["away_score"] else 0), axis=1,
    )
    mismatch = int((merged["y"] != y_check).sum())
    assert mismatch == 0, f"OOF y mismatches raw scores in {mismatch} rows (sort instability)"


if __name__ == "__main__":
    # Simple pytest-less runner
    import traceback
    tests = [f for name, f in list(globals().items()) if name.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'ALL PASS' if failed == 0 else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)

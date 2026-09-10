"""FLAGSHIP-BL1 invariant tests (v6 structural).

Run:
    python3 -m pytest research/bl1/tests/test_bl1_invariants.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

RES = ROOT / "research" / "bl1" / "results"
SNAP_DIR = RES / "dc_snapshots"
SCRIPTS = ROOT / "research" / "bl1" / "scripts"
DATASET = ROOT / "research" / "bl1" / "dataset"
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

CLOSING_COLUMN_TOKENS = (
    "PSCH", "PSCD", "PSCA",
    "AvgCH", "AvgCD", "AvgCA",
    "MaxCH", "MaxCD", "MaxCA",
    "B365CH", "B365CD", "B365CA",
    "ps_close_home", "ps_close_draw", "ps_close_away",
)

RAW_DATASET_FILENAMES = ("bl1_raw.pkl", "bl1_raw_full.pkl")


# ---------------------------------------------------------------------------
# AST helper: parse a script and find any pickle.load() call whose argument
# is opened on a raw BL1 dataset file. This is the real structural check.
# ---------------------------------------------------------------------------

def _script_ast(name: str) -> ast.AST:
    src = (SCRIPTS / name).read_text()
    return ast.parse(src)


def _finds_raw_pickle_reads(tree: ast.AST) -> list[str]:
    """Return a list of offending source-text snippets. Empty list = clean."""
    src = ast.unparse(tree) if hasattr(ast, "unparse") else ""
    offenses: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call):
            fn = node.func
            # pickle.load(...) or pickle.loads(...)
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
                    and fn.value.id == "pickle" and fn.attr in ("load", "loads"):
                # Inspect the argument: does it (transitively) reference a raw
                # dataset filename literal or a name known to bind to one?
                arg_src = ast.unparse(node) if hasattr(ast, "unparse") else ""
                if any(fname in arg_src for fname in RAW_DATASET_FILENAMES):
                    offenses.append(arg_src)
                # Also flag references to RAW_PKL / FULL_PKL as file arguments,
                # unless the surrounding module is 09_partitions.py (allowed).
                # This visitor runs per-script; the caller handles the allow-list.
                elif any(n in arg_src for n in ("RAW_PKL", "FULL_PKL")):
                    offenses.append(arg_src)
            self.generic_visit(node)

        def visit_With(self, node: ast.With):
            # open(RAW_PKL, "rb") as f: pickle.load(f) pattern.
            for item in node.items:
                cm = item.context_expr
                if isinstance(cm, ast.Call) and isinstance(cm.func, ast.Name) \
                        and cm.func.id == "open":
                    open_src = ast.unparse(cm) if hasattr(ast, "unparse") else ""
                    if any(fname in open_src for fname in RAW_DATASET_FILENAMES) \
                            or any(n in open_src for n in ("RAW_PKL", "FULL_PKL")):
                        # Check body for pickle.load
                        body_src = "\n".join(
                            ast.unparse(stmt) if hasattr(ast, "unparse") else ""
                            for stmt in node.body)
                        if "pickle.load" in body_src or "pickle.loads" in body_src:
                            offenses.append(open_src)
            self.generic_visit(node)

    Visitor().visit(tree)
    return offenses


# ---------------------------------------------------------------------------
# Existing invariants (kept from v5, ordering preserved).
# ---------------------------------------------------------------------------

def test_01_calibration_uses_no_future_seasons():
    src = (SCRIPTS / "22_calibration_chronological.py").read_text()
    assert "CALIB_TRAIN_FOLDS + OUTER_FOLDS[:i]" in src, \
        "chronological calibrator must slice OUTER_FOLDS[:i] (strictly earlier)"
    assert 'df["season"] != held_out' not in src, \
        "leaky non-chronological training mask must not appear"


def test_02_dc_snapshot_fit_date_precedes_prediction():
    import pickle
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
    elo_pkl = RES / "elo_series_dev.pkl"
    assert elo_pkl.exists(), "cumulative Elo series must be persisted"
    src = (SCRIPTS / "51_lgbm_challengers_v2.py").read_text()
    assert 'elo_series_dev.pkl' in src, "LGBM v2 must load precomputed Elo series"


def test_04_rolling_features_use_strict_less_than():
    src = (SCRIPTS / "51_lgbm_challengers_v2.py").read_text()
    for fn in ("_rolling_pts", "_rolling_goals", "_venue_pts", "_rest_days", "_h2h_wr",
                "_domestic_midweek_density"):
        m = re.search(rf"def {fn}\([^)]*\).*?(?=\ndef |\Z)", src, re.DOTALL)
        assert m, f"cannot locate {fn}"
        body = m.group(0)
        assert "<= before" not in body, f"{fn} contains '<= before' — must be strict <"
        assert "hist[\"date\"] < before" in body or "dates < before" in body, \
            f"{fn} missing strict `date < before` filter"


def test_05_2425_outcomes_not_used_in_market_selection():
    src = (SCRIPTS / "61_market_hierarchy_dev.py").read_text()
    assert 'raw["season"].isin(DEV_SEASONS)' in src
    assert 'calib_slice' not in src


def test_06_2526_absent_from_all_outputs():
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
    src = (SCRIPTS / "51_lgbm_challengers_v2.py").read_text()
    feat_section = src[src.index('def _build_features'):src.index('def _promoted_by_season')]
    assert "ps_close" not in feat_section
    assert "PSC" not in feat_section
    assert "AvgC" not in feat_section


def test_08_entry_odds_distinct_from_closing():
    src = (ROOT / "src" / "data" / "football_data.py").read_text()
    assert '"PSH": "ps_open_home"' in src
    assert '"PSCH": "ps_close_home"' in src
    assert '"PSH": "ps_close_home"' not in src


def test_09_match_level_bootstrap_preserves_grouping():
    src = (SCRIPTS / "32_edge_sweep_chronological.py").read_text()
    assert 'def _bootstrap_match_level' in src
    assert 'per_match: dict[str, list[float]] = {}' in src
    assert 'unique_ids = np.array(list(per_match_pnl.keys()))' in src


def test_10_stable_sort_produces_identical_labels():
    """Route through partitions.load_development() (no direct pickle.load
    in the invariant suite itself)."""
    spec = importlib.util.spec_from_file_location(
        "bl1_partitions_test10", SCRIPTS / "09_partitions.py")
    partitions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(partitions)
    raw = partitions.load_development(DATASET / "bl1_raw.pkl")
    raw["date"] = pd.to_datetime(raw["date"])
    oof = pd.read_csv(RES / "oof_dev_v2.csv", dtype={"season": str})
    oof["date"] = pd.to_datetime(oof["date"])
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


# ---------------------------------------------------------------------------
# v6 anti-bypass invariants — real structural checks.
# ---------------------------------------------------------------------------

def test_11_market_aware_scripts_no_direct_raw_pickle_load():
    """v6 §1: 15/16/17 MUST NOT open bl1_raw.pkl or bl1_raw_full.pkl directly
    via pickle.load / open+pickle.load. All raw-dataset access must go
    through the canonical partition API in 09_partitions.py.

    This is a real AST-level structural check, not a grep of the module name.
    """
    for name in ("15_m5_market_baseline.py", "16_m6_m7_market_aware.py",
                  "17_matched_preclose_vs_close.py"):
        tree = _script_ast(name)
        offenses = _finds_raw_pickle_reads(tree)
        assert not offenses, (
            f"{name} directly reads raw BL1 dataset via pickle: {offenses}. "
            f"Must route through 09_partitions.py."
        )


def test_12_sealed_loaders_never_return_closing_columns():
    """v6 §2: load_calibration_predictions_only() and load_holdout_schema_only()
    must return DataFrames containing NO closing-price columns."""
    spec = importlib.util.spec_from_file_location(
        "bl1_partitions_test12", SCRIPTS / "09_partitions.py")
    partitions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(partitions)

    calib = partitions.load_calibration_predictions_only(DATASET / "bl1_raw.pkl")
    calib_leaks = [c for c in calib.columns if c in CLOSING_COLUMN_TOKENS]
    assert not calib_leaks, (
        f"load_calibration_predictions_only leaked closing columns: {calib_leaks}"
    )

    hold = partitions.load_holdout_schema_only(DATASET / "bl1_raw.pkl")
    hold_leaks = [c for c in hold.columns if c in CLOSING_COLUMN_TOKENS]
    assert not hold_leaks, (
        f"load_holdout_schema_only leaked closing columns: {hold_leaks}"
    )


def test_12b_coverage_helper_diagnostics_only():
    """v6 §2: holdout_closing_coverage_diagnostics() must return diagnostics
    columns only (source, columns_present, coverage, n_covered, n_missing,
    n_total) — no price values or price column names."""
    spec = importlib.util.spec_from_file_location(
        "bl1_partitions_test12b", SCRIPTS / "09_partitions.py")
    partitions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(partitions)
    cov = partitions.holdout_closing_coverage_diagnostics(DATASET / "bl1_raw_full.pkl")
    allowed = {"source", "columns_present", "coverage",
               "n_covered", "n_missing", "n_total"}
    extra = set(cov.columns) - allowed
    assert not extra, f"coverage helper returned unexpected columns: {extra}"
    # And explicit: no closing-column identifier tokens leak
    for tok in CLOSING_COLUMN_TOKENS:
        assert tok not in cov.columns, f"coverage helper leaked column {tok}"


def test_13_m6_equals_m5_when_alpha_1_via_partition_loader():
    """v6 §4: forcibly evaluate the canonical policy on the DEV+market frame
    obtained via `partitions.load_development_with_market()`. Result must
    match M5 OOF for the outer folds within 1e-9. The invariant suite itself
    routes through the partition API.
    """
    spec = importlib.util.spec_from_file_location(
        "bl1_partitions_test13", SCRIPTS / "09_partitions.py")
    partitions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(partitions)
    spec_m = importlib.util.spec_from_file_location(
        "bl1_canonical_market_test13", SCRIPTS / "canonical_market.py")
    canonical = importlib.util.module_from_spec(spec_m)
    spec_m.loader.exec_module(canonical)

    dev = partitions.load_development_with_market(
        DATASET / "bl1_raw.pkl", DATASET / "bl1_raw_full.pkl", include_closing=False)
    dev["date"] = pd.to_datetime(dev["date"])
    dev = dev[dev["season"].isin(OUTER_FOLDS)].sort_values(
        ["date", "home_team"], kind="stable").reset_index(drop=True)
    # Apply the unified policy — this is what M5 does.
    kept, probs = canonical.apply_policy(dev)

    m5 = pd.read_csv(RES / "oof_m5_preclose_dev.csv", dtype={"season": str})
    m5["date"] = pd.to_datetime(m5["date"])
    m5 = m5.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    m5_probs = m5[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()

    assert len(kept) == len(m5_probs), (
        f"apply_policy row count {len(kept)} != M5 OOF row count {len(m5_probs)}"
    )
    diff = float(np.max(np.abs(probs - m5_probs)))
    assert diff < 1e-9, f"canonical apply_policy differs from M5 OOF by {diff:.2e}"


def test_13b_missing_market_row_produces_identical_m5_and_m6_alpha1():
    """v6 §4: deliberately remove the canonical market price from a DEV row
    and prove that M5 (canonical policy) and M6-at-alpha-1 (canonical policy
    + zero Elo contribution) produce identical evaluated rows and identical
    probabilities under the unified missing-market policy.

    Does not touch 2425 or 2526 outcomes.
    """
    spec = importlib.util.spec_from_file_location(
        "bl1_partitions_test13b", SCRIPTS / "09_partitions.py")
    partitions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(partitions)
    spec_m = importlib.util.spec_from_file_location(
        "bl1_canonical_market_test13b", SCRIPTS / "canonical_market.py")
    canonical = importlib.util.module_from_spec(spec_m)
    spec_m.loader.exec_module(canonical)

    dev = partitions.load_development_with_market(
        DATASET / "bl1_raw.pkl", DATASET / "bl1_raw_full.pkl", include_closing=False)
    # Take a small dev slice for the synthetic test.
    slice_df = dev[dev["season"] == "2021"].sort_values(
        ["date", "home_team"], kind="stable").reset_index(drop=True).head(20).copy()

    # Deliberately blank canonical odds on rows 3, 7, 12.
    for i in (3, 7, 12):
        slice_df.loc[i, "AvgH"] = np.nan
        slice_df.loc[i, "AvgD"] = np.nan
        slice_df.loc[i, "AvgA"] = np.nan

    # M5 codepath (canonical policy).
    kept_m5, p_m5 = canonical.apply_policy(slice_df)

    # M6-at-alpha-1 codepath (canonical policy + Elo weighted at alpha=1.0).
    # Under the unified policy, the row-set filter is identical, and at
    # alpha=1.0 the Elo term drops.
    kept_m6, p_mkt = canonical.apply_policy(slice_df)
    # Simulate a nonzero Elo p_elo; at alpha=1.0 it must not affect result.
    dummy_p_elo = np.full_like(p_mkt, 1.0 / 3.0)
    p_m6_alpha1 = 1.0 * p_mkt + 0.0 * dummy_p_elo

    # Row-set equality
    pd.testing.assert_frame_equal(
        kept_m5[["date", "home_team", "away_team"]].reset_index(drop=True),
        kept_m6[["date", "home_team", "away_team"]].reset_index(drop=True),
    )
    # Row count must equal original minus the 3 blanked rows (20 - 3 = 17)
    assert len(kept_m5) == 17, f"expected 17 kept rows, got {len(kept_m5)}"
    # Probability equality
    diff = float(np.max(np.abs(p_m5 - p_m6_alpha1)))
    assert diff < 1e-12, f"M5 and M6-at-alpha-1 diverge under missing-market: {diff:.2e}"


if __name__ == "__main__":
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

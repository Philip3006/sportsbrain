"""Top-5 — A/B/A' contamination test harness.

For each league, verifies that development-decision outputs are invariant
to sentinel mutation of calibration+holdout outcomes and closing prices.

Test protocol (mirrors BL1 v7 98_contamination_test.py logic):
  A  = hash all dev-output CSVs on real dataset
  B  = re-run the FULL M1-M7 pipeline on sentinel dataset (2425+2526 outcomes
       and closing prices nulled) into an isolated results dir, then hash
       the same set of dev-output CSVs from B
  A' = re-hash real dev-output CSVs after run (must be unchanged)

Pass criterion:
  hash_a == hash_b   for every checked file (sealed data has zero causal
                     influence on dev decisions)
  hash_a == hash_a'  for every checked file (real outputs untouched)

The sentinel is applied IN-MEMORY by writing temporary copies of the
raw and full pickles with 2425+2526 rows mutated — no production file is
ever modified. B outputs are written into `results_dir/_contamination_b/`
and cleaned up AFTER hash capture.
"""
from __future__ import annotations

import hashlib
import pickle
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import LeagueConfig

# Outputs whose hash is checked across A/B/A'. All are "dev decision" artefacts:
# their content must depend on nothing but the DEV partition (1617-2324) which
# the sentinel does not touch.
_DEV_OUTPUTS_TO_CHECK = [
    # M1-M7 primary OOF outputs
    "oof_m1_dev.csv",
    "oof_m2_dev.csv",
    "oof_m3_dev_v2.csv",
    "oof_m4_dev_v2.csv",
    "oof_m5_preclose_dev.csv",
    "oof_m6_dev_v3.csv",
    "oof_m7_dev_v3.csv",
    # Aggregated summaries and per-fold artefacts
    "m5_preclose_baseline_summary.csv",
    "m5_source_selection_by_fold.csv",
    "m6_alpha_sweep.csv",
    "matched_preclose_vs_close.csv",
    # Additional dev decision artefacts covered by full-pipeline B run
    "phi_selection_dev.csv",
    "fold_summary_m1m2.csv",
    "model_summary.csv",
    "paired_bootstrap.csv",
    "edge_sweep_v3_one_per_match.csv",
]


def _hash_csv(path: Path) -> str | None:
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _hash_outputs(res: Path, outputs: list[str]) -> dict[str, str | None]:
    return {name: _hash_csv(res / name) for name in outputs}


# Full outcome/closing column set to null in the sentinel. All columns not in
# the dataframe are silently ignored (missingness pattern preserved).
_OUTCOME_STRING_COLS = ("FTR", "HTR")
_OUTCOME_NUMERIC_COLS = (
    "FTHG", "FTAG", "HTHG", "HTAG",
    "HS", "AS", "HST", "AST",
    "home_score", "away_score",
)
_CLOSING_COLS = (
    "AvgCH", "AvgCD", "AvgCA",
    "MaxCH", "MaxCD", "MaxCA",
    "B365CH", "B365CD", "B365CA",
    "PSCH", "PSCD", "PSCA",
    "ps_close_home", "ps_close_draw", "ps_close_away",
)


def _sentinel_pkl(pkl_path: Path, calib_season: str, holdout_season: str) -> Path:
    """Write a sentinel copy of the pickle with 2425+2526 outcomes + closing nulled.

    - Numeric outcomes/scores → NaN
    - String outcomes (FTR/HTR) → ""
    - Closing odds → NaN

    Only columns that already exist in the dataframe are touched (preserves
    real missingness patterns for absent columns). Returns path to the
    temporary file. Caller must clean up.
    """
    with open(pkl_path, "rb") as f:
        df: pd.DataFrame = pickle.load(f)
    df = df.copy()
    mask = df["season"].astype(str).isin([calib_season, holdout_season])

    # We deliberately set home_score/away_score to a *sentinel* value (99/0)
    # rather than NaN because loaders drop NaN-score rows; the goal is to
    # make sure the value flows nowhere but should NEVER change dev outputs.
    if "home_score" in df.columns:
        df.loc[mask, "home_score"] = 99
    if "away_score" in df.columns:
        df.loc[mask, "away_score"] = 0

    # All other outcome-derived columns → null
    for col in _OUTCOME_NUMERIC_COLS:
        if col in ("home_score", "away_score"):
            continue
        if col in df.columns:
            df.loc[mask, col] = np.nan
    for col in _OUTCOME_STRING_COLS:
        if col in df.columns:
            df.loc[mask, col] = ""

    # Closing prices → null
    for col in _CLOSING_COLS:
        if col in df.columns:
            df.loc[mask, col] = np.nan

    tmp = tempfile.NamedTemporaryFile(suffix=".pkl", delete=False)
    with open(tmp.name, "wb") as f:
        pickle.dump(df, f)
    return Path(tmp.name)


def _run_full_pipeline_b(config: LeagueConfig, sentinel_raw: Path,
                          sentinel_full: Path, b_res_dir: Path,
                          top5_root: Path) -> None:
    """Re-run the full M1-M7 pipeline + analysis on the sentinel dataset.

    All outputs are written into `b_res_dir` (isolated). The real league
    results dir is not touched.
    """
    from . import models as m_mod
    from . import analysis as an_mod

    b_res_dir.mkdir(parents=True, exist_ok=True)

    class _PatchedConfigFull:
        """Delegates to `config` except for pkl/results paths.

        This ensures the pipeline reads from sentinel pkls and writes to
        the isolated B results dir, while every other config field
        (seasons, fold definitions, keys) remains unchanged.
        """
        # Forward every attribute of the underlying config
        def __getattr__(self, name):
            return getattr(config, name)
        # Path overrides (must be methods to match LeagueConfig signature)
        def raw_pkl(self, root):
            return sentinel_raw
        def full_pkl(self, root):
            return sentinel_full
        def results_dir(self, root):
            return b_res_dir
        def dataset_dir(self, root):
            # Prevent the pipeline from writing anything under the real
            # league dataset dir. Route to a sibling under b_res_dir.
            return b_res_dir / "_dataset"
        def scripts_dir(self, root):
            return b_res_dir / "_scripts"

    patched = _PatchedConfigFull()

    # Run the full M1-M7 pipeline (artefacts + all models + matched)
    m_mod.run_full_pipeline(patched, top5_root)
    # Run downstream analysis artefacts as well
    an_mod.run_paired_bootstrap(patched, top5_root)
    an_mod.run_edge_sweep(patched, top5_root)
    an_mod.run_summary(patched, top5_root)


def run_contamination_test(config: LeagueConfig, top5_root: Path) -> dict:
    """Run full A/B/A' contamination test for a league.

    Flow:
      A  = hash outputs_to_check on real dataset
      B  = full pipeline on sentinel dataset into isolated results dir,
           hash the same set of outputs
      A' = re-hash real outputs (must be unchanged)

    Writes:
      results_dir/contamination_test_hashes.csv
        columns: file, hash_a, hash_b, hash_aprime, a_eq_b, a_eq_aprime, result

    PASS criterion: all a_eq_b AND all a_eq_aprime.
    """
    res = config.results_dir(top5_root)
    outputs_to_check = [f for f in _DEV_OUTPUTS_TO_CHECK if (res / f).exists()]
    if not outputs_to_check:
        return {"league": config.key, "result": "SKIP",
                "reason": "no dev outputs found yet"}

    print(f"\n[contamination/{config.key}] === Run A: hashing "
          f"{len(outputs_to_check)} outputs ===", flush=True)
    hashes_a = _hash_outputs(res, outputs_to_check)

    # Create sentinel datasets
    sentinel_raw = _sentinel_pkl(config.raw_pkl(top5_root),
                                  config.calibration_season, config.holdout_season)
    sentinel_full = _sentinel_pkl(config.full_pkl(top5_root),
                                   config.calibration_season, config.holdout_season)
    b_res = res / "_contamination_b"
    hashes_b: dict[str, str | None] = {}
    errors: list[str] = []

    try:
        print(f"[contamination/{config.key}] === Run B: full pipeline on "
              f"sentinel dataset ===", flush=True)
        try:
            _run_full_pipeline_b(config, sentinel_raw, sentinel_full,
                                  b_res, top5_root)
        except Exception as ex:  # noqa: BLE001
            errors.append(f"B pipeline crashed: {type(ex).__name__}: {ex}")

        # CAPTURE HASHES FIRST — before any cleanup — so downstream reporting
        # sees the real B outputs.
        hashes_b = _hash_outputs(b_res, outputs_to_check)

        for fname in outputs_to_check:
            hash_a = hashes_a.get(fname)
            hash_b = hashes_b.get(fname)
            if hash_b is None:
                errors.append(f"MISSING B {fname}: B run did not produce this file")
                continue
            if hash_a != hash_b:
                errors.append(
                    f"CONTAMINATION {fname}: A={hash_a[:12] if hash_a else 'None'} "
                    f"B={hash_b[:12]}"
                )
            else:
                print(f"  A==B {fname}: {hash_a[:12]}", flush=True)
    finally:
        # Cleanup only after hashes captured
        sentinel_raw.unlink(missing_ok=True)
        sentinel_full.unlink(missing_ok=True)
        if b_res.exists():
            shutil.rmtree(b_res, ignore_errors=True)

    # Run A' = re-hash the original outputs (must be unchanged)
    hashes_aprime = _hash_outputs(res, outputs_to_check)
    for fname in outputs_to_check:
        if hashes_a.get(fname) != hashes_aprime.get(fname):
            errors.append(f"RESTORE FAIL {fname}: A!=A'")

    result = "PASS" if not errors else "FAIL"
    print(f"\n[contamination/{config.key}] {result}: {len(errors)} failures",
          flush=True)
    for e in errors:
        print(f"  {e}", flush=True)

    # Write per-file evidence CSV. hash_b uses "missing" placeholder when the
    # B pipeline did not produce that output.
    rows = []
    for fname in outputs_to_check:
        hash_a = hashes_a.get(fname)
        hash_b = hashes_b.get(fname)
        hash_ap = hashes_aprime.get(fname)
        a_eq_b = (hash_a is not None and hash_a == hash_b)
        a_eq_ap = (hash_a is not None and hash_a == hash_ap)
        rows.append({
            "file": fname,
            "hash_a": hash_a if hash_a is not None else "missing",
            "hash_b": hash_b if hash_b is not None else "missing",
            "hash_aprime": hash_ap if hash_ap is not None else "missing",
            "a_eq_b": a_eq_b,
            "a_eq_aprime": a_eq_ap,
        })
    csv_df = pd.DataFrame(rows)
    all_a_eq_b = bool(csv_df["a_eq_b"].all()) if len(csv_df) else False
    all_a_eq_ap = bool(csv_df["a_eq_aprime"].all()) if len(csv_df) else False
    csv_df["result"] = "PASS" if (all_a_eq_b and all_a_eq_ap) else "FAIL"
    csv_df.to_csv(res / "contamination_test_hashes.csv", index=False)
    return {
        "league": config.key,
        "result": result,
        "errors": errors,
        "n_files_checked": len(outputs_to_check),
        "all_a_eq_b": all_a_eq_b,
        "all_a_eq_aprime": all_a_eq_ap,
    }

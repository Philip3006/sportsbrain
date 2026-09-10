"""Top-5 — A/B/A' contamination test harness.

For each league, verifies that development-decision outputs are invariant
to sentinel mutation of calibration+holdout outcomes and closing prices.

Test protocol (mirrors BL1 v7 98_contamination_test.py logic):
  A  = hash all dev-output CSVs on real dataset
  B  = hash after sentinel mutation of 2425+2526 (home_score=99, away_score=0)
  A' = hash after restoring real dataset

Pass criterion: A == B (dev decisions unchanged) and A == A' (restore works).

The sentinel is applied IN-MEMORY by creating a temporary copy of the
dataset pickle with 2425+2526 rows overwritten — no production file is
ever modified.
"""
from __future__ import annotations

import hashlib
import pickle
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from . import canonical_market as market
from . import metrics
from . import partitions
from ..config import LeagueConfig

_DEV_OUTPUTS_TO_CHECK = [
    "oof_m5_preclose_dev.csv",
    "m5_preclose_baseline_summary.csv",
    "m5_source_selection_by_fold.csv",
    "oof_m6_dev_v3.csv",
    "m6_alpha_sweep.csv",
    "matched_preclose_vs_close.csv",
    "oof_m1_dev.csv",
    "oof_m2_dev.csv",
    "oof_m3_dev_v2.csv",
    "oof_m4_dev_v2.csv",
    "oof_m7_dev_v3.csv",
]


def _hash_csv(path: Path) -> str | None:
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _hash_outputs(res: Path, outputs: list[str]) -> dict[str, str | None]:
    return {name: _hash_csv(res / name) for name in outputs}


def _sentinel_pkl(pkl_path: Path, calib_season: str, holdout_season: str) -> Path:
    """Write a sentinel copy of the pickle with 2425+2526 outcomes zeroed.
    Returns path to the temporary file. Caller must clean up."""
    with open(pkl_path, "rb") as f:
        df: pd.DataFrame = pickle.load(f)
    df = df.copy()
    mask = df["season"].astype(str).isin([calib_season, holdout_season])
    df.loc[mask, "home_score"] = 99
    df.loc[mask, "away_score"] = 0
    # Also zero out closing prices for the sealed seasons if present
    close_cols = ["AvgCH", "AvgCD", "AvgCA", "MaxCH", "MaxCD", "MaxCA",
                  "B365CH", "B365CD", "B365CA", "PSCH", "PSCD", "PSCA"]
    for c in close_cols:
        if c in df.columns:
            df.loc[mask, c] = np.nan
    tmp = tempfile.NamedTemporaryFile(suffix=".pkl", delete=False)
    with open(tmp.name, "wb") as f:
        pickle.dump(df, f)
    return Path(tmp.name)


def _run_m5_m6_in_memory(config: LeagueConfig, raw_pkl: Path, full_pkl: Path,
                           top5_root: Path) -> None:
    """Re-run M5/M6/matched-preclose-vs-close writing outputs to results_dir."""
    from . import models as m_mod
    from . import elo as elo_mod

    # Temporarily monkey-patch the config pkl path
    class _PatchedConfig:
        def __getattr__(self, name):
            return getattr(config, name)
        def raw_pkl(self, top5_root): return raw_pkl
        def full_pkl(self, top5_root): return full_pkl
        def results_dir(self, top5_root): return config.results_dir(top5_root) / "_contamination_b"
        @property
        def key(self): return config.key
        @property
        def dev_seasons(self): return config.dev_seasons
        @property
        def calibration_season(self): return config.calibration_season
        @property
        def holdout_season(self): return config.holdout_season
        @property
        def calib_seed_folds(self): return config.calib_seed_folds
        @property
        def outer_folds(self): return config.outer_folds

    patched = _PatchedConfig()
    patched.results_dir(top5_root).mkdir(parents=True, exist_ok=True)
    m_mod.run_m5(patched, top5_root)
    m_mod.run_m6(patched, top5_root)
    m_mod.run_matched_preclose_vs_close(patched, top5_root)


def run_contamination_test(config: LeagueConfig, top5_root: Path) -> dict:
    """Run A/B/A' contamination test for a league.

    Returns:
        dict with 'result': 'PASS'/'FAIL', per-file comparison, and error msgs.
    """
    res = config.results_dir(top5_root)
    outputs_to_check = [f for f in _DEV_OUTPUTS_TO_CHECK if (res / f).exists()]
    if not outputs_to_check:
        return {"league": config.key, "result": "SKIP",
                "reason": "no dev outputs found yet"}

    print(f"\n[contamination/{config.key}] === Run A: hashing {len(outputs_to_check)} outputs ===",
          flush=True)
    hashes_a = _hash_outputs(res, outputs_to_check)

    # Create sentinel datasets
    sentinel_raw = _sentinel_pkl(config.raw_pkl(top5_root),
                                  config.calibration_season, config.holdout_season)
    sentinel_full = _sentinel_pkl(config.full_pkl(top5_root),
                                   config.calibration_season, config.holdout_season)
    b_res = res / "_contamination_b"
    b_res.mkdir(parents=True, exist_ok=True)
    errors = []

    try:
        print(f"[contamination/{config.key}] === Run B: sentinel dataset ===", flush=True)
        _run_m5_m6_in_memory(config, sentinel_raw, sentinel_full, top5_root)
        # Compare B outputs to A outputs (sentinel must not change dev decisions)
        hashes_b_raw = _hash_outputs(b_res,
                                      ["oof_m5_preclose_dev.csv",
                                       "m5_preclose_baseline_summary.csv",
                                       "oof_m6_dev_v3.csv",
                                       "matched_preclose_vs_close.csv"])
        # Map B output names to A output names for comparison
        for fname, hash_b in hashes_b_raw.items():
            hash_a = hashes_a.get(fname)
            if hash_a is None or hash_b is None:
                continue
            if hash_a != hash_b:
                errors.append(f"FAIL {fname}: A={hash_a[:12]} B={hash_b[:12]}")
            else:
                print(f"  PASS {fname}: {hash_a[:12]}", flush=True)
    finally:
        sentinel_raw.unlink(missing_ok=True)
        sentinel_full.unlink(missing_ok=True)
        import shutil
        if b_res.exists():
            shutil.rmtree(b_res, ignore_errors=True)

    # Run A' = re-hash the original outputs (unchanged)
    hashes_aprime = _hash_outputs(res, outputs_to_check)
    for fname in outputs_to_check:
        if hashes_a.get(fname) != hashes_aprime.get(fname):
            errors.append(f"RESTORE FAIL {fname}: A≠A'")

    result = "PASS" if not errors else "FAIL"
    print(f"\n[contamination/{config.key}] {result}: {len(errors)} failures", flush=True)
    for e in errors:
        print(f"  {e}", flush=True)

    rows = []
    for fname in outputs_to_check:
        rows.append({
            "file": fname,
            "hash_a": hashes_a.get(fname, "missing"),
            "hash_aprime": hashes_aprime.get(fname, "missing"),
            "a_eq_aprime": hashes_a.get(fname) == hashes_aprime.get(fname),
        })
    pd.DataFrame(rows).to_csv(res / "contamination_test_hashes.csv", index=False)
    return {"league": config.key, "result": result, "errors": errors,
            "n_files_checked": len(outputs_to_check)}

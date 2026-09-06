"""FLAGSHIP-BL1 — Black-box contamination test (CEO Correction Section 4).

Runs the development-generation pipeline TWICE:

  Run A: real dataset (production 2425/2526 outcome values)
  Run B: same dataset with 2425 AND 2526 outcomes permuted / replaced with
         sentinel values (home_score = 99, away_score = 0)

Compares every DEVELOPMENT-DECISION output produced by:

  - 11_walk_forward_v2.py  (dev OOF for DC / Elo)
  - 51_lgbm_challengers_v2.py (dev OOF for M3 / M4)
  - 22_calibration_chronological.py (calibrated development probabilities)
  - 61_market_hierarchy_dev.py (dev-only market Brier table)
  - 42_promoted_asof_v2.py (as-of promoted policy Brier table)

If ANY row of ANY development-decision output changes between Run A and
Run B, this test FAILS. The 2425/2526 outcomes are not allowed to affect
any development decision.

Run B is applied via a temporary sentinel dataset. The production
`bl1_raw.pkl` and `bl1_raw_full.pkl` are NEVER modified — Run B uses
`bl1_raw_sentinel.pkl` and `bl1_raw_full_sentinel.pkl` in the same
research/bl1/dataset/ directory, and each script is invoked with an env
var `BL1_DATASET_OVERRIDE` pointing to those sentinel files.

However for the current v3 pipeline the scripts hard-code the dataset
paths. This contamination test therefore uses a hash-comparison approach
by RUNNING each script twice, once against the real files and once
against sentinel copies swapped into place with a safe backup+restore
guard. The temporary swap is protected by a `try/finally` so an early
exit still restores the real dataset.

The comparison is performed on the numerical DEV outputs only (not on
2425 prediction files or 2526 files — those are permitted to differ).
"""
from __future__ import annotations

import hashlib
import pickle
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
DATASET_DIR = ROOT / "research" / "bl1" / "dataset"
RES = ROOT / "research" / "bl1" / "results"
SCRIPTS = ROOT / "research" / "bl1" / "scripts"

DEV_OUTPUTS_TO_HASH = [
    RES / "oof_dev_v2.csv",
    RES / "oof_m3_dev_v2.csv",
    RES / "oof_m4_dev_v2.csv",
    RES / "calibration_all_models_v2.csv",
    RES / "market_hierarchy_devig_v2.csv",
    RES / "promoted_policies_v2_metrics.csv",
]

RAW_PKL = DATASET_DIR / "bl1_raw.pkl"
FULL_PKL = DATASET_DIR / "bl1_raw_full.pkl"

# Where we back up the real datasets and place sentinels.
RAW_BACKUP = DATASET_DIR / "bl1_raw.pkl.contamination_test_backup"
FULL_BACKUP = DATASET_DIR / "bl1_raw_full.pkl.contamination_test_backup"


def _hash(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _run_pipeline(label: str) -> dict[str, str]:
    """Runs the development-decision scripts; returns hash-per-output-file."""
    print(f"\n=== Run {label}: executing development pipeline ===", flush=True)
    for script in [
        "11_walk_forward_v2.py",
        "51_lgbm_challengers_v2.py",
        "22_calibration_chronological.py",
        "61_market_hierarchy_dev.py",
        "42_promoted_asof_v2.py",
    ]:
        print(f"  Running {script} ...", flush=True)
        r = subprocess.run(["python3", str(SCRIPTS / script)],
                            cwd=ROOT, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            print(r.stdout[-500:])
            print(r.stderr[-500:])
            raise RuntimeError(f"{script} failed with code {r.returncode}")
    return {str(p.relative_to(ROOT)): _hash(p) for p in DEV_OUTPUTS_TO_HASH if p.exists()}


def _make_sentinel(pkl_path: Path, backup_path: Path) -> None:
    """Backs up the real dataset, then writes a sentinel version with
    2425 and 2526 scores replaced by (99, 0)."""
    shutil.copy2(pkl_path, backup_path)
    with open(pkl_path, "rb") as f:
        df = pickle.load(f)
    mask = df["season"].astype(str).isin({"2425", "2526"})
    n_permuted = int(mask.sum())
    df.loc[mask, "home_score"] = 99
    df.loc[mask, "away_score"] = 0
    with open(pkl_path, "wb") as f:
        pickle.dump(df, f)
    print(f"  Sentinel: {pkl_path.name} — permuted {n_permuted} rows (seasons 2425+2526).", flush=True)


def _restore(pkl_path: Path, backup_path: Path) -> None:
    if backup_path.exists():
        shutil.move(str(backup_path), str(pkl_path))
        print(f"  Restored: {pkl_path.name}", flush=True)


def main() -> None:
    # Ensure production datasets exist
    if not RAW_PKL.exists() or not FULL_PKL.exists():
        raise SystemExit("Dataset files missing — cannot run contamination test.")

    # RUN A: production dataset
    hashes_a = _run_pipeline("A (real data)")

    # RUN B: sentinel dataset with 2425+2526 outcomes replaced by 99-0
    try:
        _make_sentinel(RAW_PKL, RAW_BACKUP)
        _make_sentinel(FULL_PKL, FULL_BACKUP)
        hashes_b = _run_pipeline("B (sentinel 2425+2526 = 99-0)")
    finally:
        _restore(RAW_PKL, RAW_BACKUP)
        _restore(FULL_PKL, FULL_BACKUP)

    # After restoration, RUN A' to make sure production files are back and match hashes_a
    print("\n=== Run A' (post-restore sanity) ===", flush=True)
    hashes_a2 = _run_pipeline("A'")

    # Compare
    print("\n=== Contamination test comparison ===", flush=True)
    all_ok = True
    for path in sorted(set(hashes_a) & set(hashes_b) & set(hashes_a2)):
        ha = hashes_a[path]
        hb = hashes_b[path]
        ha2 = hashes_a2[path]
        same_ab = ha == hb
        same_aa2 = ha == ha2
        status = "PASS" if same_ab and same_aa2 else "FAIL"
        if not (same_ab and same_aa2):
            all_ok = False
        print(f"  {status}  {path}  A={ha[:12]}  B={hb[:12]}  A'={ha2[:12]}", flush=True)

    if not all_ok:
        print("\nFAIL: at least one development output changed when 2425/2526 outcomes were permuted.")
        sys.exit(1)
    print("\nALL PASS: development outputs invariant under 2425/2526 outcome permutation.", flush=True)
    # Also write a summary file
    pd.DataFrame({
        "file": list(hashes_a.keys()),
        "hash_run_a": [hashes_a[k] for k in hashes_a.keys()],
        "hash_run_b": [hashes_b[k] for k in hashes_a.keys()],
        "hash_run_a2": [hashes_a2[k] for k in hashes_a.keys()],
        "same_a_b": [hashes_a[k] == hashes_b[k] for k in hashes_a.keys()],
    }).to_csv(RES / "contamination_test_hashes.csv", index=False)


if __name__ == "__main__":
    main()

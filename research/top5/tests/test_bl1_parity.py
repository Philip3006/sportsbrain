"""BL1 automated parity gate.

Compares the Top-5 framework BL1 outputs against the frozen BL1 v7 reference
worktree (SHA 569741b4ad571a38492e4d7cfd014cf82daec396).

Parity tolerances:
  - Deterministic M1/M2/M5/M6: brier delta < 1e-12
  - LGBM M3/M4/M7: brier delta < 1e-11 (tiny float64 drift acceptable)
  - phi selection: identical best-phi value across all candidates
  - matched preclose-vs-close: brier + CI deltas < 1e-10

Note on the phi CSV brier values:
  The reference's phi sweep evaluated over a broader OOF pool (calib seeds
  + outer folds), while the Top-5 framework's phi sweep uses only outer_folds.
  This changes the *intermediate* brier values in phi_selection_dev.csv but
  the SELECTED phi (=0.0012) is identical in both, and the resulting DC
  snapshots (fit on prior data) plus M1 OOF brier are bit-identical.

The absolute reference path is hard-coded to
`/Users/philiprassillier/sportsbrain/.claude/worktrees/flagship-bl1-research`
per CEO governance (frozen worktree = single canonical reference source).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from research.top5 import config as league_cfg  # noqa: E402
from research.top5.core import metrics  # noqa: E402

TOP5 = ROOT / "research" / "top5"
REF = Path(
    "/Users/philiprassillier/sportsbrain/.claude/worktrees/"
    "flagship-bl1-research/research/bl1/results"
)
BL1_RES = TOP5 / "leagues" / "bl1" / "results"
OUTER_FOLDS = ("2021", "2122", "2223", "2324")

# Parity tolerances
TOL_DETERMINISTIC = 1e-12
TOL_LGBM = 1e-11
TOL_MATCHED = 1e-10


def _outer(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["season"] = df["season"].astype(str)
    df = df[df["season"].isin(OUTER_FOLDS)]
    return df.sort_values(
        ["date", "home_team"], kind="stable"
    ).reset_index(drop=True)


def _need_ref():
    if not REF.exists():
        pytest.skip(f"BL1 reference worktree not present at {REF}")


def _brier_from(df: pd.DataFrame, cols: list[str]) -> float:
    y = df["y"].to_numpy()
    p = df[cols].to_numpy()
    return metrics.brier(y, p)


def test_bl1_parity_m1_dc():
    _need_ref()
    ref = _outer(pd.read_csv(REF / "oof_dev_v2.csv", dtype={"season": str}))
    top = _outer(pd.read_csv(BL1_RES / "oof_m1_dev.csv", dtype={"season": str}))
    b_r = _brier_from(ref, ["dc_p_away", "dc_p_draw", "dc_p_home"])
    b_t = _brier_from(top, ["m1_p_away", "m1_p_draw", "m1_p_home"])
    delta = abs(b_r - b_t)
    print(f"[BL1 parity] M1: REF={b_r:.15f} TOP5={b_t:.15f} delta={delta:.2e}")
    assert delta < TOL_DETERMINISTIC


def test_bl1_parity_m2_elo():
    _need_ref()
    ref = _outer(pd.read_csv(REF / "oof_dev_v2.csv", dtype={"season": str}))
    top = _outer(pd.read_csv(BL1_RES / "oof_m1_dev.csv", dtype={"season": str}))
    b_r = _brier_from(ref, ["elo_p_away", "elo_p_draw", "elo_p_home"])
    b_t = _brier_from(top, ["m2_p_away", "m2_p_draw", "m2_p_home"])
    delta = abs(b_r - b_t)
    print(f"[BL1 parity] M2: REF={b_r:.15f} TOP5={b_t:.15f} delta={delta:.2e}")
    assert delta < TOL_DETERMINISTIC


def test_bl1_parity_m3_lgbm_dmwd():
    _need_ref()
    ref = _outer(pd.read_csv(REF / "oof_m3_dev_v2.csv", dtype={"season": str}))
    top = _outer(pd.read_csv(BL1_RES / "oof_m3_dev_v2.csv", dtype={"season": str}))
    b_r = _brier_from(ref, ["m3_p_away", "m3_p_draw", "m3_p_home"])
    b_t = _brier_from(top, ["m3_p_away", "m3_p_draw", "m3_p_home"])
    delta = abs(b_r - b_t)
    print(f"[BL1 parity] M3: REF={b_r:.15f} TOP5={b_t:.15f} delta={delta:.2e}")
    assert delta < TOL_LGBM


def test_bl1_parity_m4_lgbm():
    _need_ref()
    ref = _outer(pd.read_csv(REF / "oof_m4_dev_v2.csv", dtype={"season": str}))
    top = _outer(pd.read_csv(BL1_RES / "oof_m4_dev_v2.csv", dtype={"season": str}))
    b_r = _brier_from(ref, ["m4_p_away", "m4_p_draw", "m4_p_home"])
    b_t = _brier_from(top, ["m4_p_away", "m4_p_draw", "m4_p_home"])
    delta = abs(b_r - b_t)
    print(f"[BL1 parity] M4: REF={b_r:.15f} TOP5={b_t:.15f} delta={delta:.2e}")
    assert delta < TOL_LGBM


def test_bl1_parity_m5_market_preclose():
    _need_ref()
    ref = pd.read_csv(REF / "oof_m5_preclose_dev.csv", dtype={"season": str})
    top = pd.read_csv(BL1_RES / "oof_m5_preclose_dev.csv", dtype={"season": str})
    b_r = _brier_from(ref, ["m5_p_away", "m5_p_draw", "m5_p_home"])
    b_t = _brier_from(top, ["m5_p_away", "m5_p_draw", "m5_p_home"])
    delta = abs(b_r - b_t)
    assert len(ref) == len(top) == 1224
    print(f"[BL1 parity] M5: REF={b_r:.15f} TOP5={b_t:.15f} delta={delta:.2e}")
    assert delta < TOL_DETERMINISTIC


def test_bl1_parity_m6_market_elo_blend():
    _need_ref()
    ref = pd.read_csv(REF / "oof_m6_dev_v3.csv", dtype={"season": str})
    top = pd.read_csv(BL1_RES / "oof_m6_dev_v3.csv", dtype={"season": str})
    b_r = _brier_from(ref, ["m6_p_away", "m6_p_draw", "m6_p_home"])
    b_t = _brier_from(top, ["m6_p_away", "m6_p_draw", "m6_p_home"])
    delta = abs(b_r - b_t)
    print(f"[BL1 parity] M6: REF={b_r:.15f} TOP5={b_t:.15f} delta={delta:.2e}")
    assert delta < TOL_DETERMINISTIC


def test_bl1_parity_m7_market_residual():
    _need_ref()
    ref = pd.read_csv(REF / "oof_m7_dev_v3.csv", dtype={"season": str})
    top = pd.read_csv(BL1_RES / "oof_m7_dev_v3.csv", dtype={"season": str})
    b_r = _brier_from(ref, ["m7_p_away", "m7_p_draw", "m7_p_home"])
    b_t = _brier_from(top, ["m7_p_away", "m7_p_draw", "m7_p_home"])
    delta = abs(b_r - b_t)
    print(f"[BL1 parity] M7: REF={b_r:.15f} TOP5={b_t:.15f} delta={delta:.2e}")
    assert delta < TOL_LGBM


def test_bl1_parity_phi_selection():
    """Selected phi identical; per-phi brier values may differ (documented)."""
    _need_ref()
    ref = pd.read_csv(REF / "phi_selection_dev.csv")
    top = pd.read_csv(BL1_RES / "phi_selection_dev.csv")
    ref_best = float(ref.loc[ref["brier"].idxmin(), "phi"])
    top_best = float(top.loc[top["brier"].idxmin(), "phi"])
    print(f"[BL1 parity] phi: REF selects={ref_best} TOP5 selects={top_best}")
    assert ref_best == top_best == 0.0012


def test_bl1_parity_matched_preclose_vs_close():
    _need_ref()
    ref = pd.read_csv(REF / "matched_preclose_vs_close.csv")
    top = pd.read_csv(BL1_RES / "matched_preclose_vs_close.csv")
    r = ref.iloc[0]
    t = top.iloc[0]
    for col in ("n_matched",):
        assert int(r[col]) == int(t[col]), f"matched {col}: {r[col]} vs {t[col]}"
    for col in ("delta_brier_point_preclose_minus_close",
                "delta_ci_lo_95", "delta_ci_hi_95"):
        delta = abs(float(r[col]) - float(t[col]))
        print(f"[BL1 parity] matched {col}: REF={r[col]} TOP5={t[col]} "
              f"delta={delta:.2e}")
        assert delta < TOL_MATCHED, f"matched {col} exceeds tolerance"


if __name__ == "__main__":
    # Standalone runner for handoff evidence
    tests = [f for name, f in list(globals().items()) if name.startswith("test_")]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\nBL1 PARITY: {passed}/{passed + failed} tests passed")

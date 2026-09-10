"""Top-5 — Canonical market probability policy (unchanged from BL1 v7).

Frozen reference: BL1 v7 commit 569741b4ad571a38492e4d7cfd014cf82daec396.

Missing-data policy: rows without valid canonical odds are DROPPED
(deterministic, no base-rate fallback). All market-anchored models
(M5, M6, M7) call apply_policy() so they evaluate on identical rows.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CANONICAL_PRECLOSE_SOURCE = "Bookmaker_avg_preclose"
CANONICAL_COLUMNS = ("AvgH", "AvgD", "AvgA")


def _valid_mask(df: pd.DataFrame) -> np.ndarray:
    h, d, a = CANONICAL_COLUMNS
    oh = df[h].to_numpy(dtype=np.float64)
    od = df[d].to_numpy(dtype=np.float64)
    oa = df[a].to_numpy(dtype=np.float64)
    return (np.isfinite(oh) & np.isfinite(od) & np.isfinite(oa)
            & (oh > 1.0) & (od > 1.0) & (oa > 1.0))


def canonical_market_prob_vec(df: pd.DataFrame) -> np.ndarray:
    h, d, a = CANONICAL_COLUMNS
    oh = df[h].to_numpy(dtype=np.float64)
    od = df[d].to_numpy(dtype=np.float64)
    oa = df[a].to_numpy(dtype=np.float64)
    valid = _valid_mask(df)
    inv_h = np.where(valid, 1.0 / oh, np.nan)
    inv_d = np.where(valid, 1.0 / od, np.nan)
    inv_a = np.where(valid, 1.0 / oa, np.nan)
    s = inv_h + inv_d + inv_a
    p_home = inv_h / s
    p_draw = inv_d / s
    p_away = inv_a / s
    return np.stack([p_away, p_draw, p_home], axis=1)


def apply_policy(df: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    valid = _valid_mask(df)
    kept = df[valid].reset_index(drop=True).copy()
    probs = canonical_market_prob_vec(kept)
    return kept, probs


def canonical_market_prob(row: pd.Series) -> tuple[np.ndarray, bool] | None:
    h, d, a = CANONICAL_COLUMNS
    oh, od, oa = row.get(h), row.get(d), row.get(a)
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1.0 / oh, 1.0 / od, 1.0 / oa], dtype=np.float64)
    p = inv / inv.sum()
    return np.array([p[2], p[1], p[0]]), False

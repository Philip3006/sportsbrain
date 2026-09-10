"""FLAGSHIP-BL1 — Canonical market probability policy (v6).

CEO BL1 V6 FINAL STRUCTURAL CORRECTION §4: define ONE shared deterministic
missing-market policy consumed by M5, M6 and M7. All three market-anchored
models must use identical:
  - market probability generation
  - missing-data behavior
  - evaluated-row semantics

Missing-data policy:

    If the canonical pre-closing source is missing for a row, that row is
    DROPPED from the evaluated set. It is neither substituted with a
    base-rate fallback nor silently zero-imputed. Rationale: this is a
    market-ANCHORED model family; if the market anchor is missing, the
    model does not apply for that row. Dropping is deterministic,
    consistent, and preserves the invariant that M5, M6 and M7 evaluate
    on the same match set.

Canonical operational source:

    Bookmaker-average pre-closing (`AvgH / AvgD / AvgA`) with basic
    normalization. See `research/bl1/results/source_governance.md` for the
    multi-criteria rationale (research-baseline framing, not a locked
    production choice — BL1 has no production signal-time contract).

De-vig: basic normalization (locked in v3 61_market_hierarchy_dev.py via
dev-only selection).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CANONICAL_PRECLOSE_SOURCE = "Bookmaker_avg_preclose"
CANONICAL_COLUMNS = ("AvgH", "AvgD", "AvgA")  # home, draw, away pre-closing


def _valid_mask(df: pd.DataFrame) -> np.ndarray:
    h, d, a = CANONICAL_COLUMNS
    oh = df[h].to_numpy(dtype=np.float64)
    od = df[d].to_numpy(dtype=np.float64)
    oa = df[a].to_numpy(dtype=np.float64)
    return (np.isfinite(oh) & np.isfinite(od) & np.isfinite(oa)
            & (oh > 1.0) & (od > 1.0) & (oa > 1.0))


def canonical_market_prob_vec(df: pd.DataFrame) -> np.ndarray:
    """Vectorised probabilities in [p_away, p_draw, p_home] order.
    Rows with missing canonical odds get NaN — use apply_policy() to
    drop them under the unified missing-data policy."""
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
    """Unified missing-data policy for all market-anchored models.

    Returns (kept_df, probs) where:
      - kept_df is the input df restricted to rows with a valid canonical
        market probability, reset_index-dropped
      - probs is an (n_kept, 3) array in [p_away, p_draw, p_home] order

    M5, M6 and M7 MUST call this function so their evaluated row-sets and
    their probability semantics are bit-identical.
    """
    valid = _valid_mask(df)
    kept = df[valid].reset_index(drop=True).copy()
    probs = canonical_market_prob_vec(kept)
    return kept, probs


def canonical_market_prob(row: pd.Series) -> tuple[np.ndarray, bool] | None:
    """Single-row form for legacy call sites. Returns (probs, is_fallback=False)
    or None if the canonical source is missing. Callers using apply_policy()
    should prefer that entry point for consistency."""
    h, d, a = CANONICAL_COLUMNS
    oh, od, oa = row.get(h), row.get(d), row.get(a)
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1.0 / oh, 1.0 / od, 1.0 / oa], dtype=np.float64)
    p_home_draw_away = inv / inv.sum()
    return np.array([p_home_draw_away[2], p_home_draw_away[1], p_home_draw_away[0]]), False

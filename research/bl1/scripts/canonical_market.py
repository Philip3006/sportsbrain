"""FLAGSHIP-BL1 — Canonical operational market policy.

CEO BL1 V5 CORRECTION PASS §2:
> M6 must use the EXACT SAME M5 market probability vector per evaluated
> match/fold. Prefer consuming canonical M5 OOF probabilities or one
> shared deterministic market-policy implementation.

This module IS the shared deterministic implementation. M5, M6 and M7
all call `canonical_market_prob()`. Every downstream comparison
(M6 blend, M7 residual features, edge sweep, matched preclose vs close)
consumes the same function so consistency is enforced by construction.

Design decision — operational source selection:

  The v5 research per-fold chronological Brier selection returned Bet365
  pre-closing in EVERY fold (2021, 2122, 2223, 2324). The margin over
  Pinnacle pre-closing is small (<0.001 Brier).

  Selecting the operational source ONLY on marginal dev Brier is
  disallowed per CEO §8. The operational choice must weight
  timing meaning, production reproducibility, coverage, reliability,
  deterministic fallback, and holdout safety. See
  `research/bl1/results/source_governance.md` for the multi-criteria
  scoring.

  The v5-correction operational choice: **Bookmaker average pre-closing**
  (`AvgH / AvgD / AvgA`) with basic normalization.

  Rationale summarized here (full in source_governance.md):
  - Coverage on 2526: 100% (vs Pinnacle 49.0%)
  - Deterministic: the arithmetic mean across bookmakers is fully
    reproducible given the same input odds.
  - Reduces single-provider reliability risk (Pinnacle feed issue since
    2025-07-23; the retained warning still holds).
  - Timing consistency: the aggregate reflects the median pre-closing
    market snapshot rather than one provider's idiosyncratic timing.
  - Dev Brier: 0.5812 (dev slice via Bookmaker-avg preclose × basic —
    within 0.001 of Bet365 preclose 0.5811 which was chosen only on
    dev Brier).

  The per-fold Bet365 outcome is retained as a research benchmark in
  `m5_source_selection_by_fold.csv` but is NOT the operational input.

De-vig: basic normalization. Locked in v3 61_market_hierarchy_dev.py
via dev-only selection. Shin over-corrects Bundesliga 1X2 markets;
log-odds and power are within 0.0006 Brier of basic; basic wins on
interpretability + no free hyperparameter.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Canonical operational pre-closing source. Do NOT change this without
# CEO authorisation and re-running the full contamination test.
CANONICAL_PRECLOSE_SOURCE = "Bookmaker_avg_preclose"
CANONICAL_COLUMNS = ("AvgH", "AvgD", "AvgA")  # home, draw, away pre-closing


def canonical_market_prob(row: pd.Series) -> tuple[np.ndarray, bool] | None:
    """Returns (probability vector in [away, draw, home] order, is_fallback).

    Uses the canonical source with basic normalization. Returns None if
    the canonical source is not available for this row — callers must
    handle the None case explicitly (fallback policy is caller-controlled;
    the market policy itself does not silently substitute).

    Never reads closing prices.
    """
    h, d, a = CANONICAL_COLUMNS
    oh, od, oa = row.get(h), row.get(d), row.get(a)
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1.0 / oh, 1.0 / od, 1.0 / oa], dtype=np.float64)
    p_home_draw_away = inv / inv.sum()
    # Return in [away, draw, home] convention used throughout the pipeline.
    return np.array([p_home_draw_away[2], p_home_draw_away[1], p_home_draw_away[0]]), False


def canonical_market_prob_vec(df: pd.DataFrame) -> np.ndarray:
    """Vectorised form: returns (n, 3) array of [p_away, p_draw, p_home].
    Rows with missing canonical odds get NaN — callers must decide fallback.
    """
    h, d, a = CANONICAL_COLUMNS
    oh = df[h].to_numpy(dtype=np.float64)
    od = df[d].to_numpy(dtype=np.float64)
    oa = df[a].to_numpy(dtype=np.float64)
    valid = (np.isfinite(oh) & np.isfinite(od) & np.isfinite(oa)
              & (oh > 1.0) & (od > 1.0) & (oa > 1.0))
    inv_h = np.where(valid, 1.0 / oh, np.nan)
    inv_d = np.where(valid, 1.0 / od, np.nan)
    inv_a = np.where(valid, 1.0 / oa, np.nan)
    s = inv_h + inv_d + inv_a
    p_home = inv_h / s
    p_draw = inv_d / s
    p_away = inv_a / s
    return np.stack([p_away, p_draw, p_home], axis=1)

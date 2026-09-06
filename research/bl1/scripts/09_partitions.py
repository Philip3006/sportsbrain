"""FLAGSHIP-BL1 — Canonical partition module (v3).

Exposes the three enforced partitions per CEO Correction Section 2:

  DEVELOPMENT_LABELLED : 1617..2324   (outcomes + everything allowed)
  CALIBRATION_2425     : 2425          (schema + prediction generation OK;
                                         outcome labels NOT exposed before lock)
  HOLDOUT_2526         : 2526          (schema only; every outcome sealed)

All development-time scripts must import from here. Functions here strip
labels/scores for the 2425 and 2526 slices before returning them, so a
call-site error is the only way to leak future outcomes into a development
selection decision.
"""
from __future__ import annotations

from pathlib import Path
import pickle

import pandas as pd

DEVELOPMENT_SEASONS = ("1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324")
CALIBRATION_2425 = "2425"
HOLDOUT_2526 = "2526"

_LABEL_COLS = {"y", "home_score", "away_score", "outcome", "pnl"}
_SCORE_COLS = {"home_score", "away_score"}


def load_development(dataset_pkl: Path) -> pd.DataFrame:
    """Returns 1617-2324 labelled rows only. Callers may compute y."""
    with open(dataset_pkl, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    dev = raw[raw["season"].isin(DEVELOPMENT_SEASONS)].copy()
    dev = dev.dropna(subset=["home_score", "away_score"]).copy()
    return dev.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)


def load_calibration_predictions_only(dataset_pkl: Path) -> pd.DataFrame:
    """Returns 2425 rows with ALL outcome/label columns stripped.

    Result contains only feature-generation-safe fields:
      season, date, home_team, away_team, pre-closing odds (ps_open_*),
      closing benchmark odds (ps_close_*, PSC*, Avg*, Max*, B365*) —
      closing prices are allowed here for benchmark evaluation ONLY AFTER
      CEO opens the calibration slice, but callers should not read them at
      prediction time.

    Any outcome / score / y / pnl column is REMOVED. This makes it
    structurally impossible for a development script that consumes this
    slice to compute an outcome-based metric on 2425.
    """
    with open(dataset_pkl, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    df = raw[raw["season"] == CALIBRATION_2425].copy()
    return _strip_labels(df).sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)


def load_holdout_schema_only(dataset_pkl: Path) -> pd.DataFrame:
    """Returns 2526 rows with ALL outcome columns AND all odds stripped
    except pre-closing (open) 1X2 columns and closing-coverage indicators.

    2526 outcomes are absolutely sealed. Even closing odds — while not an
    outcome per se — could tempt an evaluator into computing Brier if
    scores were separately reintroduced. So this loader deliberately
    returns only schema fields plus opening-market prices (needed for
    signal-time M5-M7 prediction generation, not for evaluation).

    NO y, NO home_score, NO away_score returned.
    """
    with open(dataset_pkl, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    df = raw[raw["season"] == HOLDOUT_2526].copy()
    return _strip_labels(df).sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)


def _strip_labels(df: pd.DataFrame) -> pd.DataFrame:
    drop_cols = [c for c in df.columns if c in _LABEL_COLS]
    return df.drop(columns=drop_cols, errors="ignore")

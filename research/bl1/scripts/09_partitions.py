"""FLAGSHIP-BL1 — Canonical partition module v5 (whitelist enforcement).

Three enforced partitions per CEO Correction Section 2:

  DEVELOPMENT_LABELLED : 1617..2324
  CALIBRATION_2425     : 2425 (predictions-only, outcomes hidden)
  HOLDOUT_2526         : 2526 (schema + signal-time features only)

v5 hardening (CEO Correction — BL1 PARTITION CLOSURE):

  1. Column WHITELIST for sealed partitions. Anything NOT on the whitelist
     is dropped, including columns whose semantics are outcomes we might
     not have named upfront (FTR, HTHG, HTAG, HTR, HS, AS, HST, AST, …
     all appear in football-data.co.uk raw feeds).
  2. Explicit REJECT set for defence in depth: y, home_score, away_score,
     outcome, pnl, FTR, HTHG, HTAG, HTR, and any column containing "score",
     "goal", or "result".
  3. Loaders return partition-tagged DataFrames — every consumer prints
     the partition name on load for logging/audit.

All development-time scripts MUST import from here rather than read the
raw pickle files directly. A call-site error is the only way to leak
future outcomes into a development selection decision.
"""
from __future__ import annotations

from pathlib import Path
import pickle

import pandas as pd

DEVELOPMENT_SEASONS = ("1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324")
CALIBRATION_2425 = "2425"
HOLDOUT_2526 = "2526"

# Signal-time-safe columns that MAY appear in sealed-partition outputs.
# Everything else is dropped.
SEALED_WHITELIST = frozenset({
    "season", "date", "home_team", "away_team",
    # Signal-time pre-closing odds (kept for M5-M7 prediction generation).
    # Pinnacle:
    "ps_open_home", "ps_open_draw", "ps_open_away",
    "PSH", "PSD", "PSA",
    # Bookmaker aggregates and other bookmakers, pre-closing:
    "AvgH", "AvgD", "AvgA", "MaxH", "MaxD", "MaxA",
    "B365H", "B365D", "B365A", "BWH", "BWD", "BWA",
    "IWH", "IWD", "IWA", "WHH", "WHD", "WHA", "VCH", "VCD", "VCA",
    "LBH", "LBD", "LBA",
    # Closing odds (kept for CLV BENCHMARK reporting only — must not enter
    # as prediction features; enforced by test_07 invariant).
    "ps_close_home", "ps_close_draw", "ps_close_away",
    "PSCH", "PSCD", "PSCA",
    "AvgCH", "AvgCD", "AvgCA", "MaxCH", "MaxCD", "MaxCA",
    "B365CH", "B365CD", "B365CA",
})

# Explicit deny-list. Kept as belt-and-braces even though the whitelist
# would already drop these; used by the invariant test to prove intent.
SEALED_REJECT = frozenset({
    "y", "home_score", "away_score", "outcome", "pnl",
    "FTR", "FTHG", "FTAG", "HTR", "HTHG", "HTAG",
    "HS", "AS", "HST", "AST",
})


def _is_outcome_named(col: str) -> bool:
    c = col.lower()
    return any(tok in c for tok in ("score", "goal", "result", "outcome", "pnl"))


def load_development(dataset_pkl: Path) -> pd.DataFrame:
    """Returns 1617-2324 labelled rows. All columns kept; callers may
    compute y and use scores as needed."""
    with open(dataset_pkl, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    dev = raw[raw["season"].isin(DEVELOPMENT_SEASONS)].copy()
    dev = dev.dropna(subset=["home_score", "away_score"]).copy()
    dev = dev.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    print(f"[partitions] DEVELOPMENT_LABELLED loaded: {len(dev)} rows, "
          f"seasons {sorted(dev['season'].unique())}", flush=True)
    return dev


def load_calibration_predictions_only(dataset_pkl: Path) -> pd.DataFrame:
    """Returns 2425 rows with SEALED_WHITELIST applied.

    All outcome / score / y / pnl / FTR / HTHG / HTAG / HTR columns are
    absent. Even any future column with "score", "goal", "result",
    "outcome", or "pnl" in its name is dropped.

    This makes it structurally impossible for a downstream development
    script that consumes this slice to compute an outcome-based metric on
    2425. The slice is safe to pass to a prediction generator.
    """
    with open(dataset_pkl, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    df = raw[raw["season"] == CALIBRATION_2425].copy()
    df = _apply_sealed_whitelist(df, "CALIBRATION_2425")
    df = df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    return df


def load_holdout_schema_only(dataset_pkl: Path) -> pd.DataFrame:
    """Returns 2526 rows with SEALED_WHITELIST applied.

    Same treatment as CALIBRATION_2425. Every outcome / score column is
    dropped. Signal-time odds and closing-market-coverage-diagnostic
    columns are retained so downstream can produce M5-M7 predictions on
    2526 (allowed) and report closing-odds coverage (allowed as schema
    inspection). No y-based metric can be computed by any caller of this
    loader.
    """
    with open(dataset_pkl, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    df = raw[raw["season"] == HOLDOUT_2526].copy()
    df = _apply_sealed_whitelist(df, "HOLDOUT_2526")
    df = df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    return df


def _apply_sealed_whitelist(df: pd.DataFrame, partition: str) -> pd.DataFrame:
    """Enforce the SEALED_WHITELIST + explicit REJECT + name-heuristic."""
    kept = []
    dropped = []
    for c in df.columns:
        if c in SEALED_REJECT:
            dropped.append(c); continue
        if _is_outcome_named(c):
            dropped.append(c); continue
        if c not in SEALED_WHITELIST:
            dropped.append(c); continue
        kept.append(c)
    print(f"[partitions] {partition} loaded: {len(df)} rows, "
          f"kept={len(kept)} cols, dropped={len(dropped)} cols", flush=True)
    return df[kept].copy()

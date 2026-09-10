"""FLAGSHIP-BL1 — Canonical partition module v6 (structural closure).

Three enforced partitions:

  DEVELOPMENT_LABELLED : 1617..2324
  CALIBRATION_2425     : 2425 (predictions-only, outcomes AND closing values hidden)
  HOLDOUT_2526         : 2526 (schema + signal-time pre-close features only,
                                outcomes AND closing values hidden)

v6 hardening (CEO BL1 V6 FINAL STRUCTURAL CORRECTION):

  1. SEALED_WHITELIST no longer includes closing-price columns. 2425 pre-lock
     and 2526 pre-open loaders never expose closing prices to downstream
     research code. Closing coverage may only be audited via the dedicated
     `holdout_closing_coverage_diagnostics()` helper which returns
     diagnostics (source, columns_present, coverage counts) — NOT price
     values.
  2. `load_development_with_market()` centralises DEV raw-pickle access +
     canonical/research market column join, so M5/M6/M7 do not need to open
     the raw pickle files themselves.
  3. `load_development_closing_prices()` similarly returns DEV closing
     benchmark columns; DEV closing values ARE allowed (dev seasons are
     unsealed) and are used by 17_matched_preclose_vs_close.
"""
from __future__ import annotations

from pathlib import Path
import pickle

import pandas as pd

DEVELOPMENT_SEASONS = ("1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324")
CALIBRATION_2425 = "2425"
HOLDOUT_2526 = "2526"

# Signal-time-safe columns that MAY appear in sealed-partition outputs.
# Closing-price columns are explicitly excluded (structural non-exposure).
SEALED_WHITELIST = frozenset({
    "season", "date", "home_team", "away_team",
    # Signal-time PRE-CLOSING odds only.
    "ps_open_home", "ps_open_draw", "ps_open_away",
    "PSH", "PSD", "PSA",
    "AvgH", "AvgD", "AvgA", "MaxH", "MaxD", "MaxA",
    "B365H", "B365D", "B365A", "BWH", "BWD", "BWA",
    "IWH", "IWD", "IWA", "WHH", "WHD", "WHA", "VCH", "VCD", "VCA",
    "LBH", "LBD", "LBA",
})

# Explicit deny-list. Belt-and-braces even though the whitelist would drop
# these; used by invariant tests to prove intent.
SEALED_REJECT = frozenset({
    "y", "home_score", "away_score", "outcome", "pnl",
    "FTR", "FTHG", "FTAG", "HTR", "HTHG", "HTAG",
    "HS", "AS", "HST", "AST",
    # Closing prices — must not enter sealed loaders' returned DataFrames.
    "ps_close_home", "ps_close_draw", "ps_close_away",
    "PSCH", "PSCD", "PSCA",
    "AvgCH", "AvgCD", "AvgCA", "MaxCH", "MaxCD", "MaxCA",
    "B365CH", "B365CD", "B365CA",
})

# Closing-price column identifiers (used by the coverage helper only).
_CLOSING_SOURCES = (
    ("Pinnacle_closing", ("PSCH", "PSCD", "PSCA")),
    ("Bookmaker_avg_closing", ("AvgCH", "AvgCD", "AvgCA")),
    ("Bookmaker_max_closing", ("MaxCH", "MaxCD", "MaxCA")),
    ("Bet365_closing", ("B365CH", "B365CD", "B365CA")),
)
_PRECLOSE_SOURCES = (
    ("Pinnacle_preclose", ("PSH", "PSD", "PSA")),
    ("Bookmaker_avg_preclose", ("AvgH", "AvgD", "AvgA")),
    ("Bookmaker_max_preclose", ("MaxH", "MaxD", "MaxA")),
    ("Bet365_preclose", ("B365H", "B365D", "B365A")),
)

# Research market columns that may be joined onto DEVELOPMENT rows.
_DEV_MARKET_PRECLOSE_COLS = ("PSH", "PSD", "PSA",
                              "AvgH", "AvgD", "AvgA",
                              "MaxH", "MaxD", "MaxA",
                              "B365H", "B365D", "B365A")
_DEV_MARKET_CLOSING_COLS = ("PSCH", "PSCD", "PSCA",
                             "AvgCH", "AvgCD", "AvgCA",
                             "MaxCH", "MaxCD", "MaxCA",
                             "B365CH", "B365CD", "B365CA")


def _is_outcome_named(col: str) -> bool:
    c = col.lower()
    return any(tok in c for tok in ("score", "goal", "result", "outcome", "pnl"))


def _read_pkl(path: Path) -> pd.DataFrame:
    """Single controlled pickle read inside the trusted partition boundary."""
    with open(path, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    return raw


def load_development(dataset_pkl: Path) -> pd.DataFrame:
    """Returns 1617-2324 labelled rows (all columns kept — dev is unsealed)."""
    raw = _read_pkl(dataset_pkl)
    dev = raw[raw["season"].isin(DEVELOPMENT_SEASONS)].copy()
    dev = dev.dropna(subset=["home_score", "away_score"]).copy()
    dev = dev.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    print(f"[partitions] DEVELOPMENT_LABELLED loaded: {len(dev)} rows, "
          f"seasons {sorted(dev['season'].unique())}", flush=True)
    return dev


def load_development_with_market(dataset_pkl: Path,
                                  full_dataset_pkl: Path,
                                  include_closing: bool = False) -> pd.DataFrame:
    """DEV rows joined with pre-closing research market columns from the
    fuller dataset. Closing prices are included only when explicitly requested
    (dev seasons are unsealed — closing values on DEV are allowed for
    benchmark/CLV research).

    Returns a DataFrame containing DEV outcome columns (home_score, away_score)
    AND market columns. This is the single entry point M5/M6/M7 use so those
    scripts do not need direct raw-pickle access.
    """
    dev = load_development(dataset_pkl)
    full = _read_pkl(full_dataset_pkl)
    full["date"] = pd.to_datetime(full["date"])
    dev["date"] = pd.to_datetime(dev["date"])
    cols = list(_DEV_MARKET_PRECLOSE_COLS)
    if include_closing:
        cols += list(_DEV_MARKET_CLOSING_COLS)
    join_cols = [c for c in cols if c in full.columns]
    merged = dev.merge(
        full[["date", "home_team", "away_team"] + join_cols],
        on=["date", "home_team", "away_team"], how="left", suffixes=("", "_full"),
    )
    print(f"[partitions] DEVELOPMENT+market loaded: {len(merged)} rows, "
          f"{len(join_cols)} market cols (include_closing={include_closing})", flush=True)
    return merged


def load_development_closing_prices(dataset_pkl: Path,
                                     full_dataset_pkl: Path) -> pd.DataFrame:
    """DEV rows joined with closing-price columns. DEV is unsealed so this
    is allowed. Used exclusively by 17_matched_preclose_vs_close for the
    matched-sample closing benchmark.
    """
    return load_development_with_market(dataset_pkl, full_dataset_pkl, include_closing=True)


def load_calibration_predictions_only(dataset_pkl: Path) -> pd.DataFrame:
    """Returns 2425 rows with SEALED_WHITELIST applied.

    No outcome columns. NO closing-price columns. Only signal-time
    pre-closing odds and match identifiers.
    """
    raw = _read_pkl(dataset_pkl)
    df = raw[raw["season"] == CALIBRATION_2425].copy()
    df = _apply_sealed_whitelist(df, "CALIBRATION_2425")
    df = df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    return df


def load_holdout_schema_only(dataset_pkl: Path) -> pd.DataFrame:
    """Returns 2526 rows with SEALED_WHITELIST applied.

    No outcome columns. NO closing-price columns. Only signal-time
    pre-closing odds and match identifiers.
    """
    raw = _read_pkl(dataset_pkl)
    df = raw[raw["season"] == HOLDOUT_2526].copy()
    df = _apply_sealed_whitelist(df, "HOLDOUT_2526")
    df = df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    return df


def holdout_closing_coverage_diagnostics(full_dataset_pkl: Path) -> pd.DataFrame:
    """Dedicated helper: closing-price coverage on 2526, DIAGNOSTICS ONLY.

    Inspects closing-price source fields inside the trusted partition
    boundary. Returns per-source rows with:

        source, columns_present, coverage, n_covered, n_missing, n_total

    NO price values are returned. The caller receives only counts. This
    exists because the sealed loaders (load_holdout_schema_only) do not
    expose closing columns to downstream research code, but a coverage
    audit is still permitted as schema-level diagnostics.
    """
    raw = _read_pkl(full_dataset_pkl)
    df = raw[raw["season"] == HOLDOUT_2526].copy()
    n_total = len(df)
    rows = []
    for name, cols in _PRECLOSE_SOURCES + _CLOSING_SOURCES:
        present = all(c in df.columns for c in cols)
        if not present:
            rows.append({"source": name, "columns_present": False,
                         "coverage": 0.0, "n_covered": 0,
                         "n_missing": n_total, "n_total": n_total})
            continue
        n_covered = int(df[list(cols)].dropna().shape[0])
        rows.append({
            "source": name, "columns_present": True,
            "coverage": n_covered / max(n_total, 1),
            "n_covered": n_covered,
            "n_missing": n_total - n_covered,
            "n_total": n_total,
        })
    return pd.DataFrame(rows).sort_values("coverage", ascending=False).reset_index(drop=True)


def _apply_sealed_whitelist(df: pd.DataFrame, partition: str) -> pd.DataFrame:
    """Enforce SEALED_WHITELIST + explicit REJECT + name-heuristic."""
    kept, dropped = [], []
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

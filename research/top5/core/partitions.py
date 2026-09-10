"""Top-5 — Generic partition module (parameterized on LeagueConfig).

Direct generalization of BL1 v7 09_partitions.py. Same sealed-partition
guarantees:
  - DEVELOPMENT_LABELLED : dev seasons, all columns kept
  - CALIBRATION          : sealed — outcomes AND closing prices dropped
  - HOLDOUT              : sealed — outcomes AND closing prices dropped
  - closing-coverage-diagnostics helper returns counts only, no prices
"""
from __future__ import annotations

import pickle
from pathlib import Path

import pandas as pd

# Signal-time-safe columns. Closing prices are excluded (structural non-exposure).
SEALED_WHITELIST = frozenset({
    "season", "date", "home_team", "away_team",
    "ps_open_home", "ps_open_draw", "ps_open_away",
    "PSH", "PSD", "PSA",
    "AvgH", "AvgD", "AvgA", "MaxH", "MaxD", "MaxA",
    "B365H", "B365D", "B365A", "BWH", "BWD", "BWA",
    "IWH", "IWD", "IWA", "WHH", "WHD", "WHA", "VCH", "VCD", "VCA",
    "LBH", "LBD", "LBA",
})

SEALED_REJECT = frozenset({
    "y", "home_score", "away_score", "outcome", "pnl",
    "FTR", "FTHG", "FTAG", "HTR", "HTHG", "HTAG",
    "HS", "AS", "HST", "AST",
    "ps_close_home", "ps_close_draw", "ps_close_away",
    "PSCH", "PSCD", "PSCA",
    "AvgCH", "AvgCD", "AvgCA", "MaxCH", "MaxCD", "MaxCA",
    "B365CH", "B365CD", "B365CA",
})

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
    with open(path, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    return raw


def load_development(dataset_pkl: Path, dev_seasons: tuple[str, ...]) -> pd.DataFrame:
    raw = _read_pkl(dataset_pkl)
    dev = raw[raw["season"].isin(dev_seasons)].copy()
    dev = dev.dropna(subset=["home_score", "away_score"]).copy()
    dev = dev.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    print(f"[partitions] DEVELOPMENT_LABELLED loaded: {len(dev)} rows, "
          f"seasons {sorted(dev['season'].unique())}", flush=True)
    return dev


def load_development_with_market(dataset_pkl: Path, full_dataset_pkl: Path,
                                  dev_seasons: tuple[str, ...],
                                  include_closing: bool = False) -> pd.DataFrame:
    dev = load_development(dataset_pkl, dev_seasons)
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
    print(f"[partitions] DEV+market loaded: {len(merged)} rows, "
          f"{len(join_cols)} market cols (include_closing={include_closing})", flush=True)
    return merged


def load_development_closing_prices(dataset_pkl: Path, full_dataset_pkl: Path,
                                     dev_seasons: tuple[str, ...]) -> pd.DataFrame:
    return load_development_with_market(dataset_pkl, full_dataset_pkl, dev_seasons, include_closing=True)


def load_calibration_predictions_only(dataset_pkl: Path, calibration_season: str) -> pd.DataFrame:
    raw = _read_pkl(dataset_pkl)
    df = raw[raw["season"] == calibration_season].copy()
    df = _apply_sealed_whitelist(df, f"CALIBRATION_{calibration_season}")
    df = df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    return df


def load_holdout_schema_only(dataset_pkl: Path, holdout_season: str) -> pd.DataFrame:
    raw = _read_pkl(dataset_pkl)
    df = raw[raw["season"] == holdout_season].copy()
    df = _apply_sealed_whitelist(df, f"HOLDOUT_{holdout_season}")
    df = df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    return df


def holdout_closing_coverage_diagnostics(full_dataset_pkl: Path, holdout_season: str) -> pd.DataFrame:
    raw = _read_pkl(full_dataset_pkl)
    df = raw[raw["season"] == holdout_season].copy()
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


def compute_season_starts_from_data(dataset_pkl: Path, seasons: tuple[str, ...]) -> dict[str, pd.Timestamp]:
    """Derive per-league causal boundaries from minimum valid match date per season.

    Returns a dict mapping season code -> first match date (Timestamp).
    This is the authoritative causal cutoff for DC snapshot causality checks:
    snapshot fit_date must be <= season_start (i.e. fit strictly on prior data).
    """
    raw = _read_pkl(dataset_pkl)
    raw["date"] = pd.to_datetime(raw["date"])
    raw = raw.dropna(subset=["home_score", "away_score"])
    out: dict[str, pd.Timestamp] = {}
    for s in seasons:
        sub = raw[raw["season"] == s]
        if len(sub):
            out[s] = sub["date"].min()
    return out


def _apply_sealed_whitelist(df: pd.DataFrame, partition: str) -> pd.DataFrame:
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

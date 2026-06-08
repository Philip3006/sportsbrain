"""
Jeff Sackmann ATP match data fetcher.
Source: https://github.com/JeffSackmann/tennis_atp (public domain)
Columns used: tourney_date, tourney_name, tourney_level, surface,
              winner_name, loser_name, score, round, winner_rank, loser_rank
"""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import requests

from src.data.cache import disk_cache

_SACKMANN_URL = (
    "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv"
)
_YEARS = range(2019, 2027)  # 2019-2026 inclusive (adds Wimbledon 2019, 2021)

# Only keep columns we actually use — reduces memory and cache size
_KEEP_COLS = [
    "tourney_date", "tourney_name", "tourney_level", "surface",
    "winner_name", "loser_name", "score", "round",
    "winner_rank", "loser_rank",
]


@disk_cache("tennis_atp_matches", max_age_hours=24.0)
def fetch_atp_matches() -> pd.DataFrame:
    """
    Downloads Jeff Sackmann ATP match CSVs for 2022-2026.
    Returns a DataFrame sorted by date with standardised columns.
    Cached for 24 hours.
    """
    frames: list[pd.DataFrame] = []
    for year in _YEARS:
        url = _SACKMANN_URL.format(year=year)
        try:
            resp = requests.get(url, timeout=15)
            if not resp.ok:
                continue
            df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
            available = [c for c in _KEEP_COLS if c in df.columns]
            df = df[available].copy()
            frames.append(df)
        except Exception:
            continue

    if not frames:
        return pd.DataFrame(columns=_KEEP_COLS)

    combined = pd.concat(frames, ignore_index=True)

    # Normalise date: tourney_date is YYYYMMDD int → datetime
    combined["tourney_date"] = pd.to_datetime(
        combined["tourney_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    combined = combined.dropna(subset=["tourney_date"]).sort_values("tourney_date")
    combined["surface"] = combined["surface"].str.lower().fillna("unknown")

    return combined.reset_index(drop=True)


def grass_matches(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Returns only grass-court matches (Wimbledon surface)."""
    if df is None:
        df = fetch_atp_matches()
    return df[df["surface"] == "grass"].copy()


def wimbledon_matches(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Returns only Wimbledon matches specifically."""
    if df is None:
        df = fetch_atp_matches()
    mask = df["tourney_name"].str.contains("Wimbledon", case=False, na=False)
    return df[mask].copy()

"""
Historical tennis odds from tennis-data.co.uk (Grand Slams, 2019-2025).
ATP:  http://www.tennis-data.co.uk/{year}/{tournament}.csv
WTA:  http://www.tennis-data.co.uk/{year}w/{tournament}.csv

Columns used: Date, Winner, Loser, WRank, LRank, Round, Surface, B365W, B365L
"""
from __future__ import annotations

import io
import time
from pathlib import Path

import pandas as pd
import requests

from src.config import DATA_CACHE

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
)

_BASE = "http://www.tennis-data.co.uk"
_TOURNAMENTS = ["wimbledon", "usopen", "ausopen", "frenchopen"]
_YEARS = range(2019, 2026)  # 2019-2025 (2020 has no Wimbledon)

_SURFACE_MAP = {
    "wimbledon":   "grass",
    "usopen":      "hard",
    "ausopen":     "hard",
    "frenchopen":  "clay",
}

_KEEP = ["Date", "Winner", "Loser", "WRank", "LRank", "Round",
         "Surface", "B365W", "B365L", "AvgW", "AvgL", "MaxW", "MaxL",
         "Wsets", "Lsets", "Best of"]


def _fetch_one(year: int, tournament: str, tour: str) -> pd.DataFrame | None:
    path = f"{year}w" if tour == "wta" else str(year)
    url = f"{_BASE}/{path}/{tournament}.csv"
    try:
        r = _SESSION.get(url, timeout=10)
        if not r.ok or len(r.content) < 200:
            return None
        df = pd.read_csv(io.StringIO(r.text), on_bad_lines="skip")
        if "Winner" not in df.columns or "B365W" not in df.columns:
            return None
        available = [c for c in _KEEP if c in df.columns]
        df = df[available].copy()
        df["year"] = year
        df["tournament"] = tournament
        df["tour"] = tour.upper()
        df["surface_std"] = _SURFACE_MAP.get(tournament, "unknown")
        return df
    except Exception:
        return None


def fetch_grand_slam_odds(
    tours: list[str] | None = None,
    years: range | list[int] | None = None,
    cache: bool = True,
) -> pd.DataFrame:
    """
    Downloads all available Grand Slam odds CSVs from tennis-data.co.uk.
    Caches result to data/cache/tennis_gs_odds.pkl.
    Returns combined DataFrame sorted by date.
    """
    cache_path = DATA_CACHE / "tennis_gs_odds.pkl"
    if cache and cache_path.exists():
        age_h = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_h < 24:
            return pd.read_pickle(cache_path)

    tours = tours or ["atp", "wta"]
    years = years or _YEARS
    frames: list[pd.DataFrame] = []

    for tour in tours:
        for year in years:
            for t in _TOURNAMENTS:
                df = _fetch_one(year, t, tour)
                if df is not None:
                    frames.append(df)
                    time.sleep(0.25)  # polite rate limiting

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined["Date"] = pd.to_datetime(combined["Date"], dayfirst=True, errors="coerce")
    combined = combined.dropna(subset=["Date", "Winner", "B365W", "B365L"])
    combined["B365W"] = pd.to_numeric(combined["B365W"], errors="coerce")
    combined["B365L"] = pd.to_numeric(combined["B365L"], errors="coerce")
    combined = combined.dropna(subset=["B365W", "B365L"])
    combined = combined.sort_values("Date").reset_index(drop=True)

    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    combined.to_pickle(cache_path)
    return combined

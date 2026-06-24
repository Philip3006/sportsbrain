"""
Historical tennis odds from tennis-data.co.uk.

Two access patterns:
1. Per-tournament Grand-Slam-CSVs (legacy, 2019-2025):
     ATP:  http://www.tennis-data.co.uk/{year}/{tournament}.csv
     WTA:  http://www.tennis-data.co.uk/{year}w/{tournament}.csv
2. Annual full-tour Excel-Files (Phase J2-B, ab 2001 ATP / 2007 WTA):
     ATP:  http://www.tennis-data.co.uk/{year}/{year}.xlsx
     WTA:  http://www.tennis-data.co.uk/{year}w/{year}.xlsx

Columns used: Date, Winner, Loser, WRank, LRank, Round, Surface, B365W, B365L,
              AvgW, AvgL, MaxW, MaxL, Wsets, Lsets, Best of, Tournament, Series
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
         "Wsets", "Lsets", "Best of",
         # Per-Set game scores (J2-G full-backtest, optional)
         "W1", "L1", "W2", "L2", "W3", "L3", "W4", "L4", "W5", "L5"]


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


# ---------------------------------------------------------------------------
# Phase J2-B — Full-Tour-Quoten (Annual XLSX)
# ---------------------------------------------------------------------------

# Mapping von tennis-data.co.uk "Series"-Spalte → interne Kategorie aus Registry.
# Quelle: tennis-data.co.uk-Headerzeile (Spalte "Series" für ATP, ähnlich WTA).
_SERIES_TO_CATEGORY: dict[str, str] = {
    "Grand Slam":           "grand_slam",
    "Masters 1000":         "m1000",
    "Masters Cup":          "tour_final",  # ATP Finals (old name)
    "ATP Finals":           "tour_final",
    "ATP500":               "atp500",
    "International Gold":   "atp500",        # alter Series-Name vor 2009
    "ATP250":               "atp250",
    # WTA-Series-Namen
    "Premier Mandatory":    "wta1000",
    "Premier 5":            "wta1000",
    "Premier":              "wta500",
    "WTA1000":              "wta1000",
    "WTA500":               "wta500",
    "WTA250":               "wta250",
    "Tour Championships":   "tour_final",
    "WTA Finals":           "tour_final",
    # 'International' ist tour-abhängig (siehe categorize_series)
}


def categorize_series(series: str, tour: str) -> str:
    """tennis-data Series-String → interne Kategorie.

    Fallback bei unbekannten Strings: 'atp250'/'wta250' (konservativ höchste min_edge).
    'International' ist tour-abhängig: ATP=atp250, WTA=wta250.
    """
    is_wta = tour.lower() == "wta"
    default = "wta250" if is_wta else "atp250"
    if not isinstance(series, str):
        return default
    s = series.strip()
    if s == "International":
        return default
    return _SERIES_TO_CATEGORY.get(s, default)


def _fetch_full_year_xlsx(year: int, tour: str) -> pd.DataFrame | None:
    """Lädt tennis-data.co.uk annual full-tour XLSX. Returns None bei Fehler."""
    path = f"{year}w" if tour.lower() == "wta" else str(year)
    url = f"{_BASE}/{path}/{year}.xlsx"
    try:
        r = _SESSION.get(url, timeout=20)
        if not r.ok or len(r.content) < 1000:
            return None
        df = pd.read_excel(io.BytesIO(r.content), engine="openpyxl")
        if "Winner" not in df.columns or "B365W" not in df.columns:
            return None
        # Spalten-Normalisierung (Dedup gegen Duplikate in _KEEP)
        keep_extra = ["Tournament", "Series", "Tier", "Court"]
        seen: set[str] = set()
        keep: list[str] = []
        for c in _KEEP + keep_extra:
            if c in df.columns and c not in seen:
                keep.append(c); seen.add(c)
        df = df[keep].copy()
        df["year"] = year
        df["tour"] = tour.upper()
        # Surface normalisieren
        if "Surface" in df.columns:
            df["surface_std"] = df["Surface"].astype(str).str.lower().str.strip()
        return df
    except Exception:
        return None


def fetch_full_tour_odds(
    tours: list[str] | None = None,
    years: range | list[int] | None = None,
    cache: bool = True,
) -> pd.DataFrame:
    """Lädt full-tour annual XLSX-Files (alle ATP/WTA-Turniere inkl. Series-Info).

    Cache: data/cache/tennis_full_tour_odds.pkl, 24h TTL.
    """
    cache_path = DATA_CACHE / "tennis_full_tour_odds.pkl"
    if cache and cache_path.exists():
        age_h = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_h < 24:
            return pd.read_pickle(cache_path)

    tours = tours or ["atp", "wta"]
    years = years or _YEARS
    frames: list[pd.DataFrame] = []

    for tour in tours:
        for year in years:
            df = _fetch_full_year_xlsx(year, tour)
            if df is not None and not df.empty:
                frames.append(df)
                time.sleep(0.25)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined["Date"] = pd.to_datetime(combined["Date"], dayfirst=True, errors="coerce")
    combined = combined.dropna(subset=["Date", "Winner", "B365W", "B365L"])
    combined["B365W"] = pd.to_numeric(combined["B365W"], errors="coerce")
    combined["B365L"] = pd.to_numeric(combined["B365L"], errors="coerce")
    combined = combined.dropna(subset=["B365W", "B365L"])

    # Kategorie-Spalte ableiten (ATP: 'Series', WTA: 'Tier')
    def _category_from_row(r):
        for col in ("Series", "Tier"):
            val = r.get(col)
            if isinstance(val, str) and val.strip():
                return categorize_series(val, r.get("tour", "ATP"))
        return "wta250" if str(r.get("tour", "ATP")).lower() == "wta" else "atp250"

    combined["category"] = combined.apply(_category_from_row, axis=1)

    combined = combined.sort_values("Date").reset_index(drop=True)

    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    combined.to_pickle(cache_path)
    return combined

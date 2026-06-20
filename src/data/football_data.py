"""
Fetches historical match odds from football-data.co.uk free CSVs.
Relevant for CLV backtest: columns PSCH/PSCD/PSCA = Pinnacle closing odds.
"""
import io

import pandas as pd
import requests

from scripts._http_retry import retry_request
from src.config import DATA_RAW, FBDATA_BASE, canonical_name
from src.data.cache import disk_cache

# International tournaments are not on football-data.co.uk directly.
# We use it solely for Pinnacle closing line data on club matches
# as a proxy calibration dataset for the CLV backtest framework.
# For WC/EURO odds, we use stored snapshots or TheOddsAPI.

# Season codes used by football-data.co.uk (e.g. "2324" = 2023/24)
AVAILABLE_LEAGUES = {
    "E0": "English Premier League",
    "SP1": "Spanish La Liga",
    "D1": "German Bundesliga",
    "I1": "Italian Serie A",
    "F1": "French Ligue 1",
}


def _season_url(league: str, season: str) -> str:
    return f"{FBDATA_BASE}/{season}/{league}.csv"


def fetch_season(
    league: str,
    season: str,
    force: bool = False,
) -> pd.DataFrame | None:
    """
    Downloads one league-season CSV.
    Returns DataFrame with standardized columns, or None if unavailable.
    Cached at data/cache/fd_{league}_{season}.pkl for 30 days.
    """
    cache_name = f"fd_{league}_{season}"

    # Check cache manually (disk_cache decorator doesn't handle None return well)
    import pickle, time
    from src.config import DATA_CACHE
    cache_path = DATA_CACHE / f"{cache_name}.pkl"

    if not force and cache_path.exists():
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < 24 * 30:
            with open(cache_path, "rb") as f:
                return pickle.load(f)

    url = _season_url(league, season)
    try:
        resp = retry_request("GET",url, timeout=20)
        resp.raise_for_status()
    except Exception:
        return None

    df = pd.read_csv(io.StringIO(resp.text), low_memory=False)

    # Keep only relevant columns
    keep = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG",
            "PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA"}
    existing = keep & set(df.columns)
    df = df[list(existing)].copy()

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date"])

    rename = {
        "Date": "date", "HomeTeam": "home_team", "AwayTeam": "away_team",
        "FTHG": "home_score", "FTAG": "away_score",
        "PSH": "ps_open_home", "PSD": "ps_open_draw", "PSA": "ps_open_away",
        "PSCH": "ps_close_home", "PSCD": "ps_close_draw", "PSCA": "ps_close_away",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    if "home_team" in df.columns:
        df["home_team"] = df["home_team"].map(canonical_name)
        df["away_team"] = df["away_team"].map(canonical_name)

    df = df.sort_values("date").reset_index(drop=True)

    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump(df, f)

    return df


def fetch_odds_history(
    leagues: list[str] | None = None,
    seasons: list[str] | None = None,
) -> pd.DataFrame:
    """
    Concatenates all available league-season files into one odds frame.
    Default: last 3 seasons of the 5 major leagues.
    Returns DataFrame with Pinnacle closing columns for CLV computation.
    """
    if leagues is None:
        leagues = list(AVAILABLE_LEAGUES.keys())
    if seasons is None:
        seasons = ["2122", "2223", "2324", "2425"]

    frames = []
    for league in leagues:
        for season in seasons:
            df = fetch_season(league, season)
            if df is not None and not df.empty:
                df["league"] = league
                df["season"] = season
                frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)

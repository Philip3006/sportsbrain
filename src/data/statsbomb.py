"""
StatsBomb open-data xG fetcher.

Fetches match-level xG (summed from shot events) for WC/EURO/Copa matches.
Free data from: https://github.com/statsbomb/open-data

Data is cached as pickle (24h TTL). Rate-limited to 0.5s between requests.
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

from src.config import DATA_CACHE

_CACHE_PATH = DATA_CACHE / "statsbomb_xg.pkl"
_CACHE_MAX_AGE_H = 24

# Target competition IDs — season IDs are discovered dynamically from StatsBomb
_SB_COMPETITION_IDS = {43, 55, 223}  # FIFA World Cup, UEFA Euro, Copa América

_SB_BASE = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
_TOURNAMENT_NAMES = {
    43:  "FIFA World Cup",
    55:  "UEFA Euro",
    223: "Copa América",
}


def _discover_competitions() -> dict[int, list[int]]:
    """Fetches competitions.json from StatsBomb and returns {comp_id: [season_ids]}."""
    url = f"{_SB_BASE}/competitions.json"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        comps = resp.json()
    except Exception:
        # Fallback to known seasons if GitHub is unreachable
        return {43: [106, 107], 55: [43, 282], 223: [282]}

    result: dict[int, list[int]] = {}
    for c in comps:
        cid = c.get("competition_id")
        sid = c.get("season_id")
        if cid in _SB_COMPETITION_IDS and sid is not None:
            result.setdefault(cid, []).append(sid)
    return result


def _cache_is_fresh() -> bool:
    if not _CACHE_PATH.exists():
        return False
    age_h = (time.time() - _CACHE_PATH.stat().st_mtime) / 3600
    return age_h < _CACHE_MAX_AGE_H


def _fetch_match_xg(competition_id: int, season_id: int) -> list[dict]:
    """Fetches all match xG for one competition/season."""
    matches_url = f"{_SB_BASE}/matches/{competition_id}/{season_id}.json"
    try:
        resp = requests.get(matches_url, timeout=15)
        resp.raise_for_status()
        matches = resp.json()
    except Exception:
        return []

    rows = []
    for match in matches:
        match_id = match.get("match_id")
        if match_id is None:
            continue

        home = match.get("home_team", {}).get("home_team_name", "")
        away = match.get("away_team", {}).get("away_team_name", "")
        date_str = match.get("match_date", "")
        try:
            date = pd.Timestamp(date_str)
        except Exception:
            continue

        # Fetch events for this match to get shot xG
        time.sleep(0.5)  # rate limit
        events_url = f"{_SB_BASE}/events/{match_id}.json"
        try:
            ev_resp = requests.get(events_url, timeout=20)
            ev_resp.raise_for_status()
            events = ev_resp.json()
        except Exception:
            continue

        home_xg, away_xg = 0.0, 0.0
        for event in events:
            if event.get("type", {}).get("name") != "Shot":
                continue
            xg = event.get("shot", {}).get("statsbomb_xg", 0.0) or 0.0
            team = event.get("team", {}).get("name", "")
            if team == home:
                home_xg += xg
            elif team == away:
                away_xg += xg

        rows.append({
            "home_team": home,
            "away_team": away,
            "date": date,
            "home_xg": home_xg,
            "away_xg": away_xg,
            "tournament": _TOURNAMENT_NAMES.get(competition_id, ""),
        })

    return rows


def fetch_statsbomb_xg(force: bool = False) -> pd.DataFrame:
    """
    Returns DataFrame with columns: home_team, away_team, date, home_xg, away_xg, tournament.
    Fetches from StatsBomb open-data GitHub; cached 24h.
    """
    if not force and _cache_is_fresh():
        return pd.read_pickle(_CACHE_PATH)

    print("Fetching StatsBomb xG data (this may take 1-3 minutes)...")
    competitions = _discover_competitions()

    # Warn about new unknown seasons (e.g. WM 2026 once StatsBomb publishes it)
    _known = {43: {106, 107}, 55: {43, 282}, 223: {282}}
    for cid, sids in competitions.items():
        new = set(sids) - _known.get(cid, set())
        if new:
            name = _TOURNAMENT_NAMES.get(cid, str(cid))
            print(f"  Neue {name} Daten bei StatsBomb: Season-IDs {new} — werden geladen!")

    all_rows: list[dict] = []

    for comp_id, season_ids in competitions.items():
        for season_id in season_ids:
            print(f"  competition={comp_id}, season={season_id}...")
            rows = _fetch_match_xg(comp_id, season_id)
            all_rows.extend(rows)
            print(f"    {len(rows)} matches fetched.")

    if not all_rows:
        return pd.DataFrame(columns=["home_team", "away_team", "date", "home_xg", "away_xg", "tournament"])

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(_CACHE_PATH)
    print(f"  StatsBomb xG: {len(df)} matches cached.")
    return df


def get_team_xg_stats(
    team: str,
    before_date: pd.Timestamp,
    xg_df: pd.DataFrame,
    n_games: int = 5,
) -> dict[str, float]:
    """
    Returns rolling xG stats for team in the n_games before before_date.
    Keys: xg_avg, xga_avg, xg_diff, xg_conversion (goals/xg ratio placeholder).
    Returns zeros if no data available.
    """
    if xg_df.empty:
        return {"xg_avg": 0.0, "xga_avg": 0.0, "xg_diff": 0.0}

    mask = (
        ((xg_df["home_team"] == team) | (xg_df["away_team"] == team))
        & (xg_df["date"] < before_date)
    )
    recent = xg_df[mask].sort_values("date", ascending=False).head(n_games)

    if recent.empty:
        return {"xg_avg": 0.0, "xga_avg": 0.0, "xg_diff": 0.0}

    xg_list, xga_list = [], []
    for _, row in recent.iterrows():
        if row["home_team"] == team:
            xg_list.append(row["home_xg"])
            xga_list.append(row["away_xg"])
        else:
            xg_list.append(row["away_xg"])
            xga_list.append(row["home_xg"])

    xg_avg = float(sum(xg_list) / len(xg_list))
    xga_avg = float(sum(xga_list) / len(xga_list))
    return {
        "xg_avg": xg_avg,
        "xga_avg": xga_avg,
        "xg_diff": xg_avg - xga_avg,
    }

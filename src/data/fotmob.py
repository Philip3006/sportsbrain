"""
Fotmob match data scraper — per-player ratings + goalscorers.

Uses the Next.js embedded JSON from fotmob.com (no API key needed).
Rate-limit: 2s between requests. Caches finished matches permanently.

Data available per match:
  - Team average rating (1-10)
  - Per-player rating (1-10) for starters + subs
  - Goalscorers with player ID + minute
  - Player market value (EUR)
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests

from scripts._http_retry import retry_request
from src.config import DATA_CACHE

_CACHE_DIR = DATA_CACHE / "fotmob"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# WC group-stage league IDs — used to filter XML feed
_WC_LEAGUE_PREFIX = "World Cup"
_RATE_LIMIT_S = 2.0


def _cache_path(match_id: int) -> Path:
    return _CACHE_DIR / f"match_{match_id}.json"


def _load_cached(match_id: int) -> dict | None:
    p = _cache_path(match_id)
    if p.exists():
        return json.loads(p.read_text())
    return None


def _save_cache(match_id: int, data: dict) -> None:
    _cache_path(match_id).write_text(json.dumps(data))


def fetch_match_ids_for_date(date_str: str) -> list[int]:
    """
    Returns Fotmob match IDs for international WC/tournament matches on date_str (YYYYMMDD).
    Uses the Fotmob XML feed — no auth required.
    """
    url = f"https://api.fotmob.com/matches?date={date_str}"
    try:
        r = retry_request("GET",url, headers=_HEADERS, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except Exception:
        return []

    match_ids = []
    for league in root.findall(".//league"):
        pl_name = league.get("plName", "") or league.get("name", "")
        if _WC_LEAGUE_PREFIX in pl_name or "FIFA" in pl_name:
            for match in league.findall("match"):
                try:
                    match_ids.append(int(match.get("id", 0)))
                except ValueError:
                    pass
    return match_ids


def fetch_match_data(match_id: int, force: bool = False) -> dict | None:
    """
    Scrapes Fotmob match page and returns structured dict:
    {
        match_id, home_team, away_team, home_score, away_score, finished,
        home_rating, away_rating,
        home_players: [{name, player_id, position, rating, market_value}],
        away_players: [...],
        goalscorers: [{player_id, name, minute, team, own_goal}],
    }
    Returns None if match not finished or on scrape error.
    """
    if not force:
        cached = _load_cached(match_id)
        if cached is not None:
            return cached

    time.sleep(_RATE_LIMIT_S)
    try:
        r = retry_request("GET",
            f"https://www.fotmob.com/match/{match_id}",
            headers=_HEADERS,
            timeout=15,
            allow_redirects=True,
        )
        r.raise_for_status()
    except Exception:
        return None

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        r.text,
        re.DOTALL,
    )
    if not match:
        return None

    try:
        page = json.loads(match.group(1))
        props = page["props"]["pageProps"]
        header = props.get("header", {})
        content = props.get("content", {})
        lineup = content.get("lineup", {})
        match_facts = content.get("matchFacts", {})
    except (KeyError, json.JSONDecodeError):
        return None

    # Check match is finished
    status = header.get("status", {})
    finished = bool(status.get("finished") or status.get("fullTimeShown"))
    if not finished:
        return None

    teams = header.get("teams", [{}, {}])
    home_team_name = teams[0].get("name", "") if teams else ""
    away_team_name = teams[1].get("name", "") if len(teams) > 1 else ""
    home_score = teams[0].get("score", 0) if teams else 0
    away_score = teams[1].get("score", 0) if len(teams) > 1 else 0

    def _parse_players(team_data: dict) -> list[dict]:
        players = []
        for group in ("starters", "subs"):
            for p in team_data.get(group, []):
                perf = p.get("performance", {})
                rating = perf.get("rating")
                if rating is None:
                    continue
                players.append({
                    "player_id": p.get("id"),
                    "name": p.get("name", ""),
                    "position": p.get("positionShort", p.get("positionId", "")),
                    "rating": float(rating),
                    "market_value": p.get("marketValue", 0) or 0,
                    "starter": group == "starters",
                })
        return players

    home_lineup = lineup.get("homeTeam", {})
    away_lineup = lineup.get("awayTeam", {})

    # Parse goalscorers from events
    events = match_facts.get("events", {})
    event_list = events.get("events", []) if isinstance(events, dict) else []
    goalscorers = []
    for ev in event_list:
        if ev.get("type") == "Goal":
            pl = ev.get("player", {}) or {}
            goalscorers.append({
                "player_id": pl.get("id"),
                "name": pl.get("name", ""),
                "minute": ev.get("timeStr") or ev.get("time"),
                "team": home_team_name if ev.get("isHome") else away_team_name,
                "own_goal": bool(ev.get("ownGoal")),
            })

    result = {
        "match_id": match_id,
        "home_team": home_team_name,
        "away_team": away_team_name,
        "home_score": home_score,
        "away_score": away_score,
        "finished": True,
        "home_rating": home_lineup.get("rating"),
        "away_rating": away_lineup.get("rating"),
        "home_players": _parse_players(home_lineup),
        "away_players": _parse_players(away_lineup),
        "goalscorers": goalscorers,
    }

    _save_cache(match_id, result)
    return result


def fetch_tournament_ratings(
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """
    Scrapes all WC tournament matches between start_date and end_date (YYYY-MM-DD).
    Returns DataFrame with one row per (match, team, player):
      date | home_team | away_team | team | player | player_id | rating | market_value | starter
    Also includes team-level rows with player='__team__'.
    """
    rows = []
    dates = pd.date_range(start=start_date, end=end_date, freq="D")

    for dt in dates:
        date_str = dt.strftime("%Y%m%d")
        match_ids = fetch_match_ids_for_date(date_str)
        for mid in match_ids:
            data = fetch_match_data(mid)
            if data is None:
                continue
            base = {
                "date": dt.normalize(),
                "home_team": data["home_team"],
                "away_team": data["away_team"],
            }
            for side, key, players in (
                ("home", "home_players", data["home_players"]),
                ("away", "away_players", data["away_players"]),
            ):
                team_name = data[f"{side}_team"]
                team_rating = data.get(f"{side}_rating")
                if team_rating is not None:
                    rows.append({
                        **base,
                        "team": team_name,
                        "player": "__team__",
                        "player_id": None,
                        "rating": float(team_rating),
                        "market_value": 0,
                        "starter": True,
                    })
                for p in players:
                    rows.append({
                        **base,
                        "team": team_name,
                        **p,
                    })

    return pd.DataFrame(rows)


def get_team_rolling_rating(
    team: str,
    before_date: pd.Timestamp,
    ratings_df: pd.DataFrame,
    n_games: int = 5,
    starters_only: bool = True,
    decay: float = 0.85,
) -> dict[str, float]:
    """
    Returns exponentially decayed team + top-player ratings over last n_games.
    Falls back to {team_rating: 0.0, top_player_rating: 0.0} when no data.
    """
    _zero = {"fotmob_team_rating": 0.0, "fotmob_top_player_rating": 0.0}

    if ratings_df is None or ratings_df.empty:
        return _zero

    team_rows = ratings_df[
        (ratings_df["team"] == team)
        & (ratings_df["player"] == "__team__")
        & (ratings_df["date"] < before_date)
    ].sort_values("date", ascending=False).head(n_games)

    if team_rows.empty:
        return _zero

    weights = [decay ** i for i in range(len(team_rows))]
    team_rating = sum(w * r for w, r in zip(weights, team_rows["rating"])) / sum(weights)

    # Best player rating per match (starter with highest rating)
    player_rows = ratings_df[
        (ratings_df["team"] == team)
        & (ratings_df["player"] != "__team__")
        & (ratings_df["date"] < before_date)
        & (ratings_df["date"].isin(team_rows["date"]))
    ]
    if starters_only:
        player_rows = player_rows[player_rows["starter"]]

    if player_rows.empty:
        return {"fotmob_team_rating": float(team_rating), "fotmob_top_player_rating": 0.0}

    top_per_match = (
        player_rows.sort_values("rating", ascending=False)
        .groupby("date")["rating"]
        .first()
        .sort_index(ascending=False)
        .head(n_games)
    )
    top_weights = [decay ** i for i in range(len(top_per_match))]
    top_rating = sum(w * r for w, r in zip(top_weights, top_per_match)) / sum(top_weights)

    return {
        "fotmob_team_rating": float(team_rating),
        "fotmob_top_player_rating": float(top_rating),
    }

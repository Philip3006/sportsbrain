"""
StatsBomb Open-Data: Scorer-Outcomes pro Spieler × Match.

Erweiterung zu `statsbomb.py` (xG) und `statsbomb_ppda.py`. Für den
Goalscorer-Backtest brauchen wir die *Realität*: hat ein Spieler in dem
Match getroffen oder nicht. Aus den StatsBomb-Events ist das via
`event.shot.outcome.name == "Goal"` ableitbar.

Eigener Cache `statsbomb_scorers.pkl` mit Spalten:
  date, tournament, home_team, away_team, team, player, goals

Cache lebt 24h. Bei Bedarf reuse-bar in src/betting/goalscorer.py oder
Auswertungs-Scripts.
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from scripts._http_retry import retry_request
from src.config import DATA_CACHE, canonical_name as _cn

_CACHE_PATH = DATA_CACHE / "statsbomb_scorers.pkl"
_CACHE_MAX_AGE_H = 24

_SB_BASE = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
_SB_COMPETITION_IDS = {43, 55, 223}  # WC, Euro, Copa
_TOURNAMENT_NAMES = {43: "FIFA World Cup", 55: "UEFA Euro", 223: "Copa América"}


def _cache_fresh() -> bool:
    if not _CACHE_PATH.exists():
        return False
    return (time.time() - _CACHE_PATH.stat().st_mtime) / 3600 < _CACHE_MAX_AGE_H


def _discover() -> dict[int, list[int]]:
    url = f"{_SB_BASE}/competitions.json"
    try:
        r = retry_request("GET", url, timeout=15)
        r.raise_for_status()
        comps = r.json()
    except Exception:
        return {43: [106, 107], 55: [43, 282], 223: [282]}
    out: dict[int, list[int]] = {}
    for c in comps:
        cid, sid = c.get("competition_id"), c.get("season_id")
        if cid in _SB_COMPETITION_IDS and sid is not None:
            out.setdefault(cid, []).append(sid)
    return out


def _fetch_match_scorers(comp_id: int, season_id: int) -> list[dict]:
    matches_url = f"{_SB_BASE}/matches/{comp_id}/{season_id}.json"
    try:
        r = retry_request("GET", matches_url, timeout=15)
        r.raise_for_status()
        matches = r.json()
    except Exception:
        return []

    tournament = _TOURNAMENT_NAMES.get(comp_id, "")
    rows: list[dict] = []
    for m in matches:
        mid = m.get("match_id")
        if mid is None:
            continue
        home = (m.get("home_team") or {}).get("home_team_name") or ""
        away = (m.get("away_team") or {}).get("away_team_name") or ""
        date_str = m.get("match_date", "")
        try:
            date = pd.Timestamp(date_str)
        except Exception:
            continue

        time.sleep(0.5)
        ev_url = f"{_SB_BASE}/events/{mid}.json"
        try:
            er = retry_request("GET", ev_url, timeout=20)
            er.raise_for_status()
            events = er.json()
        except Exception:
            continue

        goals: dict[tuple[str, str], int] = {}
        for ev in events:
            if (ev.get("type") or {}).get("name") != "Shot":
                continue
            outcome = ((ev.get("shot") or {}).get("outcome") or {}).get("name", "")
            if outcome != "Goal":
                continue
            team_name = (ev.get("team") or {}).get("name") or ""
            player_name = (ev.get("player") or {}).get("name") or ""
            key = (team_name, player_name)
            goals[key] = goals.get(key, 0) + 1

        for (team_name, player_name), g in goals.items():
            canon_team = _cn(home) if team_name == home else _cn(away)
            rows.append({
                "date": date,
                "tournament": tournament,
                "home_team": _cn(home),
                "away_team": _cn(away),
                "team": canon_team,
                "player": player_name,
                "goals": g,
            })

    return rows


def fetch_statsbomb_scorers(force: bool = False) -> pd.DataFrame:
    """Returns Scorer-Outcomes DataFrame. Caches 24h."""
    if not force and _cache_fresh():
        return pd.read_pickle(_CACHE_PATH)

    print("Fetching StatsBomb Scorer-Outcomes (this may take 1-3 minutes)...")
    comps = _discover()
    all_rows: list[dict] = []
    for cid, sids in comps.items():
        for sid in sids:
            print(f"  competition={cid}, season={sid}...")
            rows = _fetch_match_scorers(cid, sid)
            all_rows.extend(rows)
            print(f"    {len(rows)} scorer-rows fetched.")

    if not all_rows:
        return pd.DataFrame(columns=["date", "tournament", "home_team", "away_team",
                                     "team", "player", "goals"])

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(_CACHE_PATH)
    print(f"  StatsBomb scorers: {len(df)} player-match rows cached.")
    return df

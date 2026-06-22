"""
Sofascore live xG fetcher for WM 2026.

Eliminates the StatsBomb 1-2 day publication lag during the running tournament:
Sofascore publishes per-match xG within minutes of full-time.

Endpoints (RapidAPI sofascore.p.rapidapi.com):
  GET tournaments/get-matches?tournamentId=16&seasonId=58210
      → list of WC 2026 events with status (Ended / In progress / Not started)
  GET matches/get-statistics?matchId=<event_id>
      → Expected goals + many other stats per period

Needs API_FOOTBALL_KEY in .env (same RapidAPI key works for both api-football
and sofascore — different host header).
"""
from __future__ import annotations

import os
import time
from pathlib import Path  # noqa: F401  (used in _team_value_cache_path)

import pandas as pd
import requests

from scripts._http_retry import retry_request
from src.config import DATA_CACHE, canonical_name as _cn

_CACHE_PATH = DATA_CACHE / "sofascore_xg.pkl"
_CACHE_MAX_AGE_H = 3.0  # WM phase: refresh every 3h to pick up newly-finished matches
_CACHE_STALE_MAX_H = 48.0  # graceful-degradation ceiling — if API is down/rate-limited
                            # we still return stale cache up to 48h old before giving up.
                            # Beyond that the live-xG feature is disabled to avoid stale
                            # form signals masking real changes (Lever 2 collapse).

_TEAM_IDS_CACHE = DATA_CACHE / "sofascore_team_ids.json"
_PLAYER_VALUES_CACHE = DATA_CACHE / "sofascore_player_values"
_PLAYER_VALUES_MAX_AGE_DAYS = 30.0  # player MVs change slowly
_quota_exhausted = False  # circuit breaker — set on first 429, clears on process restart

_HOST = "sofascore.p.rapidapi.com"
_BASE = f"https://{_HOST}"

# Sofascore unique-tournament IDs (verified for FIFA WC = 16)
WC2026_TOURNAMENT_ID = 16
WC2026_SEASON_ID = 58210


def _get_api_key() -> str | None:
    return os.getenv("API_FOOTBALL_KEY")


def _headers() -> dict[str, str]:
    key = _get_api_key()
    if not key:
        raise RuntimeError(
            "API_FOOTBALL_KEY not set in environment — needed for Sofascore RapidAPI access"
        )
    return {"X-RapidAPI-Key": key, "X-RapidAPI-Host": _HOST}


def _cache_age_h() -> float | None:
    """Returns cache age in hours, or None if cache does not exist."""
    if not _CACHE_PATH.exists():
        return None
    return (time.time() - _CACHE_PATH.stat().st_mtime) / 3600


def _cache_is_fresh() -> bool:
    age = _cache_age_h()
    return age is not None and age < _CACHE_MAX_AGE_H


def _load_cache_or_empty() -> pd.DataFrame:
    """Returns cached xG DataFrame, or empty if no cache exists. Never raises."""
    if not _CACHE_PATH.exists():
        return pd.DataFrame(columns=["home_team", "away_team", "date",
                                      "home_xg", "away_xg", "tournament"])
    try:
        return pd.read_pickle(_CACHE_PATH)
    except Exception as e:
        print(f"  [sofascore] cache load failed ({e}) — returning empty")
        return pd.DataFrame(columns=["home_team", "away_team", "date",
                                      "home_xg", "away_xg", "tournament"])


def _persist_team_ids(name_to_id: dict[str, int]) -> None:
    import json as _json
    try:
        existing = _json.loads(_TEAM_IDS_CACHE.read_text()) if _TEAM_IDS_CACHE.exists() else {}
    except Exception:
        existing = {}
    existing.update({k: int(v) for k, v in name_to_id.items()})
    _TEAM_IDS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _TEAM_IDS_CACHE.write_text(_json.dumps(existing, ensure_ascii=False, indent=2))


def bootstrap_team_ids_from_standings() -> dict[str, int]:
    """One-shot: fetch tournaments/get-standings and persist all WC team IDs.
    Use this when the per-event population (last/next 2 matches only) is
    insufficient — standings exposes all 48 groups even pre-tournament.
    """
    if not _get_api_key():
        return {}
    try:
        r = retry_request("GET",
            f"{_BASE}/tournaments/get-standings",
            headers=_headers(),
            params={"tournamentId": WC2026_TOURNAMENT_ID, "seasonId": WC2026_SEASON_ID},
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"  [sofascore] standings fetch failed: {e}")
        return {}
    j = r.json()
    name_to_id: dict[str, int] = {}
    for st in (j.get("standings") or []):
        for row in (st.get("rows") or []):
            team = row.get("team", {})
            tid = team.get("id")
            name = team.get("name")
            if tid and name:
                name_to_id[_cn(name)] = int(tid)
    if name_to_id:
        _persist_team_ids(name_to_id)
        print(f"  [sofascore] bootstrapped {len(name_to_id)} WC team IDs from standings")
    return name_to_id


def load_team_ids() -> dict[str, int]:
    """Returns canonicalized {team_name: sofascore_team_id} from cache."""
    import json as _json
    if not _TEAM_IDS_CACHE.exists():
        return {}
    try:
        return {k: int(v) for k, v in _json.loads(_TEAM_IDS_CACHE.read_text()).items()}
    except Exception:
        return {}


def fetch_wc2026_event_ids() -> list[dict]:
    """Returns list of {event_id, date, home, away, status, home_score, away_score}.
    Only returns matches with a numeric event_id (skips draft/postponed entries).
    """
    out: list[dict] = []
    seen: set[int] = set()
    # Sofascore wrapper paginates last/next; we iterate both up to a safe ceiling
    for course in ("last", "next"):
        for page in range(0, 8):
            try:
                r = retry_request("GET",
                    f"{_BASE}/tournaments/get-matches",
                    headers=_headers(),
                    params={
                        "tournamentId": WC2026_TOURNAMENT_ID,
                        "seasonId": WC2026_SEASON_ID,
                        "course": course,
                        "page": page,
                    },
                    timeout=15,
                )
            except Exception as e:
                print(f"  [sofascore] tournament fetch failed (course={course} page={page}): {e}")
                break
            if r.status_code != 200 or not r.text:
                break
            j = r.json()
            events = j.get("events") or []
            if not events:
                break
            new_in_page = 0
            team_ids: dict[str, int] = {}
            for ev in events:
                eid = ev.get("id")
                if eid is None or eid in seen:
                    continue
                seen.add(eid)
                new_in_page += 1
                ts = ev.get("startTimestamp") or 0
                date = pd.Timestamp(ts, unit="s") if ts else pd.NaT
                home_team = _cn(ev.get("homeTeam", {}).get("name", ""))
                away_team = _cn(ev.get("awayTeam", {}).get("name", ""))
                hid = ev.get("homeTeam", {}).get("id")
                aid = ev.get("awayTeam", {}).get("id")
                if home_team and hid:
                    team_ids[home_team] = int(hid)
                if away_team and aid:
                    team_ids[away_team] = int(aid)
                out.append({
                    "event_id": int(eid),
                    "date": date,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_score": ev.get("homeScore", {}).get("current"),
                    "away_score": ev.get("awayScore", {}).get("current"),
                    "status": ev.get("status", {}).get("type", ""),
                    "status_desc": ev.get("status", {}).get("description", ""),
                })
            if team_ids:
                _persist_team_ids(team_ids)
            if new_in_page == 0:
                break
            time.sleep(0.3)
    return out


def fetch_match_xg(event_id: int) -> tuple[float | None, float | None]:
    """Returns (home_xg, away_xg) from matches/get-statistics for a finished event.
    Sofascore returns multiple periods; we take 'ALL' period's Expected goals row.

    Raises RuntimeError("rate-limited" / "quota") on HTTP 429 so the caller
    can fall back to cache instead of silently returning (None, None) per call.
    """
    try:
        r = retry_request("GET",
            f"{_BASE}/matches/get-statistics",
            headers=_headers(),
            params={"matchId": event_id},
            timeout=15,
        )
    except Exception as e:
        print(f"  [sofascore] stats fetch failed for {event_id}: {e}")
        return None, None
    if r.status_code == 429:
        raise RuntimeError(f"sofascore rate-limited (429) on event {event_id}")
    if r.status_code in (402, 403):
        raise RuntimeError(f"sofascore quota exhausted ({r.status_code}) on event {event_id}")
    if r.status_code != 200 or not r.text:
        return None, None
    j = r.json()
    # Structure: statistics → [{period: "ALL"/"1ST"/"2ND", groups: [{statisticsItems: [...]}]}]
    matches_xg: list[tuple[float, float]] = []
    for period in (j.get("statistics") or []):
        if period.get("period") != "ALL":
            continue
        for group in period.get("groups", []):
            for stat in group.get("statisticsItems", []):
                key = (stat.get("key") or "").lower()
                name = (stat.get("name") or "").lower().strip()
                if key == "expected_goals" or name == "expected goals":
                    try:
                        matches_xg.append((float(stat.get("home")), float(stat.get("away"))))
                    except (TypeError, ValueError):
                        pass
    if not matches_xg:
        return None, None
    if len(matches_xg) > 1:
        print(f"  [sofascore] WARN: {len(matches_xg)} expected_goals rows for event {event_id} — using first")
    return matches_xg[0]


def _team_value_cache_path(team_id: int) -> Path:
    _PLAYER_VALUES_CACHE.mkdir(parents=True, exist_ok=True)
    return _PLAYER_VALUES_CACHE / f"team_{team_id}.json"


def _value_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return age_days < _PLAYER_VALUES_MAX_AGE_DAYS


def fetch_team_player_values(team_id: int, force: bool = False) -> dict[str, float]:
    """Returns {player_name: market_value_eur_m} for a Sofascore team.

    Uses teams/get-squad to enumerate, then players/detail for each player's
    proposedMarketValue. Result cached 30 days (market values change slowly).
    Skips network entirely when cache is fresh; one batch is ~27 API calls.
    Circuit breaker: skips all API calls once a 429 is seen in this process run.
    """
    global _quota_exhausted
    import json as _json
    path = _team_value_cache_path(team_id)
    if not force and _value_cache_fresh(path):
        try:
            return {k: float(v) for k, v in _json.loads(path.read_text()).items()}
        except Exception:
            pass

    # Return stale cache if quota is exhausted or API key missing
    if _quota_exhausted or not _get_api_key():
        if path.exists():
            try:
                return {k: float(v) for k, v in _json.loads(path.read_text()).items()}
            except Exception:
                pass
        return {}

    try:
        r = retry_request("GET",
            f"{_BASE}/teams/get-squad",
            headers=_headers(),
            params={"teamId": team_id},
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        if "429" in str(e):
            _quota_exhausted = True
            print(f"  [sofascore] 429 quota exhausted — skipping further squad fetches this run")
        else:
            print(f"  [sofascore] team {team_id} squad fetch failed: {e}")
        if path.exists():
            try:
                return {k: float(v) for k, v in _json.loads(path.read_text()).items()}
            except Exception:
                pass
        return {}

    players = (r.json() or {}).get("players", []) or []
    result: dict[str, float] = {}
    for entry in players:
        p = entry.get("player", {})
        pid = p.get("id")
        name = p.get("name")
        if not pid or not name:
            continue
        try:
            r2 = retry_request("GET",
                f"{_BASE}/players/detail",
                headers=_headers(),
                params={"playerId": pid},
                timeout=15,
            )
            r2.raise_for_status()
            pj = (r2.json() or {}).get("player", {})
            raw = pj.get("proposedMarketValueRaw") or {}
            value_eur = raw.get("value") or pj.get("proposedMarketValue") or 0
            if value_eur:
                result[name] = float(value_eur) / 1_000_000.0
        except Exception:
            pass
        time.sleep(0.35)

    if result:
        path.write_text(_json.dumps(result, ensure_ascii=False))
    return result


def overlay_player_values(team_name: str, players: list, force: bool = False) -> int:
    """Overlays sofascore player market values onto a list of PlayerStatus by name.
    Returns number of players matched. Uses simple last-name fallback when full-name
    mismatches (handles 'R. Jimenez' vs 'Raul Jimenez' style cases).
    """
    ids = load_team_ids()
    team_id = ids.get(team_name)
    if not team_id:
        # First try: enumerate played fixtures
        try:
            fetch_wc2026_event_ids()
            ids = load_team_ids()
            team_id = ids.get(team_name)
        except Exception:
            pass
    if not team_id:
        # Second try: bootstrap from standings (all 48 teams)
        try:
            bootstrap_team_ids_from_standings()
            ids = load_team_ids()
            team_id = ids.get(team_name)
        except Exception:
            pass
    if not team_id:
        return 0

    values = fetch_team_player_values(team_id, force=force)
    if not values:
        return 0

    # Multi-strategy matching to handle name variations across sources
    # (covers.com gives "Rodrygo", Sofascore lists "Rodrygo Goes", etc.).
    by_lower = {n.lower(): v for n, v in values.items()}
    last_name_map: dict[str, list[tuple[str, float]]] = {}
    first_name_map: dict[str, list[tuple[str, float]]] = {}
    for n, v in values.items():
        toks = n.split()
        if not toks:
            continue
        last_name_map.setdefault(toks[-1].lower(), []).append((n, v))
        first_name_map.setdefault(toks[0].lower(), []).append((n, v))

    matched = 0
    for p in players:
        if p.name.startswith("fit_"):
            continue  # placeholder, skip
        full_lower = p.name.lower()
        # 1. Exact
        if full_lower in by_lower:
            p.market_value_eur_m = by_lower[full_lower]
            matched += 1
            continue
        tokens = p.name.split()
        # 2. Last name unique
        if len(tokens) >= 2:
            cands = last_name_map.get(tokens[-1].lower(), [])
            if len(cands) == 1:
                p.market_value_eur_m = cands[0][1]
                matched += 1
                continue
        # 3. First name unique (mononyms like "Rodrygo")
        cands = first_name_map.get(tokens[0].lower(), [])
        if len(cands) == 1:
            p.market_value_eur_m = cands[0][1]
            matched += 1
            continue
        # 4. Substring (any Sofascore name CONTAINS player name)
        if len(p.name) >= 4:
            contains = [(n, v) for n, v in values.items() if full_lower in n.lower()]
            if len(contains) == 1:
                p.market_value_eur_m = contains[0][1]
                matched += 1
    return matched


def fetch_wc2026_xg(force: bool = False) -> pd.DataFrame:
    """
    Returns DataFrame with columns: home_team, away_team, date, home_xg, away_xg, tournament.
    Same schema as fetch_statsbomb_xg() so it can be concatenated.

    Phase 1.3 resilience:
      1. Fresh cache (< _CACHE_MAX_AGE_H)  → return immediately, no network.
      2. Force refresh OR stale cache (3h-48h) → try live fetch; on rate-limit
         or 429, fall back to whatever cache we have and WARN loudly.
      3. Cache older than _CACHE_STALE_MAX_H or missing → live fetch only.
      4. Live fetch fails completely → empty DataFrame (xG features disabled
         downstream rather than serving stale data).

    The audit (sofascore_rate_limit_backlog.md) flagged that Sofascore's RapidAPI
    quota silently zeros out, causing the LightGBM scanner-path to lose all xG
    features. This makes the fallback explicit so the scanner can keep running
    on day-old xG instead of going blind.
    """
    age = _cache_age_h()
    if not force and age is not None and age < _CACHE_MAX_AGE_H:
        return _load_cache_or_empty()

    if not _get_api_key():
        print("  [sofascore] API_FOOTBALL_KEY missing — returning empty xG")
        return _load_cache_or_empty() if age is not None and age < _CACHE_STALE_MAX_H else pd.DataFrame(
            columns=["home_team", "away_team", "date", "home_xg", "away_xg", "tournament"]
        )

    print("  [sofascore] fetching WC 2026 fixtures...")
    try:
        fixtures = fetch_wc2026_event_ids()
    except Exception as e:
        print(f"  [sofascore] ⚠️  fixture fetch failed ({e})")
        fixtures = []

    if not fixtures and age is not None and age < _CACHE_STALE_MAX_H:
        print(f"  [sofascore] ⚠️  using stale cache (age={age:.1f}h, ttl={_CACHE_STALE_MAX_H}h)")
        return _load_cache_or_empty()

    finished = [f for f in fixtures
                if f.get("status") == "finished"
                or "ended" in f.get("status_desc", "").lower()]
    print(f"  [sofascore] {len(fixtures)} total events, {len(finished)} finished — fetching xG for finished...")

    rows = []
    rate_limited = False
    for i, f in enumerate(finished, start=1):
        try:
            home_xg, away_xg = fetch_match_xg(f["event_id"])
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "rate" in msg or "quota" in msg:
                rate_limited = True
                print(f"  [sofascore] ⚠️  rate-limit hit at event {i}/{len(finished)} — aborting fetch loop")
                break
            home_xg, away_xg = None, None
        if home_xg is None:
            continue
        rows.append({
            "home_team": f["home_team"],
            "away_team": f["away_team"],
            "date": f["date"],
            "home_xg": home_xg,
            "away_xg": away_xg,
            "tournament": "FIFA World Cup",
        })
        time.sleep(0.4)  # rate-limit politely
        if i % 8 == 0:
            print(f"    {i}/{len(finished)} processed")

    df = pd.DataFrame(rows)
    if df.empty and age is not None and age < _CACHE_STALE_MAX_H:
        suffix = "rate-limited" if rate_limited else "no new data"
        print(f"  [sofascore] ⚠️  {suffix} — returning stale cache (age={age:.1f}h)")
        return _load_cache_or_empty()

    if not df.empty:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        df.to_pickle(_CACHE_PATH)
        print(f"  [sofascore] cached {len(df)} WC 2026 xG records → {_CACHE_PATH.name}")
    return df

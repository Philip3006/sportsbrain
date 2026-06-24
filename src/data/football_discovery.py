"""TheOddsAPI Football-Sports-Discovery (Roadmap I11).

Ruft https://api.the-odds-api.com/v4/sports?group=soccer auf und filtert gegen
FOOTBALL_LEAGUES_WHITELIST. Unbekannte Soccer-Keys werden geloggt (kein Crash).

Cache: 1h. Cache-File: data/cache/football_active_sports.json.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from scripts._http_retry import retry_request
from src.config import DATA_CACHE, FOOTBALL_LEAGUES_WHITELIST

_SPORTS_URL = "https://api.the-odds-api.com/v4/sports"
_CACHE_PATH = DATA_CACHE / "football_active_sports.json"
_CACHE_TTL_SEC = 3600  # 1h


def _load_cache() -> list[dict] | None:
    if not _CACHE_PATH.exists():
        return None
    if time.time() - _CACHE_PATH.stat().st_mtime > _CACHE_TTL_SEC:
        return None
    try:
        return json.loads(_CACHE_PATH.read_text())
    except Exception:
        return None


def _save_cache(payload: list[dict]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(payload))


def fetch_active_soccer_sports(
    api_key: str | None = None,
    *,
    use_cache: bool = True,
) -> list[dict]:
    """Holt alle aktiven soccer_*-Keys von TheOddsAPI /sports?group=soccer.

    Returns: List[{key, title, group, active, has_outrights}].
    """
    api_key = api_key or os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return []

    if use_cache:
        cached = _load_cache()
        if cached is not None:
            return cached

    try:
        resp = retry_request(
            "GET", _SPORTS_URL,
            params={"apiKey": api_key, "all": "false"},
            timeout=15,
            log_prefix="[football_discovery]",
        )
        resp.raise_for_status()
        all_sports = resp.json()
    except Exception as exc:
        print(f"[football_discovery] /sports fetch failed: {exc}")
        if _CACHE_PATH.exists():
            try:
                return json.loads(_CACHE_PATH.read_text())
            except Exception:
                pass
        return []

    soccer = [
        s for s in all_sports
        if s.get("key", "").startswith("soccer_") and s.get("active", False)
    ]
    _save_cache(soccer)
    return soccer


def discover_active_leagues(
    api_key: str | None = None,
    *,
    use_cache: bool = True,
) -> list[str]:
    """Gibt Liste aktiver, whitelisteter Soccer-Sport-Keys zurück.

    Unbekannte Keys (nicht in FOOTBALL_LEAGUES_WHITELIST) werden geloggt.
    Bei API-Ausfall: leere Liste — Caller fällt auf statische Konfiguration zurück.
    """
    active = fetch_active_soccer_sports(api_key=api_key, use_cache=use_cache)
    result: list[str] = []
    for s in active:
        key = s.get("key", "")
        if key in FOOTBALL_LEAGUES_WHITELIST:
            result.append(key)
        else:
            print(f"[football_discovery] unknown soccer key {key!r} — not in whitelist, skipping")
    return result

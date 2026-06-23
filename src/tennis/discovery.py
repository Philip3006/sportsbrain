"""TheOddsAPI Tennis-Sports-Discovery (Roadmap J2, Phase A).

Ruft https://api.the-odds-api.com/v4/sports auf und matched alle aktiven
tennis_*-Keys gegen die statische TENNIS_REGISTRY. Unbekannte Keys werden via
unknown_sport_key() mit konservativen Defaults gewrapt — kein Crash bei Drift.

Cache: 1h (TheOddsAPI /sports ist günstig, aber wir wollen quota-freundlich
bleiben). Cache-File: data/cache/tennis_active_sports.json.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

from scripts._http_retry import retry_request
from src.config import DATA_CACHE
from src.tennis.tournaments import (
    Tournament,
    all_sport_keys,
    get_tournament,
    unknown_sport_key,
)

_SPORTS_URL = "https://api.the-odds-api.com/v4/sports"
_CACHE_PATH = DATA_CACHE / "tennis_active_sports.json"
_CACHE_TTL_SEC = 3600  # 1h


def _load_cache() -> list[dict] | None:
    if not _CACHE_PATH.exists():
        return None
    age = time.time() - _CACHE_PATH.stat().st_mtime
    if age > _CACHE_TTL_SEC:
        return None
    try:
        return json.loads(_CACHE_PATH.read_text())
    except Exception:
        return None


def _save_cache(payload: list[dict]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(payload))


def fetch_active_tennis_sports(
    api_key: str | None = None,
    *,
    use_cache: bool = True,
) -> list[dict]:
    """Holt alle aktiven tennis_*-Keys von TheOddsAPI /sports.

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
            log_prefix="[tennis_discovery]",
        )
        resp.raise_for_status()
        all_sports = resp.json()
    except Exception as exc:
        print(f"[tennis_discovery] /sports fetch failed: {exc}")
        # Stale Cache als Notfall, auch wenn TTL abgelaufen
        if _CACHE_PATH.exists():
            try:
                return json.loads(_CACHE_PATH.read_text())
            except Exception:
                pass
        return []

    tennis = [s for s in all_sports if s.get("key", "").startswith("tennis_")
              and s.get("active", False)]
    _save_cache(tennis)
    return tennis


def discover_active_tournaments(
    today: date | None = None,
    *,
    api_key: str | None = None,
    use_cache: bool = True,
) -> list[Tournament]:
    """Liste der aktuell aktiven Tour-Events.

    Quelle 1 (primär): TheOddsAPI /sports — `active=true` heißt "Markt offen".
    Quelle 2 (fallback): typical_months-Heuristik aus Registry, falls API fehlt.

    Returns: List[Tournament] (Registry-Einträge ODER unknown_sport_key-Wraps).
    """
    today = today or date.today()
    active_sports = fetch_active_tennis_sports(api_key=api_key, use_cache=use_cache)

    if active_sports:
        out: list[Tournament] = []
        seen_slugs: set[str] = set()
        known = all_sport_keys()
        for s in active_sports:
            key = s.get("key", "")
            if key in known:
                t = get_tournament(key)
                if t and t.slug not in seen_slugs:
                    out.append(t)
                    seen_slugs.add(t.slug)
            else:
                # Unbekannter tennis_*-Key → konservative Defaults
                wrap = unknown_sport_key(key)
                if wrap.slug not in seen_slugs:
                    out.append(wrap)
                    seen_slugs.add(wrap.slug)
                print(f"[tennis_discovery] unknown sport_key {key!r} — wrapped as {wrap.category}")
        return out

    # Fallback: Monats-Heuristik (kein API-Zugang verfügbar)
    from src.tennis.tournaments import tournaments_for_month
    return tournaments_for_month(today.month)

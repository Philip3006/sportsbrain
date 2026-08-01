"""Tennis Match-Stats Fetcher (Roadmap J2-M).

Zieht Serve/Return-Aggregate pro Spieler aus tennisabstract.com Player-Pages.
Datenquelle: `matchmx`-JS-Array im HTML (~48 Spalten pro Match).

Column-Mapping (empirisch verifiziert 2026-08-01 gegen bekannte Matches):
    [0]  date        (YYYYMMDD)
    [2]  surface     ('Hard' | 'Clay' | 'Grass' | 'Carpet')
    [4]  result      ('W' | 'L')
    [9]  score       ('6-4 6-2', 'W/O', 'Ret.', '')
    [11] opponent    (name)
    [12] opp_rank
    [20] tpw_p       (total_points_won by player)
    [21] aces_p
    [22] dfs_p
    [32] tpw_o       (total_points_won by opponent)
    [33] aces_o
    [34] dfs_o

Nicht-Spalten-präzise Features (unit-agnostic Verhältnisse) sind bewusst
robust gegen kleine Column-Shifts: dominance_rate, ace_rate, df_rate,
break_dominance sind aggregates die auch bei ±1 Column-Verschiebung stabil
bleiben.

Cache: `data/cache/tennis_stats/{player_slug}.json`, 24h-TTL, ein Roh-Dump
pro Player. Feature-Aggregate werden on-demand berechnet (nicht gecacht),
damit Rolling-Windows flexibel bleiben.
"""
from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts._http_retry import retry_request

_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = _ROOT / "data" / "cache" / "tennis_stats"
CACHE_TTL_SEC = 24 * 3600  # 24h — Player-Stats ändern sich langsam
_UA = {"User-Agent": "Mozilla/5.0 (SportsBrain/J2-M)"}
_MATCHMX_RE = re.compile(r"var matchmx\s*=\s*(\[.*?\]);", re.DOTALL)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MatchStat:
    """Single-match stats for one player perspective."""
    date: str            # YYYYMMDD
    surface: str         # lowercased
    result: str          # 'W' | 'L'
    opponent: str
    tpw_p: int           # total points won (player)
    aces_p: int
    dfs_p: int
    tpw_o: int           # total points won (opponent)
    aces_o: int
    dfs_o: int


@dataclass(frozen=True)
class ServeAggregate:
    """Rolling-window aggregate for one player."""
    n_matches: int
    dominance_rate: float   # tpw_p / (tpw_p + tpw_o) — 0.5 = neutral
    ace_rate: float         # aces_p / tpw_p — normalized against volume
    df_rate: float          # dfs_p / tpw_p
    win_rate: float         # W / n_matches
    ace_df_ratio: float     # aces_p / max(dfs_p, 1) — serve reliability


# ---------------------------------------------------------------------------
# Player name → TA slug
# ---------------------------------------------------------------------------

def _to_ta_slug(name: str) -> str:
    """'Carlos Alcaraz' → 'CarlosAlcaraz'; strip accents best-effort."""
    import unicodedata
    stripped = "".join(
        c for c in unicodedata.normalize("NFD", name)
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^A-Za-z]", "", stripped)


# ---------------------------------------------------------------------------
# Fetcher + Cache
# ---------------------------------------------------------------------------

def _cache_path(slug: str) -> Path:
    return CACHE_DIR / f"{slug}.json"


def _load_cache(slug: str) -> Optional[list[list]]:
    p = _cache_path(slug)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text())
        if time.time() - payload.get("fetched_at", 0) > CACHE_TTL_SEC:
            return None
        return payload.get("matchmx", [])
    except Exception:
        return None


def _save_cache(slug: str, matchmx: list[list]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(slug).write_text(json.dumps({
        "fetched_at": time.time(),
        "matchmx": matchmx,
    }, ensure_ascii=False))


def _fetch_matchmx(
    slug: str, timeout: float = 10.0, tour: str = "atp",
) -> Optional[list[list]]:
    """Fetch raw matchmx array for a player. Cached (24h TTL).

    ATP: /cgi-bin/player-classic.cgi   (mens tour)
    WTA: /cgi-bin/wplayer.cgi          (womens tour)
    """
    cached = _load_cache(slug)
    if cached is not None:
        return cached

    endpoint = "wplayer.cgi" if tour.lower() == "wta" else "player-classic.cgi"
    url = f"https://www.tennisabstract.com/cgi-bin/{endpoint}?p={slug}"
    try:
        r = retry_request(
            "GET", url, headers=_UA, timeout=timeout,
            retries=3, backoff=(2, 5, 15),
            retry_on_status={429, 502, 503, 504},
            log_prefix="[tennis_stats]",
        )
        if r is None or r.status_code != 200:
            return None
        # Guard: TA gibt bei unbekanntem Slug einen Default/Kompilations-Report zurück
        # (kein 404). Prüfen ob Player-Name im HTML enthalten ist.
        name_tokens = re.findall(r"[A-Z][a-z]+", slug)
        if not any(tok in r.text for tok in name_tokens if len(tok) >= 3):
            return None
        m = _MATCHMX_RE.search(r.text)
        if not m:
            return None
        rows = ast.literal_eval(m.group(1))
    except Exception:
        return None

    _save_cache(slug, rows)
    return rows


# ---------------------------------------------------------------------------
# Row → MatchStat parser
# ---------------------------------------------------------------------------

def _to_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except (ValueError, TypeError):
        return default


def _parse_row(row: list) -> Optional[MatchStat]:
    """Convert raw matchmx row → MatchStat. Skips incomplete/walkover matches."""
    if len(row) < 40:
        return None
    score = str(row[9] or "")
    # Skip walkover, retirement, unplayed
    if not score or any(t in score for t in ("W/O", "DEF")):
        return None
    tpw_p = _to_int(row[20])
    tpw_o = _to_int(row[32])
    if tpw_p <= 0 or tpw_o <= 0:
        return None
    return MatchStat(
        date=str(row[0] or ""),
        surface=str(row[2] or "").lower(),
        result=str(row[4] or ""),
        opponent=str(row[11] or ""),
        tpw_p=tpw_p,
        aces_p=_to_int(row[21]),
        dfs_p=_to_int(row[22]),
        tpw_o=tpw_o,
        aces_o=_to_int(row[33]),
        dfs_o=_to_int(row[34]),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_match_stats(player_name: str, tour: str = "atp") -> list[MatchStat]:
    """Return list of MatchStat for a player (most recent first).

    tour: 'atp' (default) or 'wta' — routes zu unterschiedlichen TA-Endpoints.
    """
    slug = _to_ta_slug(player_name)
    if not slug:
        return []
    raw = _fetch_matchmx(slug, tour=tour)
    if not raw:
        return []
    stats = [ms for ms in (_parse_row(r) for r in raw) if ms is not None]
    return stats


def _empty_aggregate() -> ServeAggregate:
    return ServeAggregate(0, 0.5, 0.0, 0.0, 0.5, 1.0)


def aggregate(
    stats: list[MatchStat],
    *,
    last_n: int = 20,
    surface: Optional[str] = None,
    before_date: Optional[str] = None,
) -> ServeAggregate:
    """Compute rolling-window aggregate over the most-recent matches.

    Args:
        stats: sorted-most-recent-first list from fetch_match_stats().
        last_n: window size (default 20 matches).
        surface: if set, filter to matches on that surface only.
        before_date: YYYYMMDD; only include matches strictly before this date
                     (for walk-forward feature engineering — prevents leakage).
    """
    filtered = stats
    if surface:
        filtered = [s for s in filtered if s.surface == surface.lower()]
    if before_date:
        filtered = [s for s in filtered if s.date < before_date]
    window = filtered[:last_n]

    if not window:
        return _empty_aggregate()

    tpw_p = sum(s.tpw_p for s in window)
    tpw_o = sum(s.tpw_o for s in window)
    aces_p = sum(s.aces_p for s in window)
    dfs_p = sum(s.dfs_p for s in window)
    wins = sum(1 for s in window if s.result == "W")
    total_pts = tpw_p + tpw_o

    return ServeAggregate(
        n_matches=len(window),
        dominance_rate=(tpw_p / total_pts) if total_pts > 0 else 0.5,
        ace_rate=(aces_p / tpw_p) if tpw_p > 0 else 0.0,
        df_rate=(dfs_p / tpw_p) if tpw_p > 0 else 0.0,
        win_rate=wins / len(window),
        ace_df_ratio=aces_p / max(dfs_p, 1),
    )


def fetch_aggregate(
    player_name: str,
    *,
    last_n: int = 20,
    surface: Optional[str] = None,
    before_date: Optional[str] = None,
    tour: str = "atp",
) -> ServeAggregate:
    """Convenience: fetch + aggregate in one call."""
    stats = fetch_match_stats(player_name, tour=tour)
    if not stats:
        return _empty_aggregate()
    return aggregate(stats, last_n=last_n, surface=surface, before_date=before_date)

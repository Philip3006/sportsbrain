"""Tennis Match-Stats Fetcher (Roadmap J2-M).

Zieht Serve/Return-Aggregate pro Spieler aus tennisabstract.com Player-Pages.
Datenquelle: `matchmx`-JS-Array im HTML (~48 Spalten pro Match).

Column-Mapping (authoritativ verifiziert 2026-08-03 gegen TA-eigenes
`matchhead`-Array im ausgelieferten JS):

    [ 0] date        (YYYYMMDD)          [20] time        (Minuten!)
    [ 1] tourn                            [21] aces
    [ 2] surf                             [22] dfs
    [ 3] level                            [23] pts         (serve pts served)
    [ 4] wl          ('W'|'L')            [24] firsts      (1st serves in)
    [ 5] rank                             [25] fwon        (1st serves won)
    [ 6] seed                             [26] swon        (2nd serves won)
    [ 7] entry                            [27] games       (service games)
    [ 8] round                            [28] saved       (BP saved)
    [ 9] score                            [29] chances     (BP faced)
    [10] max         (BO3|BO5)            [30] oaces
    [11] opp                              [31] odfs
    [12] orank                            [32] opts        (opp serve pts)
    [13] oseed                            [33] ofirsts
    [14] oentry                           [34] ofwon
    [15] ohand                            [35] oswon
    [16] obday                            [36] ogames
    [17] oht                              [37] osaved
    [18] ocountry                         [38] ochances
    [19] oactive                          [39] obackhand

WICHTIG: Frühere Interpretation ([20]=TPW) war falsch — [20] ist Match-Dauer
in Minuten. TPW muss aus Serve-/Return-Stats hergeleitet werden:
    tpw = firsts_won + seconds_won + (opts - ofwon - oswon)
    total_pts = pts + opts

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
CACHE_TTL_SEC = 24 * 3600
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
    time_min: int        # match duration
    # Own serve
    aces: int
    dfs: int
    svpts: int           # serve points served
    firsts_in: int       # 1st serves in
    firsts_won: int      # 1st serves won
    seconds_won: int     # 2nd serves won
    sv_games: int
    bp_saved: int
    bp_faced: int
    # Opponent serve (= own return)
    o_aces: int
    o_dfs: int
    o_svpts: int
    o_firsts_in: int
    o_firsts_won: int
    o_seconds_won: int
    o_bp_saved: int      # BP saved by opponent = BP we converted-not
    o_bp_faced: int      # BP faced by opponent = BP we generated


@dataclass(frozen=True)
class ServeAggregate:
    """Rolling-window aggregate for one player."""
    n_matches: int
    dominance_rate: float       # TPW / TotalPts — 0.5 = neutral
    ace_rate: float             # aces / svpts
    df_rate: float              # dfs / svpts
    win_rate: float             # W / n_matches
    ace_df_ratio: float         # aces / max(dfs, 1)
    first_serve_pct: float      # firsts_in / svpts
    first_serve_win_pct: float  # firsts_won / firsts_in
    second_serve_win_pct: float # seconds_won / (svpts - firsts_in - dfs)
    bp_save_pct: float          # bp_saved / bp_faced (own service defence)
    bp_conv_pct: float          # 1 - o_bp_saved / o_bp_faced (own return offence)


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

def _cache_path(slug: str, tour: str) -> Path:
    tag = "wta" if tour.lower() == "wta" else "atp"
    return CACHE_DIR / f"{slug}_{tag}.json"


def _load_cache(slug: str, tour: str) -> Optional[list[list]]:
    p = _cache_path(slug, tour)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text())
        if time.time() - payload.get("fetched_at", 0) > CACHE_TTL_SEC:
            return None
        return payload.get("matchmx", [])
    except Exception:
        return None


def _save_cache(slug: str, tour: str, matchmx: list[list]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(slug, tour).write_text(json.dumps({
        "fetched_at": time.time(),
        "matchmx": matchmx,
    }, ensure_ascii=False))


def _fetch_matchmx(
    slug: str, timeout: float = 10.0, tour: str = "atp",
) -> Optional[list[list]]:
    """Fetch raw matchmx array for a player. Cached (24h TTL).

    ATP: /cgi-bin/player-classic.cgi        (matchmx inline im HTML)
    WTA: /jsmatches/{Slug}.js               (matchmx als separates JS-File,
                                             wplayer-classic.cgi referenziert es)
    """
    cached = _load_cache(slug, tour)
    if cached is not None:
        return cached

    if tour.lower() == "wta":
        urls = [f"https://www.tennisabstract.com/jsmatches/{slug}.js"]
    else:
        urls = [f"https://www.tennisabstract.com/cgi-bin/player-classic.cgi?p={slug}"]

    for url in urls:
        try:
            r = retry_request(
                "GET", url, headers=_UA, timeout=timeout,
                retries=3, backoff=(2, 5, 15),
                retry_on_status={429, 502, 503, 504},
                log_prefix="[tennis_stats]",
            )
            if r is None or r.status_code != 200:
                continue
            # ATP-HTML enthält Spielername; JS-Frag nicht zwangsläufig — dann skip
            if url.endswith(".cgi"):
                name_tokens = re.findall(r"[A-Z][a-z]+", slug)
                if not any(tok in r.text for tok in name_tokens if len(tok) >= 3):
                    continue
            m = _MATCHMX_RE.search(r.text)
            if not m:
                continue
            rows = ast.literal_eval(m.group(1))
            _save_cache(slug, tour, rows)
            return rows
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Row → MatchStat parser
# ---------------------------------------------------------------------------

def _to_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except (ValueError, TypeError):
        return default


def _parse_row(row: list) -> Optional[MatchStat]:
    """Convert raw matchmx row → MatchStat. Skips walkovers/incomplete stats."""
    if len(row) < 39:
        return None
    score = str(row[9] or "")
    if not score or any(t in score for t in ("W/O", "DEF")):
        return None
    svpts = _to_int(row[23])
    o_svpts = _to_int(row[32])
    if svpts <= 0 or o_svpts <= 0:
        return None
    return MatchStat(
        date=str(row[0] or ""),
        surface=str(row[2] or "").lower(),
        result=str(row[4] or ""),
        opponent=str(row[11] or ""),
        time_min=_to_int(row[20]),
        aces=_to_int(row[21]),
        dfs=_to_int(row[22]),
        svpts=svpts,
        firsts_in=_to_int(row[24]),
        firsts_won=_to_int(row[25]),
        seconds_won=_to_int(row[26]),
        sv_games=_to_int(row[27]),
        bp_saved=_to_int(row[28]),
        bp_faced=_to_int(row[29]),
        o_aces=_to_int(row[30]),
        o_dfs=_to_int(row[31]),
        o_svpts=o_svpts,
        o_firsts_in=_to_int(row[33]),
        o_firsts_won=_to_int(row[34]),
        o_seconds_won=_to_int(row[35]),
        o_bp_saved=_to_int(row[37]),
        o_bp_faced=_to_int(row[38]),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_match_stats(player_name: str, tour: str = "atp") -> list[MatchStat]:
    """Return list of MatchStat for a player (most recent first).

    tour: 'atp' (default) or 'wta'.
    """
    slug = _to_ta_slug(player_name)
    if not slug:
        return []
    raw = _fetch_matchmx(slug, tour=tour)
    if not raw:
        return []
    return [ms for ms in (_parse_row(r) for r in raw) if ms is not None]


def _empty_aggregate() -> ServeAggregate:
    return ServeAggregate(
        n_matches=0,
        dominance_rate=0.5,
        ace_rate=0.0,
        df_rate=0.0,
        win_rate=0.5,
        ace_df_ratio=1.0,
        first_serve_pct=0.60,
        first_serve_win_pct=0.65,
        second_serve_win_pct=0.50,
        bp_save_pct=0.60,
        bp_conv_pct=0.40,
    )


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

    svpts = sum(s.svpts for s in window)
    o_svpts = sum(s.o_svpts for s in window)
    aces = sum(s.aces for s in window)
    dfs = sum(s.dfs for s in window)
    firsts_in = sum(s.firsts_in for s in window)
    firsts_won = sum(s.firsts_won for s in window)
    seconds_won = sum(s.seconds_won for s in window)
    bp_saved = sum(s.bp_saved for s in window)
    bp_faced = sum(s.bp_faced for s in window)
    o_firsts_won = sum(s.o_firsts_won for s in window)
    o_seconds_won = sum(s.o_seconds_won for s in window)
    o_bp_saved = sum(s.o_bp_saved for s in window)
    o_bp_faced = sum(s.o_bp_faced for s in window)
    wins = sum(1 for s in window if s.result == "W")

    tpw = firsts_won + seconds_won + (o_svpts - o_firsts_won - o_seconds_won)
    total_pts = svpts + o_svpts
    seconds_in = max(svpts - firsts_in - dfs, 0)

    return ServeAggregate(
        n_matches=len(window),
        dominance_rate=(tpw / total_pts) if total_pts > 0 else 0.5,
        ace_rate=(aces / svpts) if svpts > 0 else 0.0,
        df_rate=(dfs / svpts) if svpts > 0 else 0.0,
        win_rate=wins / len(window),
        ace_df_ratio=aces / max(dfs, 1),
        first_serve_pct=(firsts_in / svpts) if svpts > 0 else 0.60,
        first_serve_win_pct=(firsts_won / firsts_in) if firsts_in > 0 else 0.65,
        second_serve_win_pct=(seconds_won / seconds_in) if seconds_in > 0 else 0.50,
        bp_save_pct=(bp_saved / bp_faced) if bp_faced > 0 else 0.60,
        bp_conv_pct=(1.0 - o_bp_saved / o_bp_faced) if o_bp_faced > 0 else 0.40,
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


_SNAP_DIR = _ROOT / "data" / "cache" / "tennis_serve_snapshots"


def save_serve_snapshot(
    player_name: str,
    surface: str,
    match_date: str,
    agg: "ServeAggregate",
    tour: str = "atp",
) -> None:
    """Persistiert asof-Snapshot für Walk-Forward-Training (J8-I7 / J2-N).

    Idempotent: überschreibt nicht wenn Datei bereits existiert.
    Format: data/cache/tennis_serve_snapshots/{slug}_{surface}_{date}.json
    """
    import dataclasses
    from datetime import datetime, timezone
    slug = _to_ta_slug(player_name)
    surface_key = (surface or "unknown").lower()
    _SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap_path = _SNAP_DIR / f"{slug}_{surface_key}_{match_date}.json"
    if snap_path.exists():
        return
    data: dict = {
        "player": player_name,
        "slug": slug,
        "surface": surface_key,
        "match_date": match_date,
        "tour": tour,
        "snapshot_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **dataclasses.asdict(agg),
    }
    snap_path.write_text(json.dumps(data))

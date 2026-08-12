"""Tennisexplorer.com Sekundär-Scraper für Turniere die TheOddsAPI nicht listet.

TheOddsAPI listet aktuell nur 2 aktive Tennis-Turniere (Washington ATP+WTA).
Kitzbühel, Los Cabos, Prag WTA, Vancouver WTA, Memphis, Challenger, ITF etc.
fehlen komplett. TE hat sie alle in `/next/`.

Konzept:
- Discovery: `/next/` liefert alle Matches der nächsten 24-48h
- Detail: `/match-detail/?id=<n>` hat Odds pro Bookmaker (Home/Away Markt)
- Best-Price-Aggregation über alle Bookies → analog TheOddsAPI-Output-Format
- Cache: 30 Min (Odds bewegen sich langsam bei kleineren Events)

Public API:
    fetch_te_upcoming_matches(min_bookies=1) -> list[dict]
        gibt list von matches im tennis_scan-format zurück:
        {match_id, player_a, player_b, commence_time, odds_a, odds_b,
         ah_odds_a=0, ah_odds_b=0, first_set_odds_a=0, first_set_odds_b=0,
         totals_over={}, totals_under={}, scorelines={},
         sport_key="te:{tournament_slug}",
         te_tournament, te_bookies_count}
"""
from __future__ import annotations

import pickle
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_EUROPE_BERLIN = ZoneInfo("Europe/Berlin")
from pathlib import Path

import requests

from src.config import DATA_CACHE

_BASE = "https://www.tennisexplorer.com"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
_CACHE_PATH = DATA_CACHE / "te_upcoming.pkl"
_CACHE_TTL_S = 30 * 60  # 30 min

# Regex: match-detail links auf /next/
_RE_MATCH_ID = re.compile(r'/match-detail/\?id=(\d+)')

# Blocklist: UTR Pro Tennis Series ist kein ATP/WTA-Event — kein Model-Support.
# Wenn das HTML eines Match-Details einen dieser Strings enthält → None zurückgeben.
_TE_UTR_BLOCKLIST_RE = re.compile(r'UTR\s*Pro\s*Tennis|utr-pro-tennis|utr-tennis-series', re.IGNORECASE)

# Detail-Page: Home/Away-Sektion mit Player-Namen + Odds-Zeilen
_RE_HOMEAWAY_HEAD = re.compile(
    r'<td class="k1">([^<]+)</td>\s*<td class="k2">([^<]+)</td>',
    re.DOTALL,
)
# Odds-Row-Pattern: eine Bookmaker-Zeile mit zwei Odds (k1, k2)
_RE_ODDS_ROW = re.compile(
    r'<tr class="(?:one|two)">.*?<td class="k1[^"]*"><div class="odds-in[^"]*">(\d+\.\d{2})</div>.*?'
    r'<td class="k2[^"]*"><div class="odds-in[^"]*">(\d+\.\d{2})</div>',
    re.DOTALL,
)
# Tournament-Link: /<slug>/2026/(atp-men|wta-women)/. Erster Treffer = eigenes Match-Turnier.
_RE_TOURNAMENT_LINK = re.compile(
    r'<a href="/([^/"]+)/2026/(atp-men|wta-women)[^"]*"[^>]*>([A-Z][^<]{2,40})</a>'
)
# Kickoff-Zeit im Detail-Header (z.B. "31.07. 22:00")
_RE_KICKOFF = re.compile(r'(\d{2}\.\d{2}\.)\s*(\d{2}:\d{2})')


def _http_get(url: str, timeout: int = 15) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception:
        return None
    return None


def _parse_commence_time(dtstr: str, timestr: str) -> str:
    """'31.07.', '22:00' → ISO-8601 UTC using Europe/Berlin timezone.

    TE displays match times in local Central European time (CET/CEST).
    zoneinfo handles the CET↔CEST DST boundary automatically so we do not
    hardcode +1/+2 offsets.  Signal IDs are date-only so the UTC conversion
    does not change fixture identity for matches within normal playing hours.
    """
    try:
        year = datetime.now(timezone.utc).year
        d, m, _ = dtstr.split(".")
        h, mi = int(timestr[:2]), int(timestr[3:5])
        local_dt = datetime(year, int(m), int(d), h, mi,
                            tzinfo=_EUROPE_BERLIN)
        utc_dt = local_dt.astimezone(timezone.utc)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _discover_match_ids() -> list[str]:
    """Holt alle unique match-detail-IDs aus /next/."""
    html = _http_get(f"{_BASE}/next/")
    if not html:
        return []
    return list(dict.fromkeys(_RE_MATCH_ID.findall(html)))  # preserve order, dedup


def _fetch_match_detail(match_id: str) -> dict | None:
    """Holt Odds + Metadata für ein Match. Best-Price-Aggregation über alle Bookies."""
    html = _http_get(f"{_BASE}/match-detail/?id={match_id}")
    if not html:
        return None

    # Player-Namen aus Home/Away-Head
    head = _RE_HOMEAWAY_HEAD.search(html)
    if not head:
        return None
    player_a = head.group(1).strip()
    player_b = head.group(2).strip()

    # Odds — TE-Detail-Seite mischt aktuelles H2H mit HEAD-TO-HEAD-Historie
    # (frühere Duelle derselben Spieler). Beide Blöcke haben identische
    # <tr class="one/two"> k1/k2-Struktur → re.DOTALL greift alle.
    # Strategie:
    # 1) Nur Pairs mit plausiblem Overround (0.95-1.15) behalten
    # 2) Nach Favorit clustern (A-fav vs B-fav)
    # 3) Größeren Cluster wählen (aktuelles Match hat i.d.R. mehr Bookies)
    # 4) MEDIAN pro Seite = Konsensus (max_per_side kombiniert falsche Bookies)
    from statistics import median as _median
    odds_raw = _RE_ODDS_ROW.findall(html)
    if not odds_raw:
        return None
    h2h_pairs: list[tuple[float, float]] = []
    for a_s, b_s in odds_raw:
        try:
            a = float(a_s); b = float(b_s)
        except ValueError:
            continue
        if a < 1.02 or b < 1.02 or a > 50 or b > 50:
            continue
        overround = (1.0 / a) + (1.0 / b)
        if 0.95 <= overround <= 1.15:
            h2h_pairs.append((a, b))
    if not h2h_pairs:
        return None
    a_fav = [(a, b) for a, b in h2h_pairs if a < b]
    b_fav = [(a, b) for a, b in h2h_pairs if a >= b]
    cluster = a_fav if len(a_fav) >= len(b_fav) else b_fav
    if not cluster:
        return None
    best_a = round(_median([a for a, _ in cluster]), 2)
    best_b = round(_median([b for _, b in cluster]), 2)
    # Finaler Sanity-Check auf dem Konsens-Preis
    if not (0.95 <= (1.0 / best_a) + (1.0 / best_b) <= 1.15):
        return None
    odds = cluster

    # Turnier-Slug + Tour (atp-men | wta-women) aus erstem passendem Link
    tour_m = _RE_TOURNAMENT_LINK.search(html)
    tournament = ""
    tour = ""
    slug = ""
    if tour_m:
        slug = tour_m.group(1).strip()
        tour = "atp" if tour_m.group(2) == "atp-men" else "wta"
        tournament = tour_m.group(3).strip()

    # UTR Pro Tennis Series blocklist: check the MATCH'S OWN tournament slug only.
    # TE sidebar links to UTR appear on every page — checking full HTML would
    # false-positive on legitimate Challenger/ATP pages. Slug is deterministic.
    if _TE_UTR_BLOCKLIST_RE.search(slug):
        return None

    # Kickoff
    ko_m = _RE_KICKOFF.search(html)
    commence = _parse_commence_time(ko_m.group(1), ko_m.group(2)) if ko_m else ""

    return {
        "match_id": f"te_{match_id}",
        "player_a": player_a, "player_b": player_b,
        "commence_time": commence,
        "odds_a": best_a, "odds_b": best_b,
        "ah_odds_a": 0.0, "ah_odds_b": 0.0,
        "first_set_odds_a": 0.0, "first_set_odds_b": 0.0,
        "totals_over": {}, "totals_under": {},
        "scorelines": {},
        "sport_key": f"te:{slug}_{tour}" if slug else "te:unknown",
        "te_tournament": tournament,
        "te_tour": tour,
        "te_slug": slug,
        "te_bookies_count": len(odds),
    }


def fetch_te_upcoming_matches(
    min_bookies: int = 1,
    max_matches: int = 200,
    use_cache: bool = True,
    max_workers: int = 4,
) -> list[dict]:
    """Public API. Liefert alle TE-Matches der nächsten 24-48h.

    Args:
      min_bookies: skippt Matches mit weniger Bookmaker-Odds (Rauschen-Filter).
      max_matches: hartes Limit (Netzwerk-Guard, ~1s pro Match).
      use_cache: 30-Min-Disk-Cache.
      max_workers: parallel detail-fetches.
    """
    if use_cache and _CACHE_PATH.exists():
        age = time.time() - _CACHE_PATH.stat().st_mtime
        if age < _CACHE_TTL_S:
            try:
                return pickle.loads(_CACHE_PATH.read_bytes())
            except Exception:
                pass

    ids = _discover_match_ids()[:max_matches]
    if not ids:
        return []

    matches: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_match_detail, mid): mid for mid in ids}
        for fut in as_completed(futures):
            try:
                m = fut.result()
            except Exception:
                m = None
            if m and m.get("te_bookies_count", 0) >= min_bookies:
                matches.append(m)

    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    try:
        _CACHE_PATH.write_bytes(pickle.dumps(matches))
    except Exception:
        pass
    return matches

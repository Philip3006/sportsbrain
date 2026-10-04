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
from html import unescape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

_EUROPE_BERLIN = ZoneInfo("Europe/Berlin")
from pathlib import Path

import requests

from src.config import DATA_CACHE

_BASE = "https://www.tennisexplorer.com"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
_CACHE_PATH = DATA_CACHE / "te_upcoming.pkl"
_CACHE_TTL_S = 30 * 60  # 30 min
# Bound exact lookups to the existing schedule-only inventory ceiling.
_MAX_TARGETED_MATCH_DETAILS = 25
_NEXT_HTML: str | None = None
_NEXT_HTML_TS = 0.0
_TARGETED_MATCH_DETAIL_COUNT = 0

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
# Kickoff-Zeit im Detail-Header. Aktuelle Seiten enthalten das Jahr
# ("05.10.2026, 05:00"); historische Seiten teils nur "31.07. 22:00".
# Immer den datierten Matchkopf vor Odds-Verlaufszeitstempeln bevorzugen.
_RE_KICKOFF_WITH_YEAR = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\s*,?\s*(\d{2}:\d{2})\b")
_RE_KICKOFF = re.compile(r"\b(\d{2}\.\d{2}\.)\s*(\d{2}:\d{2})\b")


def _http_get(url: str, timeout: int = 15) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception:
        return None
    return None


def _parse_commence_time(dtstr: str, timestr: str) -> str:
    """Parse TE's dated or legacy header in Europe/Berlin local time.

    TE displays match times in local Central European time (CET/CEST).
    zoneinfo handles the CET↔CEST DST boundary automatically so we do not
    hardcode +1/+2 offsets. A year supplied by the current page is retained;
    older pages without a year use the current UTC year.
    """
    try:
        d, m, *year_part = dtstr.split(".")
        year = (
            int(year_part[0])
            if year_part and year_part[0]
            else datetime.now(timezone.utc).year
        )
        h, mi = int(timestr[:2]), int(timestr[3:5])
        local_dt = datetime(year, int(m), int(d), h, mi, tzinfo=_EUROPE_BERLIN)
        utc_dt = local_dt.astimezone(timezone.utc)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _discover_match_ids() -> list[str]:
    """Holt alle unique match-detail-IDs aus /next/."""
    html = _get_next_html()
    if not html:
        return []
    return list(dict.fromkeys(_RE_MATCH_ID.findall(html)))  # preserve order, dedup


def _get_next_html() -> str | None:
    """Return one short-lived /next/ page snapshot shared by bulk and exact lookup."""
    global _NEXT_HTML, _NEXT_HTML_TS, _TARGETED_MATCH_DETAIL_COUNT
    if _NEXT_HTML and time.time() - _NEXT_HTML_TS < _CACHE_TTL_S:
        return _NEXT_HTML
    html = _http_get(f"{_BASE}/next/")
    if html:
        _NEXT_HTML = html
        _NEXT_HTML_TS = time.time()
        _TARGETED_MATCH_DETAIL_COUNT = 0
    return html


class _NamedMatchLinkParser(HTMLParser):
    """Find explicit match-detail anchors whose visible text names both players."""

    def __init__(self, wanted: frozenset[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.wanted = wanted
        self._active_id: str | None = None
        self._active_text: list[str] = []
        self.match_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        match = _RE_MATCH_ID.search(href)
        if match:
            self._active_id = match.group(1)
            self._active_text = []

    def handle_data(self, data: str) -> None:
        if self._active_id is not None:
            self._active_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._active_id is None:
            return
        label = " ".join(" ".join(self._active_text).split())
        participants = re.split(r"\s+[–—-]\s+", label, maxsplit=1)
        if len(participants) == 2:
            from src.tennis.name_norm import to_elo_name_from_te

            pair = frozenset(
                to_elo_name_from_te(name).casefold() for name in participants
            )
            if pair == self.wanted:
                self.match_ids.append(self._active_id)
        self._active_id = None
        self._active_text = []


def _named_match_ids(html: str, player_a: str, player_b: str) -> list[str]:
    """Resolve only exact two-player match links, independent of their page rank."""
    from src.tennis.name_norm import to_elo_name_from_odds_api

    wanted = frozenset(
        to_elo_name_from_odds_api(name).casefold() for name in (player_a, player_b)
    )
    if len(wanted) != 2 or "" in wanted:
        return []
    parser = _NamedMatchLinkParser(wanted)
    parser.feed(html)
    return list(dict.fromkeys(parser.match_ids))


def fetch_te_match_for_hint(match_hint: dict, min_bookies: int = 2) -> dict | None:
    """Fetch one exact requested fixture omitted by the bounded 200-ID bulk.

    The existing bulk limit is intentionally unchanged. This fallback follows
    an explicit two-player match link from the same /next/ response, then
    validates participants and UTC kickoff date again on its detail response.
    """
    global _TARGETED_MATCH_DETAIL_COUNT
    player_a = str(match_hint.get("player_a", "")).strip()
    player_b = str(match_hint.get("player_b", "")).strip()
    if not player_a or not player_b:
        return None
    requested_kickoff = _parse_aware_utc(match_hint.get("commence_time"))
    if requested_kickoff is None:
        return None
    html = _get_next_html()
    if not html:
        return None

    for match_id in _named_match_ids(html, player_a, player_b):
        if _TARGETED_MATCH_DETAIL_COUNT >= _MAX_TARGETED_MATCH_DETAILS:
            return None
        _TARGETED_MATCH_DETAIL_COUNT += 1
        match = _fetch_match_detail(match_id)
        if not match or int(match.get("te_bookies_count", 0) or 0) < min_bookies:
            continue
        from src.tennis.name_norm import to_elo_name_from_odds_api, to_elo_name_from_te

        requested_pair = frozenset(
            to_elo_name_from_odds_api(name).casefold() for name in (player_a, player_b)
        )
        returned_pair = frozenset(
            to_elo_name_from_te(str(match.get(field, ""))).casefold()
            for field in ("player_a", "player_b")
        )
        if len(returned_pair) != 2 or returned_pair != requested_pair:
            continue
        returned_kickoff = _parse_aware_utc(match.get("commence_time"))
        if (
            returned_kickoff is None
            or returned_kickoff.date() != requested_kickoff.date()
        ):
            continue
        return match
    return None


def _parse_aware_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fetch_match_detail(match_id: str) -> dict | None:
    """Holt Odds + Metadata für ein Match. Best-Price-Aggregation über alle Bookies."""
    html = _http_get(f"{_BASE}/match-detail/?id={match_id}")
    if not html:
        return None
    # This is the capture time of the provider response. Keep it with the cached
    # quote so a later disk-cache read cannot masquerade as a new observation.
    source_observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
    visible_text = unescape(re.sub(r"<[^>]+>", " ", html))
    ko_m = _RE_KICKOFF_WITH_YEAR.search(visible_text) or _RE_KICKOFF.search(
        visible_text
    )
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
        "source_observed_at": source_observed_at,
    }


def _bulk_has_current_source_times(matches: object) -> bool:
    if not isinstance(matches, list):
        return False
    if not matches:
        return True

    now = datetime.now(timezone.utc)
    for match in matches:
        if not isinstance(match, dict):
            return False
        value = match.get("source_observed_at")
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            observed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if observed_at.tzinfo is None:
            return False
        age_seconds = (now - observed_at.astimezone(timezone.utc)).total_seconds()
        if age_seconds < 0 or age_seconds > _CACHE_TTL_S:
            return False
    return True


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
                cached = pickle.loads(_CACHE_PATH.read_bytes())
                if _bulk_has_current_source_times(cached):
                    return cached
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

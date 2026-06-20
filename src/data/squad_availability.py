"""
Squad availability layer with live Transfermarkt injury scraping.

Data flow:
  fetch_transfermarkt_squad(team, match_date)
    → scrapes transfermarkt.com/kader page (Playwright, JS-rendered)
    → finds span.verletzt-table (injuries) + span.gesperrt-table (suspensions)
    → caches to data/cache/squad/{team}.json (24h TTL)
    → returns list[PlayerStatus] (unavailable players only)

  squad_report(team, match_date) → SquadReport
    → calls fetch_transfermarkt_squad
    → if empty, tries _fetch_wikipedia_squad (requests + bs4, no JS needed)
    → falls back to default_report only if both sources return empty

Usage in scanner:
    from src.data.squad_availability import squad_report
    report_home = squad_report("Germany", match_date)
    report_away = squad_report("France", match_date)
    features.update(squad_impact_features(report_home, report_away))
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import requests

from scripts._http_retry import retry_request
from src.config import DATA_CACHE


# ---------------------------------------------------------------------------
# Availability weights per status
# ---------------------------------------------------------------------------
AVAILABILITY = {
    "fit":       1.0,
    "doubtful":  0.7,
    "injured":   0.0,
    "suspended": 0.0,
}

# Transfermarkt: canonical_name → (slug, verein_id)
# Covers all 48 WM 2026 qualified nations + major tournament teams
_TM_TEAMS: dict[str, tuple[str, str]] = {
    # CONCACAF
    "United States":  ("united-states", "3617"),
    "Mexico":         ("mexico", "3824"),
    "Canada":         ("kanada", "3378"),
    "Panama":         ("panama", "3823"),
    "Costa Rica":     ("costa-rica", "3625"),
    "Honduras":       ("honduras", "3628"),
    "Jamaica":        ("jamaika", "3825"),
    "El Salvador":    ("el-salvador", "3632"),
    "Guatemala":      ("guatemala", "3629"),
    # CONMEBOL
    "Argentina":      ("argentinien", "3437"),
    "Brazil":         ("brasilien", "3439"),
    "Uruguay":        ("uruguay", "3441"),
    "Colombia":       ("kolumbien", "3449"),
    "Ecuador":        ("ecuador", "3451"),
    "Venezuela":      ("venezuela", "3456"),
    "Paraguay":       ("paraguay", "3442"),
    "Bolivia":        ("bolivien", "3444"),
    "Chile":          ("chile", "3443"),
    "Peru":           ("peru", "3448"),
    # UEFA
    "Germany":        ("deutschland", "3262"),
    "France":         ("frankreich", "3377"),
    "Spain":          ("spanien", "3375"),
    "Portugal":       ("portugal", "3876"),
    "England":        ("england", "6566"),
    "Netherlands":    ("niederlande", "3379"),
    "Belgium":        ("belgien", "3382"),
    "Italy":          ("italien", "3376"),
    "Croatia":        ("kroatien", "3556"),
    "Austria":        ("osterreich", "3380"),
    "Switzerland":    ("schweiz", "3381"),
    "Denmark":        ("danemark", "3383"),
    "Poland":         ("polen", "3385"),
    "Serbia":         ("serbien", "3557"),
    "Ukraine":        ("ukraine", "3427"),
    "Turkey":         ("turkei", "3419"),
    "Scotland":       ("schottland", "3514"),
    "Hungary":        ("ungarn", "3388"),
    "Slovakia":       ("slowakei", "3386"),
    "Albania":        ("albanien", "3389"),
    "Czech Republic": ("tschechien", "3390"),
    "Czechia":        ("tschechien", "3390"),  # canonical name used in DC model
    "Romania":        ("rumanien", "3392"),
    "Slovenia":       ("slowenien", "3558"),
    "Georgia":        ("georgien", "3560"),
    # CAF
    "Morocco":        ("marokko", "3898"),
    "Senegal":        ("senegal", "3664"),
    "Nigeria":        ("nigeria", "3667"),
    "Egypt":          ("agypten", "3668"),
    "Ivory Coast":    ("elfenbeinskuste", "3655"),
    "Cote d'Ivoire":  ("elfenbeinskuste", "3655"),
    "South Africa":   ("sudafrika", "3660"),
    "Algeria":        ("algerien", "3671"),
    "Tunisia":        ("tunesien", "3670"),
    "Ghana":          ("ghana", "3656"),
    "Cameroon":       ("kamerun", "3662"),
    "Mali":           ("mali", "3672"),
    "DR Congo":       ("demokratische-republik-kongo", "11458"),
    # AFC
    "Japan":          ("japan", "3669"),
    "South Korea":    ("sudkorea", "3384"),
    "Saudi Arabia":   ("saudi-arabien", "3387"),
    "Iran":           ("iran", "3396"),
    "Australia":      ("australien", "3403"),
    "Indonesia":      ("indonesien", "3821"),
    "New Zealand":    ("neuseeland", "3405"),
    "Uzbekistan":     ("usbekistan", "3562"),
    "Qatar":          ("katar", "3397"),
    # OFC
    "New Caledonia":  ("neukaledonien", "3854"),
    # WM 2026 — additional qualifiers
    "Bosnia and Herzegovina": ("bosnien-herzegowina", "3447"),
    "Sweden":         ("schweden", "3394"),
    "Norway":         ("norwegen", "3393"),
    "Haiti":          ("haiti", "3626"),
    "Curacao":        ("curacao", "3909"),
    "Cape Verde":     ("kap-verde", "3673"),
    "Iraq":           ("irak", "3406"),
    "Jordan":         ("jordanien", "3561"),
}

_CACHE_DIR = DATA_CACHE / "squad"
_CACHE_TTL_HOURS = 24

_SUSPENSIONS_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "suspensions.json"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
}

_POS_MAP = {
    "goalkeeper": "GK", "keeper": "GK",
    "centre-back": "DEF", "left-back": "DEF", "right-back": "DEF",
    "defender": "DEF", "back": "DEF",
    "defensive mid": "MID", "central mid": "MID", "attacking mid": "MID",
    "midfield": "MID",
    "left wing": "FWD", "right wing": "FWD",
    "centre-forward": "FWD", "forward": "FWD", "striker": "FWD",
}

# Return date patterns in Transfermarkt injury titles:
# "Ankle injury - Return unknown"
# "Adductor injury - Return expected on 20/07/2026"
_RETURN_RE = re.compile(r"Return expected on (\d{2}/\d{2}/\d{4})", re.I)

# Market value parsing: "€10.00m" → 10.0, "€500k" → 0.5, "€1.50bn" → 1500.0
_MV_RE = re.compile(r"€\s*([\d.,]+)\s*([kmbn]+)?", re.I)


def _parse_market_value_m(text: str) -> float:
    """Returns market value in millions EUR. 0.0 if unparseable."""
    if not text:
        return 0.0
    m = _MV_RE.search(text.replace("\xa0", " "))
    if not m:
        return 0.0
    try:
        num = float(m.group(1).replace(",", ".").rstrip("."))
    except ValueError:
        return 0.0
    suffix = (m.group(2) or "").lower()
    if "b" in suffix:
        return num * 1000.0  # billion → millions
    if "m" in suffix:
        return num
    if "k" in suffix:
        return num / 1000.0
    # No suffix on TM is unusual; treat as raw EUR
    return num / 1_000_000.0


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PlayerStatus:
    name: str
    position: str = "unknown"
    availability: float = 1.0
    status: str = "fit"
    key_player: bool = False
    p_plays: float = 1.0
    market_value_eur_m: float = 0.0  # Transfermarkt-derived, 0.0 when unknown


@dataclass
class SquadReport:
    team: str
    report_date: pd.Timestamp
    players: list[PlayerStatus] = field(default_factory=list)
    data_source: str = "default"
    suspended_count: int = 0

    @property
    def availability_score(self) -> float:
        if not self.players:
            return 1.0
        return float(sum(p.availability for p in self.players) / len(self.players))

    @property
    def risk_players(self) -> list[PlayerStatus]:
        return sorted(
            [p for p in self.players if p.key_player and p.availability < 1.0],
            key=lambda p: p.availability,
        )

    @property
    def ampel_status(self) -> str:
        s = self.availability_score
        if s >= 0.95:
            return "🟢"
        elif s >= 0.80:
            return "🟡"
        return "🔴"

    @property
    def fatigue_flag(self) -> bool:
        return self.availability_score < 0.90


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def default_report(team: str, match_date: pd.Timestamp) -> SquadReport:
    """Fully-fit placeholder. data_source='default' signals no real data."""
    return SquadReport(team=team, report_date=match_date, players=[], data_source="default")


# ---------------------------------------------------------------------------
# Suspension overlay — manual JSON-based tracking
# ---------------------------------------------------------------------------

def load_suspensions() -> dict[str, list[str]]:
    """Load manually maintained suspension list from data/suspensions.json."""
    if not _SUSPENSIONS_FILE.exists():
        return {}
    try:
        data = json.loads(_SUSPENSIONS_FILE.read_text())
        # Filter out comment keys (starting with _)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception:
        return {}


def get_suspended_players(team: str) -> list[str]:
    """Returns list of suspended player names for a team."""
    suspensions = load_suspensions()
    # Try exact match first, then case-insensitive
    if team in suspensions:
        return suspensions[team]
    for k, v in suspensions.items():
        if k.lower() == team.lower():
            return v
    return []


def _apply_suspension_overlay(
    players: list[PlayerStatus],
    team: str,
) -> tuple[list[PlayerStatus], int]:
    """
    Applies manual suspension overlay to player list.
    Returns (updated_players, suspended_count).
    Matches are case-insensitive and support partial name matching
    (e.g. "Rodrygo" matches "Rodrygo Goes").
    """
    suspended = get_suspended_players(team)
    if not suspended:
        return players, 0

    count = 0
    for player in players:
        if any(
            player["name"].lower() in s.lower() or s.lower() in player["name"].lower()
            if isinstance(player, dict)
            else player.name.lower() in s.lower() or s.lower() in player.name.lower()
            for s in suspended
        ):
            if isinstance(player, dict):
                player["available"] = False
                player["status"] = "suspended"
            else:
                player.status = "suspended"
                player.availability = 0.0
            count += 1
    return players, count


def _apply_suspension_overlay_to_statuses(
    players: list[PlayerStatus],
    team: str,
) -> tuple[list[PlayerStatus], int]:
    """
    Applies manual suspension overlay to a list[PlayerStatus].
    Returns (updated_players, suspended_count).
    """
    suspended = get_suspended_players(team)
    if not suspended:
        return players, 0

    count = 0
    for player in players:
        if any(
            player.name.lower() in s.lower() or s.lower() in player.name.lower()
            for s in suspended
        ):
            player.status = "suspended"
            player.availability = 0.0
            count += 1
    return players, count


def _fetch_covers_squad(team: str, match_date: pd.Timestamp) -> list[PlayerStatus]:
    """
    Fetches injury data from covers.com and converts to PlayerStatus list.
    Returns a small list of unavailable/doubtful players (not the full squad).
    Used as primary real-injury source before Transfermarkt.
    """
    try:
        from src.data.injury_data import get_team_injuries
    except ImportError:
        return []

    injuries = get_team_injuries(team)
    if not injuries:
        return []

    players = []
    for inj in injuries:
        players.append(PlayerStatus(
            name=inj["player"],
            position="unknown",
            availability=inj["availability"],
            status=inj["status"],
            key_player=True,
            p_plays=inj["availability"],
        ))
    return players


def _overlay_sofascore_values(team: str, players: list[PlayerStatus]) -> None:
    """Best-effort overlay of Sofascore-derived per-player market values onto
    a SquadReport's PlayerStatus list. Silently does nothing on failure.
    """
    try:
        from src.data.sofascore import overlay_player_values
        overlay_player_values(team, players)
    except Exception:
        pass


def squad_report(
    team: str,
    match_date: pd.Timestamp,
    force: bool = False,
) -> SquadReport:
    """
    Returns SquadReport for team at match_date.
    Priority: Covers.com injuries → Transfermarkt → Wikipedia → default_report.
    Suspension overlay (data/suspensions.json) is applied on top of all sources.
    Sofascore market values are overlayed onto the chosen source's player list.
    """
    # Covers.com: structured injury data, scrapable without bot protection
    covers_players = _fetch_covers_squad(team, match_date)
    if covers_players:
        covers_players, susp_count = _apply_suspension_overlay_to_statuses(covers_players, team)
        # For covers: we only have injured/doubtful players, not the full squad.
        # Pad with a proxy full-squad so availability_score reflects reality.
        squad_size = 26
        n_injured = len(covers_players)
        fit_players = [
            PlayerStatus(name=f"fit_{i}", position="unknown", availability=1.0, status="fit")
            for i in range(max(0, squad_size - n_injured))
        ]
        all_players = covers_players + fit_players
        _overlay_sofascore_values(team, all_players)
        return SquadReport(
            team=team,
            report_date=match_date,
            players=all_players,
            data_source="covers",
            suspended_count=susp_count,
        )

    players = fetch_transfermarkt_squad(team, match_date, force=force)
    if players:
        players, susp_count = _apply_suspension_overlay_to_statuses(players, team)
        _overlay_sofascore_values(team, players)
        return SquadReport(
            team=team,
            report_date=match_date,
            players=players,
            data_source="transfermarkt",
            suspended_count=susp_count,
        )

    # TM blocked — try consolidated WC squads page (parse API, no bot blocking)
    wiki_players = _fetch_wc_squads_page(team, match_date)
    if not wiki_players:
        wiki_players = _fetch_wikipedia_squad(team, match_date)
    if wiki_players:
        wiki_players, susp_count = _apply_suspension_overlay_to_statuses(wiki_players, team)
        _overlay_sofascore_values(team, wiki_players)
        return SquadReport(
            team=team,
            report_date=match_date,
            players=wiki_players,
            data_source="wikipedia",
            suspended_count=susp_count,
        )

    # Even for default reports, note any known suspensions
    susp_count = len(get_suspended_players(team))
    report = default_report(team, match_date)
    report.suspended_count = susp_count
    return report


# Position-weighted impact (Lever 3 proxy without per-player market values).
# Rationale: GK losses hurt more than outfield since they're rarely replaced 1:1.
# DEF/MID slightly above FWD because tournament defenses rely on chemistry.
_POSITION_IMPACT_WEIGHTS = {
    "GK":  1.30, "Goalkeeper": 1.30,
    "DEF": 1.05, "Defender": 1.05, "Centre-Back": 1.05, "Left-Back": 1.05,
                  "Right-Back": 1.05,
    "MID": 1.05, "Midfielder": 1.05, "Central Midfield": 1.05,
                  "Defensive Midfield": 1.05, "Attacking Midfield": 1.05,
    "FWD": 1.00, "Forward": 1.00, "Centre-Forward": 1.00, "Left Winger": 1.00,
                  "Right Winger": 1.00, "Second Striker": 1.00,
    "unknown": 1.00,
}


def _weighted_impact_lost(report: SquadReport) -> float:
    """Normalized impact lost ∈ [0,1].

    Preferred: per-player market-value share × position weight × (1 − availability).
    Fallback: position-weighted only (used when no per-player values are present —
    e.g. Wikipedia squads or pre-Lever-3 caches).

    The market-value formula reflects the intuition that Mbappé out hurts France
    far more than a backup midfielder out hurts France, even with the same
    binary "1 player missing" signal.
    """
    if not report.players:
        return 0.0

    total_value = sum(p.market_value_eur_m for p in report.players if p.market_value_eur_m > 0)
    if total_value > 0:
        # Value-weighted impact
        return float(sum(
            (p.market_value_eur_m / total_value)
            * _POSITION_IMPACT_WEIGHTS.get(p.position, 1.0)
            * (1.0 - p.availability)
            for p in report.players
            if p.market_value_eur_m > 0
        ))

    # Fallback: position-weight only
    total_w = sum(_POSITION_IMPACT_WEIGHTS.get(p.position, 1.0) for p in report.players)
    if total_w == 0:
        return 0.0
    lost = sum(
        _POSITION_IMPACT_WEIGHTS.get(p.position, 1.0) * (1.0 - p.availability)
        for p in report.players
    )
    return float(lost / total_w)


def squad_impact_features(
    home_report: SquadReport,
    away_report: SquadReport,
) -> dict[str, float]:
    """Numeric features for the model from two SquadReports."""
    impact_h = _weighted_impact_lost(home_report)
    impact_a = _weighted_impact_lost(away_report)
    return {
        "squad_availability_home": home_report.availability_score,
        "squad_availability_away": away_report.availability_score,
        "squad_availability_diff": (
            home_report.availability_score - away_report.availability_score
        ),
        "key_player_risk_home": float(len(home_report.risk_players)),
        "key_player_risk_away": float(len(away_report.risk_players)),
        # Lever 3: position-weighted impact-lost (proxy for per-player value impact)
        "weighted_impact_lost_home": impact_h,
        "weighted_impact_lost_away": impact_a,
        "weighted_impact_lost_diff": impact_h - impact_a,
    }


# ---------------------------------------------------------------------------
# Wikipedia WC-squads page scraper (MediaWiki parse API — no bot blocking)
# ---------------------------------------------------------------------------

# All 48 WM 2026 teams → Wikipedia section index on "2026_FIFA_World_Cup_squads"
_WC_SECTION_MAP: dict[str, int] = {
    "Czech Republic": 2, "Mexico": 3, "South Africa": 4, "South Korea": 5,
    "Bosnia and Herzegovina": 7, "Bosnia & Herzegovina": 7,
    "Canada": 8, "Qatar": 9, "Switzerland": 10,
    "Brazil": 12, "Haiti": 13, "Morocco": 14, "Scotland": 15,
    "Australia": 17, "Paraguay": 18, "Turkey": 19, "United States": 20,
    "Curacao": 22, "Ecuador": 23, "Germany": 24,
    "Ivory Coast": 25, "Cote d'Ivoire": 25,
    "Japan": 27, "Netherlands": 28, "Sweden": 29, "Tunisia": 30,
    "Belgium": 32, "Egypt": 33, "Iran": 34, "New Zealand": 35,
    "Cape Verde": 37, "Saudi Arabia": 38, "Spain": 39, "Uruguay": 40,
    "France": 42, "Iraq": 43, "Norway": 44, "Senegal": 45,
    "Algeria": 47, "Argentina": 48, "Austria": 49, "Jordan": 50,
    "Colombia": 52, "DR Congo": 53, "Portugal": 54, "Uzbekistan": 55,
    "Croatia": 57, "England": 58, "Ghana": 59, "Panama": 60,
    # Aliases
    "USA": 20, "Czechia": 2,
}

_WC_PAGE = "2026_FIFA_World_Cup_squads"
_WIKI_API = "https://en.wikipedia.org/w/api.php"

# Wikitext player template pattern: |name=[[Link|Display Name]] or |name=[[Name]]
_WT_NAME_RE = re.compile(r"\|name=\[\[(?:[^\|\]]+\|)?([^\]]+)\]\]")
_WT_POS_RE  = re.compile(r"\|pos=(GK|DF|MF|FW)")


def _parse_wikitext_squad(wikitext: str) -> list[PlayerStatus]:
    """Extract players from Wikipedia wikitext squad template format."""
    players: list[PlayerStatus] = []
    lines = wikitext.split("\n")
    for line in lines:
        name_m = _WT_NAME_RE.search(line)
        pos_m  = _WT_POS_RE.search(line)
        if not name_m:
            continue
        raw_name = name_m.group(1).strip()
        # Strip disambiguation suffixes like "(soccer)" or "(footballer)"
        raw_name = re.sub(r"\s*\([^)]*\)\s*$", "", raw_name).strip()
        pos_raw = pos_m.group(1) if pos_m else "MF"
        position = _WIKI_POS_MAP.get(pos_raw, "MID")
        players.append(PlayerStatus(
            name=raw_name, position=position,
            availability=1.0, status="fit",
            key_player=True, p_plays=1.0,
        ))
    return players


def _fetch_wc_squads_page(team: str, match_date: pd.Timestamp) -> list[PlayerStatus]:
    """
    Fetches squad from the Wikipedia WC 2026 squads consolidated page using
    the MediaWiki parse API (no HTML scraping — no bot blocking).
    Caches result to data/cache/squad/{team}_wiki.json.
    """
    cache_file = _wiki_cache_path(team)
    if _cache_fresh(cache_file):
        return _load_cache(cache_file)

    section_idx = _WC_SECTION_MAP.get(team)
    if section_idx is None:
        return []

    _wiki_headers = {
        "User-Agent": "SportsBrain/1.0 (WM 2026 squad fetcher; +https://github.com/sportsbrain)",
        "Accept": "application/json",
    }
    try:
        resp = retry_request("GET",
            _WIKI_API,
            params={
                "action": "parse",
                "page": _WC_PAGE,
                "prop": "wikitext",
                "section": section_idx,
                "format": "json",
            },
            headers=_wiki_headers,
            timeout=15,
        )
        time.sleep(0.3)
    except Exception as exc:
        print(f"  [wiki-wc] {team}: request failed — {exc}")
        return []

    if resp.status_code != 200:
        print(f"  [wiki-wc] {team}: HTTP {resp.status_code}")
        return []

    try:
        wikitext = resp.json()["parse"]["wikitext"]["*"]
    except (KeyError, ValueError):
        return []

    players = _parse_wikitext_squad(wikitext)
    if players:
        _save_cache(cache_file, players)
        print(f"  [wiki-wc] {team}: {len(players)} players (WC squads page)")
    else:
        print(f"  [wiki-wc] {team}: no players parsed from wikitext")
    return players


# ---------------------------------------------------------------------------
# Wikipedia squad scraper (requests + bs4 — no JS needed)
# ---------------------------------------------------------------------------

# Wikipedia position abbreviations → internal position codes
_WIKI_POS_MAP = {
    "GK": "GK",
    "DF": "DEF",
    "MF": "MID",
    "FW": "FWD",
    # some pages spell it out
    "Goalkeeper": "GK",
    "Defender": "DEF",
    "Midfielder": "MID",
    "Forward": "FWD",
}

# Map team canonical name to Wikipedia URL slug (overrides default underscore logic)
_WIKI_SLUG_OVERRIDES: dict[str, str] = {
    "United States": "United_States",
    "South Korea": "South_Korea",
    "DR Congo": "DR_Congo",
    "Cote d'Ivoire": "Ivory_Coast",
    "Ivory Coast": "Ivory_Coast",
    "Czech Republic": "Czech_Republic",
    "Saudi Arabia": "Saudi_Arabia",
    "New Zealand": "New_Zealand",
    "New Caledonia": "New_Caledonia",
    "El Salvador": "El_Salvador",
    "Costa Rica": "Costa_Rica",
    "South Africa": "South_Africa",
}


def _wiki_cache_path(team: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = team.lower().replace(" ", "_").replace("/", "_").replace("'", "")
    return _CACHE_DIR / f"{safe}_wiki.json"


def _fetch_wikipedia_squad(
    team: str,
    match_date: pd.Timestamp,
) -> list[PlayerStatus]:
    """
    Fetches the squad list from Wikipedia's WM 2026 team page.
    URL pattern: https://en.wikipedia.org/wiki/{Team}_at_the_2026_FIFA_World_Cup

    Uses requests + BeautifulSoup (no Playwright/JS needed).
    Caches to data/cache/squad/{team}_wiki.json with 24h TTL.
    Returns [] on any error (404, parse failure, bs4 missing, etc.).
    """
    cache_file = _wiki_cache_path(team)
    if _cache_fresh(cache_file):
        return _load_cache(cache_file)

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  [wiki] beautifulsoup4 not installed — run: pip3 install beautifulsoup4")
        return []

    slug = _WIKI_SLUG_OVERRIDES.get(team, team.replace(" ", "_"))
    url = f"https://en.wikipedia.org/wiki/{slug}_at_the_2026_FIFA_World_Cup"

    try:
        resp = retry_request("GET",
            url,
            headers=_HEADERS,
            timeout=15,
        )
        time.sleep(0.5)  # rate limit
    except Exception as exc:
        print(f"  [wiki] {team}: request failed — {exc}")
        return []

    if resp.status_code == 404:
        print(f"  [wiki] {team}: Wikipedia page not found (404) — {url}")
        return []
    if resp.status_code != 200:
        print(f"  [wiki] {team}: HTTP {resp.status_code} — {url}")
        return []

    players = _parse_wikipedia_squad_html(resp.text, team)
    if players:
        _save_cache(cache_file, players)
        print(f"  [wiki] {team}: {len(players)} players parsed from Wikipedia")
    else:
        print(f"  [wiki] {team}: page found but no squad table parsed — {url}")
    return players


def _parse_wikipedia_squad_html(html: str, team: str) -> list[PlayerStatus]:
    """
    Parses the squad table from a Wikipedia WM 2026 team page.
    Wikipedia squad tables have columns like: No., Pos., Name, DOB (Age), Caps, Club.
    Returns list[PlayerStatus] with availability=1.0 (Wikipedia has no injury data).
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html, "html.parser")
    players: list[PlayerStatus] = []

    # Find the squad section — look for a wikitable that contains position column
    # Wikipedia uses class="wikitable" for squad tables
    for table in soup.find_all("table", class_="wikitable"):
        # Check if this table has a position-like column header
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        has_pos = any("pos" in h for h in headers)
        has_name = any("name" in h or "player" in h for h in headers)
        if not (has_pos and has_name):
            continue

        # Determine column indices from the header row
        header_row = table.find("tr")
        if not header_row:
            continue
        cols = [th.get_text(strip=True).lower() for th in header_row.find_all("th")]

        pos_idx = next((i for i, c in enumerate(cols) if "pos" in c), None)
        name_idx = next(
            (i for i, c in enumerate(cols) if "name" in c or "player" in c), None
        )
        dob_idx = next(
            (i for i, c in enumerate(cols) if "dob" in c or "birth" in c or "age" in c),
            None,
        )

        if pos_idx is None or name_idx is None:
            continue

        for row in table.find_all("tr")[1:]:  # skip header
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(pos_idx, name_idx):
                continue

            pos_raw = cells[pos_idx].get_text(strip=True)
            name_raw = cells[name_idx].get_text(strip=True)

            # Clean name: remove footnote references like [1] or (c) captain markers
            name = re.sub(r"\[.*?\]|\(c\)", "", name_raw).strip()
            if not name:
                continue

            position = _WIKI_POS_MAP.get(pos_raw, "unknown")

            players.append(PlayerStatus(
                name=name,
                position=position,
                availability=1.0,   # Wikipedia has no injury data
                status="fit",
                key_player=True,    # all squad members are potential starters
                p_plays=1.0,
            ))

        if players:
            break  # found and parsed the main squad table

    return players


# ---------------------------------------------------------------------------
# Transfermarkt scraper (Playwright — needed for JS-rendered injury icons)
# ---------------------------------------------------------------------------

def fetch_transfermarkt_squad(
    team: str,
    match_date: pd.Timestamp,
    force: bool = False,
) -> list[PlayerStatus]:
    """
    Scrapes Transfermarkt kader page (JS-rendered) for a national team.
    Finds span.verletzt-table (injuries) and span.gesperrt-table (suspensions).
    Returns list of unavailable PlayerStatus. Caches 24h.
    Returns [] on any failure → caller falls back to default_report.
    """
    if team not in _TM_TEAMS:
        return []

    cache_file = _cache_path(team)
    if not force and _cache_fresh(cache_file):
        return _load_cache(cache_file)

    slug, team_id = _TM_TEAMS[team]
    # Try current year first (WM 2026 season), then previous year as fallback.
    # National team pages use different saison_id conventions than clubs.
    for saison in (str(match_date.year), str(match_date.year - 1)):
        url = f"https://www.transfermarkt.com/{slug}/kader/verein/{team_id}/saison_id/{saison}"
        players = _scrape_kader_playwright(url, match_date)
        if players:
            break

    if players:
        _save_cache(cache_file, players)
    else:
        print(f"  [squad] {team}: 0 players returned (TM blocked or page structure changed) — not caching")
    return players


def _scrape_kader_playwright(url: str, match_date: pd.Timestamp) -> list[PlayerStatus]:
    """
    Uses Playwright to load the full kader page.
    Extracts ALL squad members (fit + unavailable) so availability_score is accurate.
    Injury/suspension status detected via span.verletzt-table / span.gesperrt-table.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [squad] Playwright not installed — run: pip3 install playwright && "
              "python3 -m playwright install chromium")
        return []

    players = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=_UA,
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "DNT": "1",
                },
            )
            page = ctx.new_page()
            # Remove navigator.webdriver flag that TM uses to detect headless bots
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(2000)

            # Select main player rows: those with the jersey-number td (rueckennummer).
            # This td only appears in the outer player row, not in inline-table sub-rows,
            # avoiding duplicates from the nested inline-table structure.
            rows = page.query_selector_all("table.items tbody tr:has(td.rueckennummer)")
            if not rows:
                # Fallback: some TM national team pages use a different layout.
                # Wait longer for lazy-loaded content and try a broader selector.
                page.wait_for_timeout(2000)
                rows = page.query_selector_all("table.items tbody tr:has(td.rueckennummer)")
            if not rows:
                # Second fallback: try without :has() — filter by hauptlink presence.
                rows = [r for r in page.query_selector_all("table.items tbody tr")
                        if r.query_selector("td.hauptlink a")]
            for row in rows:
                name_a = row.query_selector("td.hauptlink a")
                if not name_a:
                    continue

                # Extract text nodes only (excludes span nbsp; text)
                name = name_a.evaluate(
                    "el => Array.from(el.childNodes)"
                    "      .filter(n => n.nodeType === 3)"
                    "      .map(n => n.textContent.trim())"
                    "      .join('')"
                ).strip()
                if not name:
                    continue

                # Position: jersey number td has title="Goalkeeper" / "Centre-Back" etc.
                pos_td = row.query_selector("td.zentriert.rueckennummer[title]")
                pos_raw = pos_td.get_attribute("title") if pos_td else ""
                position = _map_position(pos_raw or "")

                # Status from embedded injury/suspension spans
                status = "fit"
                availability = 1.0

                verletzt = row.query_selector("span.verletzt-table")
                gesperrt = row.query_selector("span.gesperrt-table")

                if verletzt:
                    title_text = verletzt.get_attribute("title") or ""
                    until_date = _parse_return_date(title_text, match_date)
                    if until_date is None or until_date >= match_date:
                        status = "injured"
                        availability = 0.0
                elif gesperrt:
                    status = "suspended"
                    availability = 0.0

                # Market value: TM kader page has a right-aligned hauptlink cell
                # at the end of each row with values like "€10.00m" / "€500k".
                mv_eur_m = 0.0
                mv_td = row.query_selector("td.rechts.hauptlink") or row.query_selector("td.rechts:last-child")
                if mv_td:
                    mv_eur_m = _parse_market_value_m(mv_td.inner_text() or "")

                players.append(PlayerStatus(
                    name=name,
                    position=position,
                    availability=availability,
                    status=status,
                    key_player=True,  # marked after full squad is loaded
                    market_value_eur_m=mv_eur_m,
                ))

            browser.close()
    except Exception as exc:
        print(f"  [squad] Playwright scrape failed: {exc}")

    return players


def _parse_return_date(title: str, match_date: pd.Timestamp) -> pd.Timestamp | None:
    """
    Extracts return date from Transfermarkt span title.
    Format: 'Ankle injury - Return expected on 20/07/2026'
    Returns None when unknown (= treat as still unavailable).
    """
    m = _RETURN_RE.search(title)
    if not m:
        return None
    try:
        return pd.to_datetime(m.group(1), dayfirst=True)
    except Exception:
        return None


def _map_position(text: str) -> str:
    lower = text.lower()
    for key, mapped in _POS_MAP.items():
        if key in lower:
            return mapped
    return "unknown"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(team: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = team.lower().replace(" ", "_").replace("/", "_")
    return _CACHE_DIR / f"{safe}.json"


def _cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    if age_hours >= _CACHE_TTL_HOURS:
        return False
    # Treat empty cache (TM was blocked) as stale so we retry next call.
    try:
        data = json.loads(path.read_text())
        return len(data) > 0
    except Exception:
        return False


def _save_cache(path: Path, players: list[PlayerStatus]) -> None:
    data = [
        {
            "name": p.name,
            "position": p.position,
            "availability": p.availability,
            "status": p.status,
            "key_player": p.key_player,
            "p_plays": p.p_plays,
            "market_value_eur_m": p.market_value_eur_m,
        }
        for p in players
    ]
    path.write_text(json.dumps(data, ensure_ascii=False))


def _load_cache(path: Path) -> list[PlayerStatus]:
    try:
        data = json.loads(path.read_text())
        return [
            PlayerStatus(
                name=d["name"],
                position=d.get("position", "unknown"),
                availability=d.get("availability", 1.0),
                status=d.get("status", "fit"),
                key_player=d.get("key_player", False),
                p_plays=d.get("p_plays", 1.0),
                market_value_eur_m=d.get("market_value_eur_m", 0.0),
            )
            for d in data
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# FBref stub (future integration)
# ---------------------------------------------------------------------------

def fetch_fbref_player_form(
    team: str,
    before_date: pd.Timestamp,
    n_matches: int = 8,
) -> dict[str, float]:
    """
    STUB: Fetch player-level xG/xA from FBref.
    Returns empty dict until implemented (soccerdata package required).
    """
    return {}

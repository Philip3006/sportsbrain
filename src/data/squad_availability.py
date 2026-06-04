"""
Squad availability layer with live Transfermarkt injury scraping.

Data flow:
  fetch_transfermarkt_squad(team, match_date)
    → scrapes transfermarkt.com/kader page (Playwright, JS-rendered)
    → finds span.verletzt-table (injuries) + span.gesperrt-table (suspensions)
    → caches to data/cache/squad/{team}.json (24h TTL)
    → returns list[PlayerStatus] (unavailable players only)

  squad_report(team, match_date) → SquadReport
    → calls fetch_transfermarkt_squad, falls back to default_report on error

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
    "Romania":        ("rumanien", "3392"),
    "Slovenia":       ("slowenien", "3558"),
    "Georgia":        ("georgien", "3560"),
    # CAF
    "Morocco":        ("marokko", "3898"),
    "Senegal":        ("senegal", "3664"),
    "Nigeria":        ("nigeria", "3667"),
    "Egypt":          ("agypten", "3668"),
    "Ivory Coast":    ("elfenbeinskuste", "3655"),
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
}

_CACHE_DIR = DATA_CACHE / "squad"
_CACHE_TTL_HOURS = 24

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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


@dataclass
class SquadReport:
    team: str
    report_date: pd.Timestamp
    players: list[PlayerStatus] = field(default_factory=list)
    data_source: str = "default"

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


def squad_report(
    team: str,
    match_date: pd.Timestamp,
    force: bool = False,
) -> SquadReport:
    """
    Returns SquadReport for team at match_date.
    Tries Transfermarkt first; falls back to default_report on any error.
    """
    players = fetch_transfermarkt_squad(team, match_date, force=force)
    if not players:
        return default_report(team, match_date)
    return SquadReport(
        team=team,
        report_date=match_date,
        players=players,
        data_source="transfermarkt",
    )


def squad_impact_features(
    home_report: SquadReport,
    away_report: SquadReport,
) -> dict[str, float]:
    """Numeric features for the model from two SquadReports."""
    return {
        "squad_availability_home": home_report.availability_score,
        "squad_availability_away": away_report.availability_score,
        "squad_availability_diff": (
            home_report.availability_score - away_report.availability_score
        ),
        "key_player_risk_home": float(len(home_report.risk_players)),
        "key_player_risk_away": float(len(away_report.risk_players)),
    }


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
    # saison_id: current season start year (e.g., for Jun 2026 → 2025)
    saison = str(match_date.year - 1)
    url = f"https://www.transfermarkt.com/{slug}/kader/verein/{team_id}/saison_id/{saison}"

    players = _scrape_kader_playwright(url, match_date)
    _save_cache(cache_file, players)
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
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_UA, locale="en-US")
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(1500)

            # Select main player rows: those with the jersey-number td (rueckennummer).
            # This td only appears in the outer player row, not in inline-table sub-rows,
            # avoiding duplicates from the nested inline-table structure.
            rows = page.query_selector_all("table.items tbody tr:has(td.rueckennummer)")
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

                players.append(PlayerStatus(
                    name=name,
                    position=position,
                    availability=availability,
                    status=status,
                    key_player=False,
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
        return pd.Timestamp(m.group(1), dayfirst=True)
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
    return age_hours < _CACHE_TTL_HOURS


def _save_cache(path: Path, players: list[PlayerStatus]) -> None:
    data = [
        {
            "name": p.name,
            "position": p.position,
            "availability": p.availability,
            "status": p.status,
            "key_player": p.key_player,
            "p_plays": p.p_plays,
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

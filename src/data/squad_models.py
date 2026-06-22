"""
Shared data models, constants, and cache helpers for the squad availability system.
All other squad_* modules import from here; nothing here imports from squad_*.
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


def default_report(team: str, match_date: pd.Timestamp) -> SquadReport:
    """Fully-fit placeholder. data_source='default' signals no real data."""
    return SquadReport(team=team, report_date=match_date, players=[], data_source="default")


# ---------------------------------------------------------------------------
# Cache helpers (shared by TM and Wikipedia scrapers)
# ---------------------------------------------------------------------------

def _cache_path(team: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = team.lower().replace(" ", "_").replace("/", "_")
    return _CACHE_DIR / f"{safe}.json"


def _wiki_cache_path(team: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = team.lower().replace(" ", "_").replace("/", "_").replace("'", "")
    return _CACHE_DIR / f"{safe}_wiki.json"


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

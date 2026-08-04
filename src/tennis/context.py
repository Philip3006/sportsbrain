"""J8-I9: Kontextfeatures für Tennis-Matches.

Bringt drei zusätzliche Signale ins Feature-Set:
  1. rest_days  — Tage seit letztem Match je Spieler (Fatigue)
  2. altitude_m — Höhenlage des Venue (Höhen-Boost, siehe Bogotá/Kitzbühel)
  3. tz_shift_h — Zeitzonen-Delta gegen Heim-Region (Reise-Fatigue)

Die eigentliche Feature-Integration passiert in `src/tennis/features.py` als
optional-Parameter; hier nur die Lookup-Funktionen + statisches Venue-Dict.

Keine Zeile ohne Datenquelle. Aktuell: Venue-Metadata als statisches Dict —
Datenerhebung aus TheOddsAPI-`sport_key` bzw. Tournament-Registry.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class VenueMeta:
    """Statisches Venue-Metadatum. Fehlwerte → Fallback in features.py."""
    altitude_m: int
    tz_utc_offset_h: int
    country: str


# Tournament-Slug → VenueMeta. Nur Turniere mit relevanten Deltas
# (Höhe > 1000m ODER TZ-Delta ≥ 6h). Rest fällt auf Default 0/0.
VENUE_META: dict[str, VenueMeta] = {
    # Höhenlage
    "bogota":           VenueMeta(altitude_m=2640, tz_utc_offset_h=-5, country="CO"),
    "quito":            VenueMeta(altitude_m=2850, tz_utc_offset_h=-5, country="EC"),
    "kitzbuhel":        VenueMeta(altitude_m=760,  tz_utc_offset_h=+1, country="AT"),
    "gstaad":           VenueMeta(altitude_m=1050, tz_utc_offset_h=+1, country="CH"),
    # Reise-relevant (asiatische Slams)
    "australian_open":  VenueMeta(altitude_m=10,   tz_utc_offset_h=+11, country="AU"),
    "us_open":          VenueMeta(altitude_m=10,   tz_utc_offset_h=-5,  country="US"),
    "beijing":          VenueMeta(altitude_m=44,   tz_utc_offset_h=+8,  country="CN"),
    "shanghai":         VenueMeta(altitude_m=4,    tz_utc_offset_h=+8,  country="CN"),
    "tokyo":            VenueMeta(altitude_m=40,   tz_utc_offset_h=+9,  country="JP"),
    "acapulco":         VenueMeta(altitude_m=8,    tz_utc_offset_h=-6,  country="MX"),
    "rio":              VenueMeta(altitude_m=2,    tz_utc_offset_h=-3,  country="BR"),
}


def lookup_venue(tournament_slug: str) -> Optional[VenueMeta]:
    """Case-insensitive Substring-Lookup (z.B. 'atp_bogota_open' → 'bogota')."""
    if not tournament_slug:
        return None
    key = tournament_slug.lower()
    for slug, meta in VENUE_META.items():
        if slug in key:
            return meta
    return None


def rest_days_between(prev_match_date: str | None, current_match_date: str | None) -> float:
    """Tage zwischen zwei Match-Daten (Format YYYY-MM-DD oder YYYYMMDD).

    Return: float in [0, 30] (>30 wird gecapped). None-Input → 7 (Prior).
    """
    if not prev_match_date or not current_match_date:
        return 7.0
    def _parse(d: str) -> datetime | None:
        d = d.strip()
        for fmt, ln in (("%Y-%m-%d", 10), ("%Y%m%d", 8), ("%Y-%m-%dT%H:%M:%S", 19)):
            try:
                return datetime.strptime(d[:ln], fmt)
            except Exception:
                continue
        return None
    a = _parse(prev_match_date)
    b = _parse(current_match_date)
    if a is None or b is None:
        return 7.0
    diff = (b - a).days
    return float(max(0, min(30, diff)))


def altitude_factor(meta: VenueMeta | None) -> float:
    """Höhen-Boost normalisiert auf 0..1 (Kalibrierung folgt in I10-Recal)."""
    if meta is None:
        return 0.0
    if meta.altitude_m < 1000:
        return 0.0
    # Bogotá 2640m → 1.0; Kitzbühel 760m → 0.0; linear dazwischen
    return min(1.0, (meta.altitude_m - 1000) / 1640.0)


# IOC 3-letter code → typical home UTC offset (standard time, no DST).
# Covers top-100 ATP/WTA nationalities. Default 0 for unlisted.
IOC_HOME_TZ: dict[str, int] = {
    # Europe CET (+1)
    "SRB": 1, "ESP": 1, "GER": 1, "FRA": 1, "ITA": 1, "AUT": 1, "SUI": 1,
    "NED": 1, "BEL": 1, "POL": 1, "CZE": 1, "SVK": 1, "HUN": 1, "HRV": 1,
    "SLO": 1, "DEN": 1, "NOR": 1, "SWE": 1, "MON": 1, "LUX": 1, "MNE": 1,
    # Europe EET (+2)
    "FIN": 2, "GRE": 2, "ROU": 2, "BUL": 2, "UKR": 2, "MDA": 2, "LAT": 2,
    "LTU": 2, "EST": 2, "CYP": 2, "RSA": 2, "EGY": 2,
    # Europe / Middle East (+3)
    "RUS": 3, "TUR": 3, "BLR": 3, "ISR": 2, "SAU": 3, "UAE": 4,
    # UK / Portugal / Morocco
    "GBR": 0, "IRL": 0, "POR": 0, "MAR": 0,
    # Americas East (-5)
    "USA": -5, "CAN": -5, "COL": -5, "PER": -5, "ECU": -5,
    # Americas other
    "MEX": -6, "CHI": -4, "VEN": -4, "PAR": -4, "URU": -3, "BRA": -3,
    "ARG": -3, "BOL": -4,
    # Asia / Pacific
    "JPN": 9, "KOR": 9, "CHN": 8, "TPE": 8, "HKG": 8, "THA": 7,
    "AUS": 10, "NZL": 12, "IND": 6, "KAZ": 6, "UZB": 5, "GEO": 4,
    # Other
    "TUN": 1, "ALG": 1, "NIG": 1,
}


def player_home_tz(ioc: str) -> int:
    """UTC offset (hours) for a player's home country by IOC code."""
    return IOC_HOME_TZ.get((ioc or "").upper(), 0)


def tz_travel_penalty(home_offset_h: int, venue_meta: VenueMeta | None) -> float:
    """Absolutes TZ-Delta gegen die Heim-Region (max Jet-Lag ≈ 12h)."""
    if venue_meta is None:
        return 0.0
    delta = abs(home_offset_h - venue_meta.tz_utc_offset_h)
    return min(12.0, delta) / 12.0

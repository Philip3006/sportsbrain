"""Tennis-Tournament-Registry (Roadmap J2).

Statische Whitelist aller relevanten ATP/WTA-Events ab Kategorie 250 aufwärts.
Discovery (src.tennis.discovery) gleicht diese gegen TheOddsAPI /sports ab und
markiert, welche Events aktuell live sind (active=true im API-Response).

Kategorien:
  - grand_slam: 4× (Aus Open, French, Wimbledon, US Open) — BO5 (ATP) / BO3 (WTA)
  - m1000:      ATP Masters 1000 (9 Events)
  - wta1000:    WTA 1000 (5 Events)
  - atp500:     ATP 500 (~7 Events)
  - wta500:     WTA 500 (~5 Events)
  - atp250:     ATP 250 (~30+ Events)
  - wta250:     WTA 250 (~20+ Events)
  - tour_final: ATP Finals + WTA Finals (Saison-Ende)

Min-Edge pro Kategorie aus src.config.TENNIS_MIN_EDGE_BY_CATEGORY.

Sport-Keys: TheOddsAPI rotiert Keys teilweise (z.B. tennis_atp_aus_open_singles
vs tennis_atp_australian_open). Discovery prüft live-keys; bei Drift wird die
Registry hier nachgezogen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Tournament:
    """Meta-Eintrag für ein Tour-Event."""

    slug: str                  # interne stabile ID, z.B. "wimbledon"
    name: str                  # Anzeigename, z.B. "Wimbledon"
    tour: str                  # "atp" | "wta"
    category: str              # "grand_slam" | "m1000" | "wta1000" | "atp500" | "wta500" | "atp250" | "wta250" | "tour_final"
    surface: str               # "grass" | "clay" | "hard" | "carpet"
    best_of: int               # 5 für ATP-Slams, 3 sonst
    sport_keys: tuple[str, ...] = field(default_factory=tuple)
    # Erwartetes Monatsfenster für UI/Discovery-Hinweise (1-12). None = ganzjährig möglich.
    typical_months: tuple[int, ...] = field(default_factory=tuple)

    @property
    def is_grand_slam(self) -> bool:
        return self.category == "grand_slam"


# ---------------------------------------------------------------------------
# Static registry — pragmatisch kuratiert.
# Sport-Keys orientieren sich an TheOddsAPI-Konvention (`tennis_<tour>_<event>`).
# Discovery verifiziert + erweitert dynamisch.
# ---------------------------------------------------------------------------

TENNIS_REGISTRY: tuple[Tournament, ...] = (
    # ----- Grand Slams -----
    Tournament("aus_open_atp",   "Australian Open",     "atp", "grand_slam", "hard",  5,
               ("tennis_atp_aus_open_singles", "tennis_atp_australian_open"), (1,)),
    Tournament("aus_open_wta",   "Australian Open",     "wta", "grand_slam", "hard",  3,
               ("tennis_wta_aus_open_singles", "tennis_wta_australian_open"), (1,)),
    Tournament("french_open_atp","French Open",         "atp", "grand_slam", "clay",  5,
               ("tennis_atp_french_open",), (5, 6)),
    Tournament("french_open_wta","French Open",         "wta", "grand_slam", "clay",  3,
               ("tennis_wta_french_open",), (5, 6)),
    Tournament("wimbledon_atp",  "Wimbledon",           "atp", "grand_slam", "grass", 5,
               ("tennis_atp_wimbledon",), (6, 7)),
    Tournament("wimbledon_wta",  "Wimbledon",           "wta", "grand_slam", "grass", 3,
               ("tennis_wta_wimbledon",), (6, 7)),
    Tournament("us_open_atp",    "US Open",             "atp", "grand_slam", "hard",  5,
               ("tennis_atp_us_open",), (8, 9)),
    Tournament("us_open_wta",    "US Open",             "wta", "grand_slam", "hard",  3,
               ("tennis_wta_us_open",), (8, 9)),

    # ----- ATP Masters 1000 (9 Events) -----
    Tournament("indian_wells_atp",   "Indian Wells",     "atp", "m1000",   "hard",  3,
               ("tennis_atp_indian_wells",), (3,)),
    Tournament("miami_open_atp",     "Miami Open",       "atp", "m1000",   "hard",  3,
               ("tennis_atp_miami_open",), (3, 4)),
    Tournament("monte_carlo_atp",    "Monte Carlo",      "atp", "m1000",   "clay",  3,
               ("tennis_atp_monte_carlo",), (4,)),
    Tournament("madrid_open_atp",    "Madrid Open",      "atp", "m1000",   "clay",  3,
               ("tennis_atp_madrid_open",), (4, 5)),
    Tournament("italian_open_atp",   "Italian Open",     "atp", "m1000",   "clay",  3,
               ("tennis_atp_italian_open", "tennis_atp_rome"), (5,)),
    Tournament("canadian_open_atp",  "Canadian Open",    "atp", "m1000",   "hard",  3,
               ("tennis_atp_canadian_open",), (7, 8)),
    Tournament("cincinnati_atp",     "Cincinnati Open",  "atp", "m1000",   "hard",  3,
               ("tennis_atp_cincinnati_open", "tennis_atp_cincinnati"), (8,)),
    Tournament("shanghai_atp",       "Shanghai Masters", "atp", "m1000",   "hard",  3,
               ("tennis_atp_shanghai",), (10,)),
    Tournament("paris_masters_atp",  "Paris Masters",    "atp", "m1000",   "hard",  3,
               ("tennis_atp_paris_masters",), (10, 11)),

    # ----- WTA 1000 (5 mandatory + 2 optional) -----
    Tournament("indian_wells_wta",   "Indian Wells",     "wta", "wta1000", "hard",  3,
               ("tennis_wta_indian_wells",), (3,)),
    Tournament("miami_open_wta",     "Miami Open",       "wta", "wta1000", "hard",  3,
               ("tennis_wta_miami_open",), (3, 4)),
    Tournament("madrid_open_wta",    "Madrid Open",      "wta", "wta1000", "clay",  3,
               ("tennis_wta_madrid_open",), (4, 5)),
    Tournament("italian_open_wta",   "Italian Open",     "wta", "wta1000", "clay",  3,
               ("tennis_wta_italian_open", "tennis_wta_rome"), (5,)),
    Tournament("canadian_open_wta",  "Canadian Open",    "wta", "wta1000", "hard",  3,
               ("tennis_wta_canadian_open",), (7, 8)),
    Tournament("cincinnati_wta",     "Cincinnati Open",  "wta", "wta1000", "hard",  3,
               ("tennis_wta_cincinnati_open", "tennis_wta_cincinnati"), (8,)),
    Tournament("china_open_wta",     "China Open",       "wta", "wta1000", "hard",  3,
               ("tennis_wta_china_open", "tennis_wta_beijing"), (9, 10)),
    Tournament("wuhan_open_wta",     "Wuhan Open",       "wta", "wta1000", "hard",  3,
               ("tennis_wta_wuhan",), (10,)),

    # ----- ATP 500 (Auswahl der Haupt-Events) -----
    Tournament("rotterdam_atp",      "Rotterdam Open",   "atp", "atp500",  "hard",  3,
               ("tennis_atp_rotterdam",), (2,)),
    Tournament("rio_open_atp",       "Rio Open",         "atp", "atp500",  "clay",  3,
               ("tennis_atp_rio_open",), (2,)),
    Tournament("dubai_atp",          "Dubai Championships","atp", "atp500", "hard", 3,
               ("tennis_atp_dubai",), (2,)),
    Tournament("acapulco_atp",       "Mexican Open",     "atp", "atp500",  "hard",  3,
               ("tennis_atp_acapulco", "tennis_atp_mexican_open"), (2, 3)),
    Tournament("barcelona_atp",      "Barcelona Open",   "atp", "atp500",  "clay",  3,
               ("tennis_atp_barcelona",), (4,)),
    Tournament("queens_atp",         "Queen's Club",     "atp", "atp500",  "grass", 3,
               ("tennis_atp_queens",), (6,)),
    Tournament("halle_atp",          "Halle Open",       "atp", "atp500",  "grass", 3,
               ("tennis_atp_halle",), (6,)),
    Tournament("hamburg_atp",        "Hamburg European Open","atp","atp500","clay", 3,
               ("tennis_atp_hamburg",), (7,)),
    Tournament("washington_atp",     "Washington Open",  "atp", "atp500",  "hard",  3,
               ("tennis_atp_washington",), (7, 8)),
    Tournament("beijing_atp",        "China Open",       "atp", "atp500",  "hard",  3,
               ("tennis_atp_china_open", "tennis_atp_beijing"), (9, 10)),
    Tournament("tokyo_atp",          "Japan Open",       "atp", "atp500",  "hard",  3,
               ("tennis_atp_tokyo", "tennis_atp_japan_open"), (9, 10)),
    Tournament("basel_atp",          "Swiss Indoors",    "atp", "atp500",  "hard",  3,
               ("tennis_atp_basel",), (10,)),
    Tournament("vienna_atp",         "Erste Bank Open",  "atp", "atp500",  "hard",  3,
               ("tennis_atp_vienna",), (10,)),

    # ----- WTA 500 (Auswahl) -----
    Tournament("doha_wta",           "Qatar Open",       "wta", "wta500",  "hard",  3,
               ("tennis_wta_doha", "tennis_wta_qatar_open"), (2,)),
    Tournament("dubai_wta",          "Dubai Championships","wta","wta500", "hard",  3,
               ("tennis_wta_dubai",), (2,)),
    Tournament("stuttgart_wta",      "Stuttgart Open",   "wta", "wta500",  "clay",  3,
               ("tennis_wta_stuttgart",), (4,)),
    Tournament("charleston_wta",     "Charleston Open",  "wta", "wta500",  "clay",  3,
               ("tennis_wta_charleston",), (4,)),
    Tournament("berlin_wta",         "Berlin Open",      "wta", "wta500",  "grass", 3,
               ("tennis_wta_berlin",), (6,)),
    Tournament("eastbourne_wta",     "Eastbourne Intl.", "wta", "wta500",  "grass", 3,
               ("tennis_wta_eastbourne",), (6,)),
    Tournament("tokyo_wta",          "Toray Pan Pacific","wta", "wta500",  "hard",  3,
               ("tennis_wta_tokyo",), (9, 10)),

    # ----- ATP 250 (gängige Beispiele — Liste nicht-exhaustiv; Discovery ergänzt) -----
    Tournament("adelaide_atp",       "Adelaide Intl.",   "atp", "atp250",  "hard",  3,
               ("tennis_atp_adelaide",), (1,)),
    Tournament("auckland_atp",       "Auckland Open",    "atp", "atp250",  "hard",  3,
               ("tennis_atp_auckland",), (1,)),
    Tournament("marseille_atp",      "Marseille Open",   "atp", "atp250",  "hard",  3,
               ("tennis_atp_marseille",), (2,)),
    Tournament("buenos_aires_atp",   "Argentina Open",   "atp", "atp250",  "clay",  3,
               ("tennis_atp_buenos_aires",), (2,)),
    Tournament("estoril_atp",        "Estoril Open",     "atp", "atp250",  "clay",  3,
               ("tennis_atp_estoril",), (4,)),
    Tournament("munich_atp",         "BMW Open Munich",  "atp", "atp250",  "clay",  3,
               ("tennis_atp_munich",), (4,)),
    Tournament("geneva_atp",         "Geneva Open",      "atp", "atp250",  "clay",  3,
               ("tennis_atp_geneva",), (5,)),
    Tournament("lyon_atp",           "Lyon Open",        "atp", "atp250",  "clay",  3,
               ("tennis_atp_lyon",), (5,)),
    Tournament("stuttgart_atp",      "Stuttgart Open",   "atp", "atp250",  "grass", 3,
               ("tennis_atp_stuttgart",), (6,)),
    Tournament("mallorca_atp",       "Mallorca Champ.",  "atp", "atp250",  "grass", 3,
               ("tennis_atp_mallorca",), (6,)),
    Tournament("newport_atp",        "Newport Hall of Fame","atp","atp250","grass", 3,
               ("tennis_atp_newport",), (7,)),
    Tournament("kitzbuhel_atp",      "Kitzbühel Open",   "atp", "atp250",  "clay",  3,
               ("tennis_atp_kitzbuhel",), (7,)),
    Tournament("winston_salem_atp",  "Winston-Salem Open","atp","atp250",  "hard",  3,
               ("tennis_atp_winston_salem",), (8,)),
    Tournament("metz_atp",           "Moselle Open",     "atp", "atp250",  "hard",  3,
               ("tennis_atp_metz",), (9,)),
    Tournament("astana_atp",         "Astana Open",      "atp", "atp250",  "hard",  3,
               ("tennis_atp_astana",), (9, 10)),
    Tournament("stockholm_atp",      "Stockholm Open",   "atp", "atp250",  "hard",  3,
               ("tennis_atp_stockholm",), (10,)),
    Tournament("antwerp_atp",        "European Open",    "atp", "atp250",  "hard",  3,
               ("tennis_atp_antwerp",), (10,)),

    # ----- WTA 250 (Auswahl) -----
    Tournament("adelaide_wta",       "Adelaide Intl.",   "wta", "wta250",  "hard",  3,
               ("tennis_wta_adelaide",), (1,)),
    Tournament("hobart_wta",         "Hobart Intl.",     "wta", "wta250",  "hard",  3,
               ("tennis_wta_hobart",), (1,)),
    Tournament("merida_wta",         "Merida Open",      "wta", "wta250",  "hard",  3,
               ("tennis_wta_merida",), (2, 3)),
    Tournament("bogota_wta",         "Copa Colsanitas",  "wta", "wta250",  "clay",  3,
               ("tennis_wta_bogota",), (4,)),
    Tournament("rabat_wta",          "Grand Prix Rabat", "wta", "wta250",  "clay",  3,
               ("tennis_wta_rabat",), (5,)),
    Tournament("nottingham_wta",     "Nottingham Open",  "wta", "wta250",  "grass", 3,
               ("tennis_wta_nottingham",), (6,)),
    Tournament("bad_homburg_wta",    "Bad Homburg Open", "wta", "wta250",  "grass", 3,
               ("tennis_wta_bad_homburg",), (6,)),
    Tournament("prague_wta",         "Prague Open",      "wta", "wta250",  "clay",  3,
               ("tennis_wta_prague",), (7,)),
    Tournament("cleveland_wta",      "Cleveland Champ.", "wta", "wta250",  "hard",  3,
               ("tennis_wta_cleveland",), (8,)),
    Tournament("guadalajara_wta",    "Guadalajara Open", "wta", "wta250",  "hard",  3,
               ("tennis_wta_guadalajara",), (9,)),
    Tournament("seoul_wta",          "Korea Open",       "wta", "wta250",  "hard",  3,
               ("tennis_wta_seoul",), (9,)),
    Tournament("ningbo_wta",         "Ningbo Open",      "wta", "wta250",  "hard",  3,
               ("tennis_wta_ningbo",), (10,)),

    # ----- Tour Finals -----
    Tournament("atp_finals",         "ATP Finals",       "atp", "tour_final","hard", 3,
               ("tennis_atp_finals",), (11,)),
    Tournament("wta_finals",         "WTA Finals",       "wta", "tour_final","hard", 3,
               ("tennis_wta_finals",), (11,)),
)


# ---------------------------------------------------------------------------
# Lookup-Indizes (zur Laufzeit gebaut, immutable)
# ---------------------------------------------------------------------------

_SPORT_KEY_INDEX: dict[str, Tournament] = {
    key: t for t in TENNIS_REGISTRY for key in t.sport_keys
}

_SLUG_INDEX: dict[str, Tournament] = {t.slug: t for t in TENNIS_REGISTRY}


def get_tournament(slug_or_sport_key: str) -> Tournament | None:
    """Lookup per Slug ODER TheOddsAPI sport_key. None wenn unbekannt."""
    if slug_or_sport_key in _SLUG_INDEX:
        return _SLUG_INDEX[slug_or_sport_key]
    return _SPORT_KEY_INDEX.get(slug_or_sport_key)


def tournaments_by_category(category: str) -> list[Tournament]:
    """Alle Turniere einer Kategorie."""
    return [t for t in TENNIS_REGISTRY if t.category == category]


def tournaments_for_month(month: int, tour: str | None = None) -> list[Tournament]:
    """Erwartete Turniere für einen Monat. Hilfreich für Discovery-Hinweise."""
    out = []
    for t in TENNIS_REGISTRY:
        if t.typical_months and month not in t.typical_months:
            continue
        if tour and t.tour != tour.lower():
            continue
        out.append(t)
    return out


def all_sport_keys() -> set[str]:
    """Alle bekannten TheOddsAPI sport_keys (für Discovery-Filter)."""
    return set(_SPORT_KEY_INDEX.keys())


def unknown_sport_key(sport_key: str) -> Tournament:
    """Stub-Tournament für unbekannte sport_keys (Discovery-Fallback).

    Vermeidet Crash wenn TheOddsAPI ein neues tennis_*-Event listet, das (noch)
    nicht in der Registry steht. Defaults sind konservativ: Surface unknown,
    Kategorie 'atp250' (höchste min_edge), BO3.
    """
    tour = "wta" if "_wta_" in sport_key else "atp"
    category = "wta250" if tour == "wta" else "atp250"
    return Tournament(
        slug=sport_key,
        name=sport_key.replace("tennis_", "").replace("_", " ").title(),
        tour=tour,
        category=category,
        surface="unknown",
        best_of=3,
        sport_keys=(sport_key,),
        typical_months=(),
    )

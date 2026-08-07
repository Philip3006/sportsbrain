"""
Durchschnittlicher Heimzuschauerschnitt pro Verein.
Quellen: weltfussball.de / sport.de (25/26-Basis für 26/27); ältere BL2-Saisons
aus eigener Recherche.
Wird als LGBM-Feature home_attendance_ratio genutzt (Heimteam / Liga-Ø).

Keys müssen exakt mit den Teamnamen im Trainingsdaten-Pickle übereinstimmen.
Für Teams mit abweichenden Schreibweisen zwischen CSV und Scanner
(z.B. "Holstein Kiel" vs "Kiel") werden beide Varianten eingetragen.
"""
from __future__ import annotations

# Durchschnitt pro Heimspiel — alle Werte in ganzen Zuschauern
HOME_ATTENDANCE: dict[str, float] = {
    # ── 2. Bundesliga 2026/27 (aktuelle Staffel) ──────────────────────────
    # Quellen: weltfussball.de BL2 25/26 + sport.de BL1 25/26 (Absteiger)
    "Hertha":           48_293.0,
    "Kaiserslautern":   46_731.0,
    "Hannover":         42_794.0,
    "Nurnberg":         35_310.0,
    "Dresden":          31_031.0,
    "Karlsruhe":        30_430.0,
    "St Pauli":         29_505.0,
    "Bielefeld":        26_108.0,
    "Bochum":           25_574.0,
    "Magdeburg":        25_084.0,
    "Wolfsburg":        24_716.0,   # Absteiger aus BL1 25/26
    "Braunschweig":     21_024.0,
    "Darmstadt":        17_640.0,
    "Heidenheim":       14_824.0,   # Absteiger aus BL1 25/26
    # Holstein Kiel: beide Schreibweisen (Trainingsdaten vs. Scanner-Alias)
    "Holstein Kiel":    14_188.0,   # exakter Name im Trainingspickle
    "Kiel":             14_188.0,   # canonical_name("Holstein Kiel") → "Kiel"
    "Greuther Furth":   12_680.0,
    # Osnabrück: beide Schreibweisen
    "Osnabruck":        11_500.0,   # exakter Name im Trainingspickle
    "Osnabrück":        11_500.0,   # Scanner-seitige Schreibweise
    "Cottbus":          10_800.0,   # Aufsteiger aus 3.Liga, Schätzung

    # ── Historische BL2-Teams (Trainingsdaten 21/22–25/26) ────────────────
    "Hamburg":          53_000.0,   # Volksparkstadion ~57k, BL2-Schnitt ~53k
    "Schalke 04":       61_000.0,   # Veltins-Arena, BL2-Saison 21/22
    "FC Koln":          49_500.0,   # RheinEnergieStadion, BL2-Saison 21/22
    "Fortuna Dusseldorf": 44_000.0, # Merkur Spiel-Arena, BL2-Phasen 19/20–22/23
    "Werder Bremen":    40_000.0,   # wohninvest Weserstadion, BL2 20/21
    "Stuttgart":        53_000.0,   # Mercedes-Benz Arena, BL2 18/19
    "Union Berlin":     22_000.0,   # Altes Försterei, BL2 bis 18/19
    "Paderborn":        14_600.0,   # Benteler-Arena ~15k
    "Hansa Rostock":    24_000.0,   # Ostseestadion ~29k, Schnitt ~24k
    "Elversberg":        8_000.0,   # Schüco Arena ~7,5k, ausverkauft
    "Erzgebirge Aue":   11_500.0,   # Erzgebirgsstadion ~15k, Schnitt ~11k
    "Duisburg":         14_000.0,   # MSV-Arena ~31k, Schnitt ~13-15k
    "Ingolstadt":       11_500.0,   # Audi Sportpark ~15k
    "Munich 1860":      15_000.0,   # Grünwalder Stadion ~15,5k
    "Preußen Münster":  13_500.0,   # Preußenstadion ~15k
    "Regensburg":       12_000.0,   # Continental Arena ~15k
    "Sandhausen":        8_500.0,   # BWT-Stadion ~15,8k, Schnitt ~8-9k
    "Ulm":              17_000.0,   # Donaustadion ~17,4k, häufig ausverkauft
    "Wehen":             7_500.0,   # BRITA-Arena ~13k, Schnitt ~7-8k
    "Wurzburger Kickers": 8_500.0,  # Flyeralarm Arena ~13k
}

# Liga-Durchschnitt 2. Bundesliga (Saison 25/26 als Basis)
_BL2_AVG_ATTENDANCE = 26_600.0

_FALLBACK = _BL2_AVG_ATTENDANCE


def get_attendance_ratio(home: str, away: str | None = None) -> float:
    """
    Gibt home_attendance / liga_avg zurück.
    Werte > 1.0 = überdurchschnittliche Heimkulisse → stärkerer Heimvorteil.
    Ratio 1.0 als Fallback wenn Team unbekannt.
    """
    home_att = HOME_ATTENDANCE.get(home, _FALLBACK)
    return float(home_att / _BL2_AVG_ATTENDANCE)


def get_attendance_log_ratio(home: str, away: str | None = None) -> float:
    import math
    return float(math.log(max(get_attendance_ratio(home), 0.1)))

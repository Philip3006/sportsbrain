"""
Durchschnittlicher Heimzuschauerschnitt pro Verein.
Quelle: weltfussball.de / sport.de (Saison 2025/26 als Basis für 2026/27).
Wird als LGBM-Feature home_attendance_ratio genutzt (Heimteam / Liga-Ø).
"""
from __future__ import annotations

# Durchschnitt pro Heimspiel in der Saison 2025/26 (Basis für 26/27)
# Quellen: weltfussball.de (BL2 25/26), sport.de (BL1 25/26 für abgestiegene Teams)
HOME_ATTENDANCE: dict[str, float] = {
    # 2. Bundesliga 2025/26 — Hinrunde-Schnitt (weltfussball.de)
    "Hertha":         48_293.0,
    "Kaiserslautern": 46_731.0,
    "Hannover":       42_794.0,
    "Nurnberg":       35_310.0,
    "Dresden":        31_031.0,
    "Karlsruhe":      30_430.0,
    "Bielefeld":      26_108.0,
    "Bochum":         25_574.0,
    "Magdeburg":      25_084.0,
    "Braunschweig":   21_024.0,
    "Darmstadt":      17_640.0,
    "Kiel":           14_188.0,
    "Greuther Furth": 12_680.0,
    # Aus 1. Bundesliga abgestiegen (sport.de, BL1 25/26 Hinrunde-Schnitt)
    "St Pauli":       29_505.0,
    "Wolfsburg":      24_716.0,
    "Heidenheim":     14_824.0,
    # Aus 3. Liga aufgestiegen (Schätzung basierend auf Stadionkapazität/Historie)
    "Osnabrück":      11_500.0,  # Bremer Brücke ~16k Kapazität, typisch ~11-13k
    "Cottbus":        10_800.0,  # Stadion der Freundschaft ~22k, typisch ~9-13k
}

# Liga-Durchschnitt 2. Bundesliga (Saison 25/26)
_BL2_AVG_ATTENDANCE = 26_600.0  # ~26.6k Schnitt über alle BL2-Teams 25/26

_FALLBACK = _BL2_AVG_ATTENDANCE


def get_attendance_ratio(home: str, away: str | None = None) -> float:
    """
    Gibt home_attendance / liga_avg zurück.
    Werte > 1.0 = überdurchschnittliche Heimkulisse → stärkerer Heimvorteil.
    """
    home_att = HOME_ATTENDANCE.get(home, _FALLBACK)
    return float(home_att / _BL2_AVG_ATTENDANCE)


def get_attendance_log_ratio(home: str, away: str | None = None) -> float:
    import math
    return float(math.log(max(get_attendance_ratio(home), 0.1)))

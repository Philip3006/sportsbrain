"""
Squad market value lookup (Transfermarkt, approximate June 2026 estimates, M EUR).
Used to compute market_value_ratio (home/away) as a LightGBM feature.

Rationale: Marktwert-Verhältnis ist ein besserer Stärkeindikator als Elo bei
Mannschaften mit wenig WM-Erfahrung (z.B. Außenseiter aus AFC/CAF).

Source: Transfermarkt squad market values, rounded. Update annually.
"""
from __future__ import annotations

# Squad market values in millions EUR (Transfermarkt, June 2026)
# Source: transfermarkt.com via sportingpedia.com/givemesport.com, cross-referenced June 2026
SQUAD_VALUES_M: dict[str, float] = {
    # UEFA — top tier
    "England":         1345.0,
    "France":          1195.0,
    "Portugal":        1000.0,
    "Germany":          775.0,
    "Spain":            861.0,
    "Netherlands":      671.7,
    "Belgium":          549.4,
    "Italy":            520.0,
    "Turkey":           494.2,
    "Ivory Coast":      530.9,
    "Cote d'Ivoire":    530.9,
    "Denmark":          346.7,
    "Croatia":          325.5,
    "Switzerland":      281.7,
    "Norway":           601.0,
    "Austria":          220.0,
    "Serbia":           291.7,
    "Poland":           253.65,
    "Ukraine":          175.0,
    "Scotland":         160.0,
    "Hungary":           90.0,
    "Slovakia":          80.0,
    "Albania":           70.0,
    "Czech Republic":   160.0,
    "Czechia":          160.0,
    "Romania":           90.0,
    "Slovenia":          55.0,
    "Georgia":           65.0,
    # CONMEBOL
    "Brazil":          1135.0,
    "Argentina":        820.7,
    "Colombia":         300.0,
    "Uruguay":          423.95,
    "Ecuador":          236.35,
    "Venezuela":        120.0,
    "Paraguay":         100.0,
    "Chile":             85.0,
    "Peru":              70.0,
    "Bolivia":           40.0,
    # CONCACAF
    "United States":    270.2,
    "Mexico":           164.6,
    "Canada":           185.0,
    "Panama":            55.0,
    "Costa Rica":        11.6,
    "Honduras":          35.0,
    "Jamaica":           40.0,
    "El Salvador":       25.0,
    "Guatemala":         20.0,
    # CAF
    "Morocco":          318.1,
    "Nigeria":          240.0,
    "Senegal":          211.8,
    "Egypt":            130.0,
    "Algeria":          257.6,
    "South Africa":      70.0,
    "Tunisia":           54.1,
    "Ghana":            242.2,
    "Cameroon":         175.7,
    "Mali":              80.0,
    "DR Congo":          50.0,
    "Cape Verde":        30.0,
    # AFC
    "Japan":            284.75,
    "South Korea":      184.3,
    "Saudi Arabia":      14.5,
    "Iran":              51.4,
    "Australia":         41.25,
    "Indonesia":         40.0,
    "Uzbekistan":        50.0,
    "Qatar":             14.3,
    "Iraq":              25.0,
    "Jordan":            15.0,
    # OFC
    "New Zealand":       20.0,
    "New Caledonia":      5.0,
    # Additional WM 2026 qualifiers
    "Bosnia and Herzegovina": 120.0,
    "Haiti":             18.0,
    "Curacao":            8.0,
    "Curaçao":            8.0,
    "Scotland":         160.0,
}

_GLOBAL_MEDIAN = 100.0  # fallback for unknown teams


def get_market_value_ratio(home: str, away: str) -> float:
    """
    Returns home_value / away_value.
    Values > 1.0 mean home team has higher squad value.
    Uses global median for unknown teams to avoid extreme ratios.
    """
    h_val = SQUAD_VALUES_M.get(home, _GLOBAL_MEDIAN)
    a_val = SQUAD_VALUES_M.get(away, _GLOBAL_MEDIAN)
    return float(h_val / max(a_val, 1.0))


def get_market_value_log_ratio(home: str, away: str) -> float:
    """Log ratio — more appropriate for LightGBM (symmetric around 0)."""
    import math
    ratio = get_market_value_ratio(home, away)
    return float(math.log(max(ratio, 0.01)))

"""
Squad market value lookup (Transfermarkt, approximate June 2026 estimates, M EUR).
Used to compute market_value_ratio (home/away) as a LightGBM feature.

Rationale: Marktwert-Verhältnis ist ein besserer Stärkeindikator als Elo bei
Mannschaften mit wenig WM-Erfahrung (z.B. Außenseiter aus AFC/CAF).

Source: Transfermarkt squad market values, rounded. Update annually.
"""
from __future__ import annotations

# Squad market values in millions EUR (approximate June 2026)
SQUAD_VALUES_M: dict[str, float] = {
    # UEFA — top tier
    "England":         1500.0,
    "France":          1380.0,
    "Portugal":        1100.0,
    "Germany":         1050.0,
    "Spain":            980.0,
    "Netherlands":      820.0,
    "Belgium":          680.0,
    "Italy":            600.0,
    "Denmark":          420.0,
    "Croatia":          390.0,
    "Switzerland":      310.0,
    "Austria":          260.0,
    "Turkey":           255.0,
    "Serbia":           210.0,
    "Poland":           200.0,
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
    "Brazil":           900.0,
    "Argentina":        800.0,
    "Colombia":         300.0,
    "Uruguay":          320.0,
    "Ecuador":          145.0,
    "Venezuela":        120.0,
    "Paraguay":         100.0,
    "Chile":             85.0,
    "Peru":              70.0,
    "Bolivia":           40.0,
    # CONCACAF
    "United States":    260.0,
    "Mexico":           230.0,
    "Canada":           190.0,
    "Panama":            55.0,
    "Costa Rica":        45.0,
    "Honduras":          35.0,
    "Jamaica":           40.0,
    "El Salvador":       25.0,
    "Guatemala":         20.0,
    # CAF
    "Morocco":          280.0,
    "Nigeria":          240.0,
    "Senegal":          250.0,
    "Egypt":            130.0,
    "Ivory Coast":      200.0,
    "Cote d'Ivoire":    200.0,
    "South Africa":      70.0,
    "Algeria":           80.0,
    "Tunisia":           70.0,
    "Ghana":             85.0,
    "Cameroon":          90.0,
    "Mali":              80.0,
    "DR Congo":          50.0,
    # AFC
    "Japan":            220.0,
    "South Korea":      180.0,
    "Saudi Arabia":      90.0,
    "Iran":              60.0,
    "Australia":         85.0,
    "Indonesia":         40.0,
    "Uzbekistan":        50.0,
    "Qatar":             35.0,
    # OFC
    "New Zealand":       20.0,
    "New Caledonia":      5.0,
}

_GLOBAL_MEDIAN = 120.0  # fallback for unknown teams


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

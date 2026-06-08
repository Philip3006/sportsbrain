from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_CACHE = ROOT / "data" / "cache"
MODELS_DIR = ROOT / "models"
RESULTS_DIR = ROOT / "results"

INTL_CSV_URL = (
    "https://raw.githubusercontent.com/martj42/"
    "international_results/master/results.csv"
)
FBDATA_BASE = "https://www.football-data.co.uk/mmz4281"
ODDS_API_URL = "https://api.the-odds-api.com/v4"

DC_PHI = 0.0065
KELLY_FRAC = 0.25
MIN_EDGE = 0.03
MIN_STAKE_EUR = 5.0
MAX_STAKE_EUR = 15.0
MAX_ACTIVE_BETS = 5
MAX_EV = 0.40           # signals with EV > 40% are almost always model artifacts

# Confederation per team — used for asymmetric divergence threshold.
# Non-UEFA/CONMEBOL away teams have higher confederation-bias risk in DC model
# (training data dominated by qualifier blowouts). Stricter filter applied.
TEAM_CONFEDERATION: dict[str, str] = {
    # UEFA
    "Germany": "UEFA", "France": "UEFA", "Spain": "UEFA", "Portugal": "UEFA",
    "England": "UEFA", "Netherlands": "UEFA", "Belgium": "UEFA", "Italy": "UEFA",
    "Croatia": "UEFA", "Austria": "UEFA", "Switzerland": "UEFA", "Denmark": "UEFA",
    "Poland": "UEFA", "Serbia": "UEFA", "Ukraine": "UEFA", "Turkey": "UEFA",
    "Scotland": "UEFA", "Hungary": "UEFA", "Slovakia": "UEFA", "Albania": "UEFA",
    "Czech Republic": "UEFA", "Czechia": "UEFA", "Romania": "UEFA", "Slovenia": "UEFA",
    "Georgia": "UEFA",
    # CONMEBOL
    "Argentina": "CONMEBOL", "Brazil": "CONMEBOL", "Uruguay": "CONMEBOL",
    "Colombia": "CONMEBOL", "Ecuador": "CONMEBOL", "Venezuela": "CONMEBOL",
    "Paraguay": "CONMEBOL", "Bolivia": "CONMEBOL", "Chile": "CONMEBOL", "Peru": "CONMEBOL",
    # CONCACAF
    "United States": "CONCACAF", "Mexico": "CONCACAF", "Canada": "CONCACAF",
    "Panama": "CONCACAF", "Costa Rica": "CONCACAF", "Honduras": "CONCACAF",
    "Jamaica": "CONCACAF", "El Salvador": "CONCACAF", "Guatemala": "CONCACAF",
    # CAF
    "Morocco": "CAF", "Senegal": "CAF", "Nigeria": "CAF", "Egypt": "CAF",
    "Ivory Coast": "CAF", "Cote d'Ivoire": "CAF", "South Africa": "CAF",
    "Algeria": "CAF", "Tunisia": "CAF", "Ghana": "CAF", "Cameroon": "CAF",
    "Mali": "CAF", "DR Congo": "CAF",
    # AFC
    "Japan": "AFC", "South Korea": "AFC", "Saudi Arabia": "AFC", "Iran": "AFC",
    "Australia": "AFC", "Indonesia": "AFC", "New Zealand": "AFC", "Uzbekistan": "AFC",
    "Qatar": "AFC",
    # OFC
    "New Caledonia": "OFC",
    # WM 2026 qualifiers — not in original list
    "Bosnia and Herzegovina": "UEFA", "Sweden": "UEFA", "Norway": "UEFA",
    "Haiti": "CONCACAF", "Curacao": "CONCACAF",
    "Cape Verde": "CAF", "South Africa": "CAF",
    "Iraq": "AFC", "Jordan": "AFC",
}

COMPETITIVE_TOURNAMENTS = {
    "FIFA World Cup",
    "UEFA Euro",
    "Copa América",
    "Copa America",
    "African Cup of Nations",
    "AFC Asian Cup",
    "UEFA Nations League",
    "CONCACAF Nations League",
    "CONCACAF Gold Cup",
    "FIFA World Cup qualification",
    "UEFA Euro qualification",
    "Copa América qualification",
    "CONMEBOL World Cup qualification",
    "AFC Asian Cup qualification",
    "CAF World Cup qualification",
    "CONCACAF World Cup qualification",
    "OFC World Cup qualification",
    "CONCACAF Nations League qualification",
}

ELO_K_BASE = 40.0

# Multiplier on ELO_K_BASE per tournament — finals count more than qualifiers.
TOURNAMENT_K_FACTORS: dict[str, float] = {
    "FIFA World Cup":                        1.30,
    "UEFA Euro":                             1.20,
    "Copa América":                          1.15,
    "Copa America":                          1.15,
    "African Cup of Nations":                1.00,
    "AFC Asian Cup":                         0.95,
    "CONCACAF Gold Cup":                     0.90,
    "UEFA Nations League":                   1.00,
    "CONCACAF Nations League":               0.80,
    "FIFA World Cup qualification":          0.50,
    "UEFA Euro qualification":               0.55,
    "Copa América qualification":            0.50,
    "CONMEBOL World Cup qualification":      0.50,
    "AFC Asian Cup qualification":           0.45,
    "CAF World Cup qualification":           0.40,
    "CONCACAF World Cup qualification":      0.45,
    "OFC World Cup qualification":           0.30,
    "CONCACAF Nations League qualification": 0.45,
}

# Tournament quality weights for DC NLL — final tournaments > group/NL > qualifiers.
# Qualifiers against weak opponents (e.g. OFC, CAF) inflate attack params without
# reflecting true ability at WC/EURO level. Down-weighting them reduces confederation bias.
TOURNAMENT_WEIGHTS: dict[str, float] = {
    # Finals: full weight — real head-to-head at balanced competition level
    "FIFA World Cup": 1.0,
    "UEFA Euro": 1.0,
    "Copa América": 1.0,
    "Copa America": 1.0,
    "African Cup of Nations": 0.90,
    "AFC Asian Cup": 0.85,
    "CONCACAF Gold Cup": 0.80,
    # Nations Leagues: strong competitive signal, peer-group matches
    "UEFA Nations League": 0.90,
    "CONCACAF Nations League": 0.75,
    # Qualifiers: heavily down-weighted — blowout wins vs minnows inflate attack params.
    # Teams like Japan (14-0 vs Bangladesh) and NZ (12-0 vs Tonga) get reduced influence.
    "FIFA World Cup qualification": 0.30,
    "UEFA Euro qualification": 0.35,
    "Copa América qualification": 0.30,
    "CONMEBOL World Cup qualification": 0.30,
    "AFC Asian Cup qualification": 0.25,
    "CAF World Cup qualification": 0.25,
    "CONCACAF World Cup qualification": 0.28,
    "OFC World Cup qualification": 0.20,
    "CONCACAF Nations League qualification": 0.28,
}

# Canonical name map: raw name -> canonical name
# Covers mismatches between martj42 and football-data.co.uk sources
TEAM_NAME_MAP: dict[str, str] = {
    "USA": "United States",
    "US": "United States",
    "Ivory Coast": "Cote d'Ivoire",
    "Korea Republic": "South Korea",
    "Korea DPR": "North Korea",
    "IR Iran": "Iran",
    "Kyrgyz Republic": "Kyrgyzstan",
    "Cabo Verde": "Cape Verde",
    "St. Kitts and Nevis": "Saint Kitts and Nevis",
    "St. Lucia": "Saint Lucia",
    "St. Vincent / Grenadines": "Saint Vincent and the Grenadines",
    "Trinidad & Tobago": "Trinidad and Tobago",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Czech Republic": "Czechia",
    "Republic of Ireland": "Ireland",
    "Northern Ireland": "Northern Ireland",
    "FYR Macedonia": "North Macedonia",
    "Macedonia": "North Macedonia",
    "Slovak Republic": "Slovakia",
    # TheOddsAPI-specific aliases
    "DR Congo": "DR Congo",  # DC uses "DR Congo" directly — no change needed
    "Cote d'Ivoire": "Cote d'Ivoire",
    # Accented/variant spellings from TheOddsAPI for WM 2026 qualifiers
    "Curaçao": "Curacao",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Côte d'Ivoire": "Cote d'Ivoire",
}


# WM 2026 Group assignments (12 groups, 4 teams each)
# Verified against official FIFA draw held 2025-12-05, Washington D.C.
# Source: https://en.wikipedia.org/wiki/2026_FIFA_World_Cup_draw
WM2026_GROUPS: dict[str, str] = {
    # Group A
    "Mexico": "A", "South Africa": "A", "South Korea": "A", "Czechia": "A",
    # Group B
    "Canada": "B", "Bosnia and Herzegovina": "B", "Qatar": "B", "Switzerland": "B",
    # Group C
    "Brazil": "C", "Morocco": "C", "Haiti": "C", "Scotland": "C",
    # Group D
    "United States": "D", "Paraguay": "D", "Australia": "D", "Turkey": "D",
    # Group E
    "Germany": "E", "Curacao": "E", "Cote d'Ivoire": "E", "Ecuador": "E",
    # Group F
    "Netherlands": "F", "Japan": "F", "Sweden": "F", "Tunisia": "F",
    # Group G
    "Belgium": "G", "Egypt": "G", "Iran": "G", "New Zealand": "G",
    # Group H
    "Spain": "H", "Cape Verde": "H", "Saudi Arabia": "H", "Uruguay": "H",
    # Group I
    "France": "I", "Senegal": "I", "Iraq": "I", "Norway": "I",
    # Group J
    "Argentina": "J", "Algeria": "J", "Austria": "J", "Jordan": "J",
    # Group K
    "Portugal": "K", "DR Congo": "K", "Uzbekistan": "K", "Colombia": "K",
    # Group L
    "England": "L", "Croatia": "L", "Ghana": "L", "Panama": "L",
}


def canonical_name(name: str) -> str:
    return TEAM_NAME_MAP.get(name, name)

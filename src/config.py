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
MAX_STAKE_PCT = 0.02
MAX_ACTIVE_BETS = 3
MAX_EV = 0.40           # signals with EV > 40% are almost always model artifacts

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
}


def canonical_name(name: str) -> str:
    return TEAM_NAME_MAP.get(name, name)

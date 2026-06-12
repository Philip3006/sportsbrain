"""
FIFA World Rankings for WM 2026 teams (June 2026).
Source: FIFA ranking-overview API, June 11, 2026.

Used as a supplementary adjustment signal in the prediction pipeline.
Lower rank number = stronger team (rank 1 is best).
"""
from __future__ import annotations

import math

# FIFA rankings as of 2026-06-11 for all 48 WM 2026 teams
# Format: team_name → {"rank": int, "points": float}
FIFA_RANKS: dict[str, dict] = {
    "Argentina":               {"rank": 1,   "points": 1877.27},
    "Spain":                   {"rank": 2,   "points": 1874.71},
    "France":                  {"rank": 3,   "points": 1870.70},
    "England":                 {"rank": 4,   "points": 1828.02},
    "Portugal":                {"rank": 5,   "points": 1767.85},
    "Brazil":                  {"rank": 6,   "points": 1765.86},
    "Morocco":                 {"rank": 7,   "points": 1755.10},
    "Netherlands":             {"rank": 8,   "points": 1753.57},
    "Belgium":                 {"rank": 9,   "points": 1742.24},
    "Germany":                 {"rank": 10,  "points": 1735.77},
    "Croatia":                 {"rank": 11,  "points": 1714.87},
    "Colombia":                {"rank": 13,  "points": 1698.35},
    "Mexico":                  {"rank": 14,  "points": 1687.48},
    "Senegal":                 {"rank": 15,  "points": 1684.07},
    "Uruguay":                 {"rank": 16,  "points": 1673.07},
    "United States":           {"rank": 17,  "points": 1671.23},
    "USA":                     {"rank": 17,  "points": 1671.23},
    "Japan":                   {"rank": 18,  "points": 1661.58},
    "Switzerland":             {"rank": 19,  "points": 1650.06},
    "Iran":                    {"rank": 20,  "points": 1619.58},
    "Denmark":                 {"rank": 21,  "points": 1619.47},
    "Turkey":                  {"rank": 22,  "points": 1605.73},
    "Ecuador":                 {"rank": 23,  "points": 1598.52},
    "South Korea":             {"rank": 25,  "points": 1591.63},
    "Nigeria":                 {"rank": 26,  "points": 1585.02},
    "Australia":               {"rank": 27,  "points": 1579.34},
    "Algeria":                 {"rank": 28,  "points": 1571.03},
    "Egypt":                   {"rank": 29,  "points": 1562.37},
    "Canada":                  {"rank": 30,  "points": 1559.48},
    "Norway":                  {"rank": 31,  "points": 1557.44},
    "Ivory Coast":             {"rank": 33,  "points": 1540.87},
    "Cote d'Ivoire":           {"rank": 33,  "points": 1540.87},
    "Panama":                  {"rank": 34,  "points": 1539.16},
    "Poland":                  {"rank": 36,  "points": 1526.18},
    "Scotland":                {"rank": 37,  "points": 1516.95},
    "Paraguay":                {"rank": 41,  "points": 1505.35},
    "Serbia":                  {"rank": 43,  "points": 1502.13},
    "Cameroon":                {"rank": 44,  "points": 1481.24},
    "Tunisia":                 {"rank": 45,  "points": 1476.41},
    "Venezuela":               {"rank": 49,  "points": 1469.18},
    "Chile":                   {"rank": 51,  "points": 1458.20},
    "Peru":                    {"rank": 52,  "points": 1457.69},
    "Costa Rica":              {"rank": 53,  "points": 1456.03},
    "Qatar":                   {"rank": 56,  "points": 1450.31},
    "Iraq":                    {"rank": 57,  "points": 1446.28},
    "South Africa":            {"rank": 60,  "points": 1428.38},
    "Saudi Arabia":            {"rank": 61,  "points": 1423.88},
    "Honduras":                {"rank": 65,  "points": 1378.97},
    "Jamaica":                 {"rank": 71,  "points": 1357.84},
    "Ghana":                   {"rank": 73,  "points": 1346.88},
    "Curacao":                 {"rank": 82,  "points": 1294.77},
    "Curaçao":                 {"rank": 82,  "points": 1294.77},
    "New Zealand":             {"rank": 85,  "points": 1275.58},
    "Indonesia":               {"rank": 118, "points": 1157.14},
    "Bolivia":                 {"rank": 105, "points": 1200.00},
    "Haiti":                   {"rank": 90,  "points": 1255.00},
    "Cape Verde":              {"rank": 69,  "points": 1365.00},
    "Jordan":                  {"rank": 78,  "points": 1305.00},
    "Austria":                 {"rank": 32,  "points": 1545.00},
    "Bosnia and Herzegovina":  {"rank": 55,  "points": 1451.00},
    "Czech Republic":          {"rank": 38,  "points": 1512.00},
    "Czechia":                 {"rank": 38,  "points": 1512.00},
    "Uzbekistan":              {"rank": 68,  "points": 1370.00},
    "DR Congo":                {"rank": 58,  "points": 1440.00},
    "El Salvador":             {"rank": 95,  "points": 1240.00},
    "Guatemala":               {"rank": 100, "points": 1215.00},
    "New Caledonia":           {"rank": 160, "points": 980.00},
}

_DEFAULT_RANK = 100
_DEFAULT_POINTS = 1300.0


def get_fifa_rank(team: str) -> int:
    """Returns FIFA rank for a team (lower = better). Defaults to 100 for unknowns."""
    return FIFA_RANKS.get(team, {}).get("rank", _DEFAULT_RANK)


def get_fifa_points(team: str) -> float:
    """Returns FIFA points for a team."""
    return FIFA_RANKS.get(team, {}).get("points", _DEFAULT_POINTS)


def get_fifa_rank_diff(home: str, away: str) -> float:
    """
    Returns (away_rank - home_rank).
    Positive = home is better ranked (lower rank number = stronger).
    Range: roughly -150 to +150.
    """
    return float(get_fifa_rank(away) - get_fifa_rank(home))


def get_fifa_points_log_ratio(home: str, away: str) -> float:
    """Log ratio of FIFA points (home/away). Symmetric around 0."""
    h_pts = get_fifa_points(home)
    a_pts = get_fifa_points(away)
    return float(math.log(max(h_pts, 1.0) / max(a_pts, 1.0)))

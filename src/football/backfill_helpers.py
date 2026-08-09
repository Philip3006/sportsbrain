"""ESPN score fetcher + name normalisation for BL2 football backfill."""
from __future__ import annotations

from datetime import date, timedelta

import requests

BL2_ESPN_LEAGUE = "ger.2"
GHOST_AGE_DAYS = 21  # weekly fixtures — allow 3 weeks before ghosting

# Teams confirmed in BL2 2026/27 (from ESPN Spieltag 1 data).
# Any signal team NOT in this set is a ghost candidate.
BL2_2627_TEAMS: frozenset[str] = frozenset({
    "Bochum", "Hertha", "Bielefeld", "Karlsruhe", "Magdeburg", "Braunschweig",
    "Darmstadt", "Kiel", "Wolfsburg", "Nurnberg", "Dresden", "Cottbus",
    "Hannover", "St. Pauli", "Greuther Furth", "Heidenheim", "Osnabruck",
    "Kaiserslautern",
})

# Keyword → normalised signal-name (lowercase match, first hit wins)
BL2_NAME_ALIASES: dict[str, str] = {
    "bochum":          "Bochum",
    "hertha":          "Hertha",
    "bielefeld":       "Bielefeld",
    "karlsruhe":       "Karlsruhe",
    "magdeburg":       "Magdeburg",
    "braunschweig":    "Braunschweig",
    "darmstadt":       "Darmstadt",
    "kiel":            "Kiel",
    "wolfsburg":       "Wolfsburg",
    "nürnberg":   "Nurnberg",
    "nurnberg":        "Nurnberg",
    "dresden":         "Dresden",
    "cottbus":         "Cottbus",
    "hannover":        "Hannover",
    "pauli":           "St. Pauli",
    "greuther":        "Greuther Furth",
    "fürth":      "Greuther Furth",
    "heidenheim":      "Heidenheim",
    "osnabrück":  "Osnabruck",
    "osnabruck":       "Osnabruck",
    "kaiserslautern":  "Kaiserslautern",
    # ghost-only teams (not in BL2 2026/27)
    "paderborn":       "Paderborn",
    "elversberg":      "Elversberg",
    "schalke":         "Schalke 04",
    "münster":    "Preußen Münster",
    "munster":         "Preußen Münster",
    "düsseldorf": "Fortuna Dusseldorf",
    "dusseldorf":      "Fortuna Dusseldorf",
}


def espn_to_signal_name(espn_name: str) -> str:
    lower = espn_name.lower()
    for keyword, sig_name in BL2_NAME_ALIASES.items():
        if keyword in lower:
            return sig_name
    return espn_name


def is_ghost_bl2(home: str, away: str) -> bool:
    """True if either team is NOT a confirmed BL2 2026/27 participant."""
    return home not in BL2_2627_TEAMS or away not in BL2_2627_TEAMS


def fetch_bl2_scores_espn(date_str: str) -> dict:
    """Fetch completed BL2 matches from ESPN for a given date (YYYYMMDD).

    Returns a dict keyed under both ESPN display names and normalised signal
    names so that the existing lookup ``scores.get(f'{home} vs {away}')``
    works without further changes.
    """
    url = (
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/"
        f"{BL2_ESPN_LEAGUE}/scoreboard?dates={date_str}"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    result: dict = {}
    for event in data.get("events", []):
        comps = event.get("competitions", [{}])[0]
        competitors = comps.get("competitors", [])
        status = comps.get("status", {}).get("type", {}).get("description", "")
        if len(competitors) != 2 or status not in ("Full Time", "Final", "FT"):
            continue
        home_espn = competitors[0]["team"]["displayName"]
        away_espn = competitors[1]["team"]["displayName"]
        h_score = int(competitors[0].get("score") or 0)
        a_score = int(competitors[1].get("score") or 0)
        home_sig = espn_to_signal_name(home_espn)
        away_sig = espn_to_signal_name(away_espn)
        entry = {
            "home_score": h_score,
            "away_score": a_score,
            "home": home_sig,
            "away": away_sig,
            "date": date_str,
            "source": "espn_bl2",
        }
        result[f"{home_espn} vs {away_espn}"] = entry
        result[f"{home_sig} vs {away_sig}"] = entry
        # Also store reversed so mismatched home/away still hits
        result[f"{away_sig} vs {home_sig}"] = {**entry, "home_score": a_score, "away_score": h_score,
                                                "home": away_sig, "away": home_sig}
    return result


def fetch_bl2_window(
    scan_dates_raw: list[str],
    existing: dict,
    window_days: int = 7,
) -> tuple[dict, int]:
    """Fetch BL2 ESPN scores for scan_dates + window_days forward."""
    fetch_dates: set[str] = set()
    today = date.today()
    for sd in scan_dates_raw:
        try:
            d = date.fromisoformat(sd)
            for offset in range(window_days + 1):
                candidate = d + timedelta(days=offset)
                if candidate <= today:
                    fetch_dates.add(candidate.strftime("%Y%m%d"))
        except ValueError:
            pass

    scores = dict(existing)
    for date_str in sorted(fetch_dates):
        daily = fetch_bl2_scores_espn(date_str)
        for k, v in daily.items():
            if k not in scores:
                scores[k] = v
    return scores, len(fetch_dates)

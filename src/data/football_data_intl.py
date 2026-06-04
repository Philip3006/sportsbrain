"""
Downloads historical World Cup odds from football-data.co.uk Excel files.
Provides complete match odds (all 64 matches per tournament) including group stage.

Available:  WorldCup2022.xlsx — contains WC2022, WC2018, WC2014 sheets
            WorldCup2026.xlsx — WC2026 qualifiers (live update)

Odds columns used (in priority order):
  WC2022: bet365-H/D/A  → H-Max/D-Max/A-Max
  WC2018: Pinny-H/D/A   → H-Max/D-Max/A-Max
"""
import io
from pathlib import Path

import pandas as pd
import requests

from src.config import DATA_RAW, canonical_name

_XLSX_URL = "https://www.football-data.co.uk/WorldCup2022.xlsx"
_SHEET_MAP = {
    "WC2022": "WorldCup2022",
    "WC2018": "WorldCup2018",
}

# Preferred odds column per sheet (home, draw, away)
_ODDS_COLS = {
    "WC2022": [("bet365-H", "bet365-D", "bet365-A"), ("H-Max", "D-Max", "A-Max")],
    "WC2018": [("Pinny-H",  "Pinny-D",  "Pinny-A"),  ("H-Max", "D-Max", "A-Max")],
}


def fetch_wc_odds(force: bool = False) -> pd.DataFrame:
    """
    Downloads and parses World Cup odds from football-data.co.uk.
    Returns combined DataFrame for WC2018 + WC2022 in odds_lookup format:
      tournament, match_id, home_team, away_team, home_odds, draw_odds, away_odds, bookmaker
    Caches to data/raw/wc_odds_fduk.csv.
    """
    cache = DATA_RAW / "wc_odds_fduk.csv"
    if not force and cache.exists():
        return pd.read_csv(cache)

    print("  Downloading WorldCup2022.xlsx from football-data.co.uk ...")
    r = requests.get(_XLSX_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()

    xl = pd.ExcelFile(io.BytesIO(r.content))
    frames = []

    for tournament, sheet in _SHEET_MAP.items():
        if sheet not in xl.sheet_names:
            print(f"  Sheet '{sheet}' not found — skipping {tournament}")
            continue

        df = xl.parse(sheet)
        df = df.dropna(subset=["Home", "Away", "HGFT", "AGFT"])

        # Pick best available odds columns
        h_col = d_col = a_col = bm_label = None
        for h, d, a in _ODDS_COLS.get(tournament, [("H-Max", "D-Max", "A-Max")]):
            if all(c in df.columns for c in (h, d, a)):
                h_col, d_col, a_col = h, d, a
                bm_label = h.split("-")[0]
                break

        if h_col is None:
            print(f"  No odds columns found for {tournament} — skipping")
            continue

        rows = []
        for _, row in df.iterrows():
            home = canonical_name(str(row["Home"]).strip())
            away = canonical_name(str(row["Away"]).strip())
            try:
                h_odds = float(row[h_col])
                d_odds = float(row[d_col])
                a_odds = float(row[a_col])
            except (ValueError, TypeError):
                continue

            if any(o <= 1.0 for o in (h_odds, d_odds, a_odds)):
                continue

            # Closing line proxy: H-Max/D-Max/A-Max = best available odds at close
            close_h = close_d = close_a = None
            if all(c in df.columns for c in ("H-Max", "D-Max", "A-Max")):
                try:
                    ch = float(row["H-Max"])
                    cd = float(row["D-Max"])
                    ca = float(row["A-Max"])
                    if all(o > 1.0 for o in (ch, cd, ca)):
                        close_h, close_d, close_a = ch, cd, ca
                except (ValueError, TypeError):
                    pass

            rows.append({
                "tournament":   tournament,
                "match_id":     f"{tournament}_{home}_vs_{away}",
                "home_team":    home,
                "away_team":    away,
                "home_odds":    h_odds,
                "draw_odds":    d_odds,
                "away_odds":    a_odds,
                "bookmaker":    bm_label,
                "close_home":   close_h,
                "close_draw":   close_d,
                "close_away":   close_a,
            })

        t_df = pd.DataFrame(rows)
        print(f"  {tournament}: {len(t_df)} matches parsed from sheet '{sheet}'")
        frames.append(t_df)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    combined.to_csv(cache, index=False)
    return combined

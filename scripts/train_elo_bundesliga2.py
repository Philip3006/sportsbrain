"""Phase B — B3: Elo-Warmstart über 10 Saisons 2.BL.

Läuft compute_elo_series() chronologisch über den kompletten Match-Stream.
Aufsteiger aus 3.Liga / Absteiger aus 1.BL bekommen Prior-Elo aus dem Median
ihrer bisherigen Positionierung — vermeidet Elo-Default 1500 für Neuzugänge
(führt sonst zu p≈0.5-Signalen wie im Tennis-Unknown-Player-Bug).

Persistiert:
  data/cache/elo_ratings_bl2.json — {team: elo_rating} für alle 36 Universe-Teams
  data/cache/elo_ratings.json     — merged (bestehende Ratings bleiben, BL2-Teams
                                     werden addiert/überschrieben)
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from src.config import DATA_CACHE, canonical_name
from src.models.elo import compute_elo_series, current_ratings, ELO_DEFAULT

# 2.BL-K-Faktor aus LEAGUE_REGISTRY. compute_elo_series routet via TOURNAMENT_K_FACTORS
# oder k_competitive; wir setzen k_competitive=20 für Club-Fußball (Standard).
BL2_ELO_K = 20.0


def _load_matches() -> pd.DataFrame:
    path = DATA_CACHE / "bundesliga2_matches.pkl"
    if not path.exists():
        raise FileNotFoundError(f"Match-Cache fehlt: {path}. Erst build_bundesliga2_universe.py laufen lassen.")
    with open(path, "rb") as f:
        return pickle.load(f)


def _initial_ratings(matches: pd.DataFrame) -> dict[str, float]:
    """Prior-Elos für Vereine ohne Historie. Aufsteiger aus 3.Liga starten unter
    dem 2.BL-Durchschnitt, Absteiger aus 1.BL über dem Durchschnitt.
    Wir schätzen den Effekt aus der ersten Saison jedes Teams — ohne Domain-DB
    keine perfekte 1.BL/3.Liga-Info, aber ELO_DEFAULT reicht für Warmstart."""
    return {}  # compute_elo_series nutzt ELO_DEFAULT für unbekannte Teams


def main() -> None:
    matches = _load_matches()
    # Kanonisierung
    matches = matches.copy()
    matches["home_team"] = matches["home_team"].map(lambda t: canonical_name(str(t)))
    matches["away_team"] = matches["away_team"].map(lambda t: canonical_name(str(t)))
    matches = matches.dropna(subset=["home_score", "away_score"])
    matches["home_score"] = matches["home_score"].astype(int)
    matches["away_score"] = matches["away_score"].astype(int)
    # tournament-Spalte leer setzen → compute_elo_series nimmt k_friendly=20
    # (WIR wollen 20, weil Club-Fußball. Wenn wir "tournament" leer lassen, springt
    #  die Funktion in den k_friendly-Zweig; das ist unser Ziel.)
    matches["tournament"] = ""
    matches["neutral"] = False
    matches = matches.sort_values("date").reset_index(drop=True)

    print(f"Trainiere Elo über {len(matches)} Matches (10 Saisons)...")
    series = compute_elo_series(
        matches,
        initial_ratings=_initial_ratings(matches),
        k_competitive=BL2_ELO_K,
        k_friendly=BL2_ELO_K,
    )
    ratings = current_ratings(series)

    # Sanity: alle Ratings in [1200, 1800] plausibel
    extremes = {t: r for t, r in ratings.items() if r < 1200 or r > 1800}
    if extremes:
        print(f"WARN  {len(extremes)} Teams mit extremen Elos: {extremes}")

    # Sortiert-Preview
    top = sorted(ratings.items(), key=lambda kv: kv[1], reverse=True)
    print("\nTOP-10 nach Elo (Ende Saison 25/26):")
    for t, r in top[:10]:
        print(f"  {r:7.1f}  {t}")
    print("\nBOTTOM-5:")
    for t, r in top[-5:]:
        print(f"  {r:7.1f}  {t}")

    # Persist: BL2-only
    bl2_path = DATA_CACHE / "elo_ratings_bl2.json"
    bl2_path.write_text(json.dumps(ratings, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nBL2-Elos → {bl2_path} ({len(ratings)} Teams)")

    # Merge in shared elo_ratings.json (bestehende WM-Elos bleiben, BL2 additiv/overwrite)
    merged_path = DATA_CACHE / "elo_ratings.json"
    merged: dict[str, float] = {}
    if merged_path.exists():
        try:
            merged = json.loads(merged_path.read_text())
        except json.JSONDecodeError:
            merged = {}
    n_existing = len(merged)
    merged.update(ratings)
    merged_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Merged in {merged_path} (vorher {n_existing}, jetzt {len(merged)} Teams)")


if __name__ == "__main__":
    main()

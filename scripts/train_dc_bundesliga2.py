"""Phase C — C1: Dixon-Coles-Fit für 2. Bundesliga.

Nutzt letzte 5 Saisons (ältere Kader zu weit weg — Elo trägt Langzeit-Info).
KEINE Cluster-Priors (TEAM_CONFEDERATION ist nationals-only), KEIN WC-Boost.

Persistiert:
  models/dc_bundesliga2/params_YYYYMMDD.pkl + params_latest.pkl (Symlink)
  models/dc_bundesliga2/metadata.json
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from src.config import DATA_CACHE, MODELS_DIR, canonical_name
from src.models import dixon_coles

DEFAULT_SEASONS = ["2122", "2223", "2324", "2425", "2526"]
BL2_PHI = 0.0018   # niedrigere Time-Decay-Rate für Club-Fußball (Dixon-Coles-Paper-Empfehlung)
BL2_ELO_SCALE = 600.0


def _load_matches(seasons: list[str]) -> pd.DataFrame:
    """Lädt 2.BL-Matches aus dem cache-pkl (build_bundesliga2_universe.py-Output)."""
    path = DATA_CACHE / "bundesliga2_matches.pkl"
    if not path.exists():
        raise FileNotFoundError(f"{path} fehlt. Erst scripts/build_bundesliga2_universe.py laufen lassen.")
    with open(path, "rb") as f:
        df = pickle.load(f)
    df = df[df["season"].isin(seasons)].copy()
    df["home_team"] = df["home_team"].map(lambda t: canonical_name(str(t)))
    df["away_team"] = df["away_team"].map(lambda t: canonical_name(str(t)))
    df = df.dropna(subset=["home_score", "away_score"])
    df["home_score"] = df["home_score"].astype(int)
    df["away_score"] = df["away_score"].astype(int)
    # tournament leer → keine TOURNAMENT_WEIGHTS-Skalierung, keine WC-Boost
    df["tournament"] = ""
    df["neutral"] = False
    return df.sort_values("date").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=str, default=",".join(DEFAULT_SEASONS),
                    help="Comma-separated season codes (default: last 5)")
    ap.add_argument("--out", type=str, default=None, help="Output directory (default: models/dc_bundesliga2)")
    args = ap.parse_args()

    seasons = [s.strip() for s in args.seasons.split(",") if s.strip()]
    matches = _load_matches(seasons)
    print(f"Trainiere DC auf {len(matches)} 2.BL-Matches ({len(seasons)} Saisons: {seasons})")

    print("Fitting Dixon-Coles (Club-Config: phi=0.0018, no cluster, no WC-boost)...")
    params = dixon_coles.fit(
        matches,
        phi=BL2_PHI,
        max_iter=2000,
        regularization=0.05,
        wc2026_boost_override=1.0,   # explizit deaktiviert (Club-Kontext)
        cluster_map=None,            # TEAM_CONFEDERATION nur für nationals
        cluster_strength=0.0,
    )
    n_teams = len(params.attack)
    print(f"  ✓ Fit complete: {n_teams} Teams, home_adv={params.home_adv:.3f}, rho={params.rho:.3f}")

    out_dir = Path(args.out) if args.out else MODELS_DIR / "dc_bundesliga2"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    dated_path = out_dir / f"params_{today}.pkl"
    dixon_coles.save(params, dated_path)

    # Symlink params_latest.pkl → params_YYYYMMDD.pkl
    latest = out_dir / "params_latest.pkl"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    try:
        latest.symlink_to(dated_path.name)
    except OSError:
        # Windows / restricted FS: fallback copy
        import shutil
        shutil.copy(dated_path, latest)

    meta = {
        "fit_date": today,
        "n_matches": len(matches),
        "n_teams": n_teams,
        "seasons": seasons,
        "phi": BL2_PHI,
        "home_adv": float(params.home_adv),
        "rho": float(params.rho),
        "top5_attack": sorted(params.attack.items(), key=lambda kv: kv[1], reverse=True)[:5],
        "bottom5_attack": sorted(params.attack.items(), key=lambda kv: kv[1])[:5],
    }
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nGespeichert: {dated_path}")
    print(f"Symlink:     {latest}")
    print(f"Meta:        {out_dir / 'metadata.json'}")
    print(f"\nTop-Attack: {meta['top5_attack']}")


if __name__ == "__main__":
    main()

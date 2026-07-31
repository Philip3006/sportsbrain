"""Tennis-Elo Retrain (Roadmap TENNIS P1.6).

Woechentlicher Rebuild der TennisEloRatings aus tennis-data.co.uk XLSX
(Sackmann-Fallback, aktuell primaere Quelle seit Sackmann offline).

Output:
  models/tennis/elo_snapshot.pkl  — pickled TennisEloRatings
  models/tennis/elo_meta.json     — Metadata (source, n_matches, ref_date)

Usage:
  python3 scripts/tennis_retrain.py
  python3 scripts/tennis_retrain.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.models.tennis_elo import compute_tennis_elo, top_players
from src.tennis.elo_source import load_match_history

MODELS_DIR = ROOT / "models" / "tennis"
SNAPSHOT_PATH = MODELS_DIR / "elo_snapshot.pkl"
META_PATH = MODELS_DIR / "elo_meta.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    ref_date = datetime.now(timezone.utc).replace(tzinfo=None)
    print(f"[retrain] Reference-Date: {ref_date.isoformat()}")

    matches, source = load_match_history()
    print(f"[retrain] Match-History-Quelle: {source}, Zeilen: {len(matches)}")

    if matches.empty:
        print("[retrain] ⚠️  Keine Matches geladen — Abbruch")
        return 1

    ratings = compute_tennis_elo(matches, reference_date=ref_date)
    print(f"[retrain] Elo berechnet: {len(ratings.overall)} Spieler in overall")

    top20_overall = top_players(ratings, surface="overall", n=20)
    top20_hard = top_players(ratings, surface="hard", n=20)
    top20_clay = top_players(ratings, surface="clay", n=20)
    top20_grass = top_players(ratings, surface="grass", n=20)

    print("\n[retrain] Top-5 Overall:")
    for name, rating in top20_overall[:5]:
        print(f"  {rating:.1f}  {name}")

    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "reference_date": ref_date.isoformat(),
        "n_matches": int(len(matches)),
        "n_players_overall": len(ratings.overall),
        "n_players_hard": len(ratings.by_surface.get("hard", {})),
        "n_players_clay": len(ratings.by_surface.get("clay", {})),
        "n_players_grass": len(ratings.by_surface.get("grass", {})),
        "top20_overall": [{"name": n, "elo": r} for n, r in top20_overall],
        "top20_hard":    [{"name": n, "elo": r} for n, r in top20_hard],
        "top20_clay":    [{"name": n, "elo": r} for n, r in top20_clay],
        "top20_grass":   [{"name": n, "elo": r} for n, r in top20_grass],
    }

    if args.dry_run:
        print(f"\n[DRY-RUN] Wuerde speichern nach: {SNAPSHOT_PATH}")
        print(f"[DRY-RUN] Meta:\n{json.dumps({k:v for k,v in meta.items() if not k.startswith('top20')}, indent=2)}")
        return 0

    with SNAPSHOT_PATH.open("wb") as f:
        pickle.dump(ratings, f)
    META_PATH.write_text(json.dumps(meta, indent=2))
    print(f"\n[retrain] ✓ Snapshot gespeichert: {SNAPSHOT_PATH} ({SNAPSHOT_PATH.stat().st_size // 1024} KB)")
    print(f"[retrain] ✓ Meta gespeichert:     {META_PATH}")

    # Health-Writer
    try:
        from src.monitoring.health_writer import write_health
        write_health("tennis_retrain", status="ok", exit_code=0, fallback_used=source if source != "sackmann" else None)
    except Exception as e:
        print(f"[retrain] Health-Writer failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

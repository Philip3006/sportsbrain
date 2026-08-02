"""J8-I7 — Tennis Serve-Stats Snapshot Recorder.

Läuft im Cron 1×/Tag: für jeden bekannten aktiven Spieler wird das aktuelle
Tennis-Abstract Aggregate (dominance/ace/df/win-rate über letzte 20 Matches)
in `data/cache/tennis_stats_history.jsonl` als point-in-time-Snapshot
append-only geschrieben.

Nach 6+ Monaten steht damit die Datenbasis für den echten LGBM-Retrain (J2-N):
statt heutigem cumulativen Aggregate kann das Modell dann pro Training-Match
den zum Match-Zeitpunkt gültigen ServeAggregate lookup.

Spieler-Liste: Union aus TheOddsAPI-Active-Matches + Top-100-Fallback aus
`data/cache/tennis_stats/*.json` (vorhandener Player-Cache).

Usage:
    python3 scripts/tennis_stats_snapshot.py
    python3 scripts/tennis_stats_snapshot.py --players Alcaraz Sinner --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SNAPSHOT_HISTORY = ROOT / "data" / "cache" / "tennis_stats_history.jsonl"
PLAYER_CACHE_DIR = ROOT / "data" / "cache" / "tennis_stats"


def _known_players(explicit: list[str] | None) -> list[tuple[str, str]]:
    """Liste (player_name, tour) — 'atp' als Default; WTA-Spieler werden anhand
    Cache-Datei-Metadaten erkannt (fällt sonst auf ATP-Endpoint zurück)."""
    if explicit:
        return [(p, "atp") for p in explicit]
    if not PLAYER_CACHE_DIR.exists():
        return []
    out: list[tuple[str, str]] = []
    for f in PLAYER_CACHE_DIR.glob("*.json"):
        slug = f.stem
        if not slug or len(slug) < 3:
            continue
        # Slug ist bereits normalisiert (kein Space) → wir extrahieren nicht rück,
        # sondern nutzen `fetch_aggregate(slug, ...)`-freundlichen Namen mit Space.
        # Grobe Rekonstruktion: CamelCase-Splits sind riskant → nutze slug direkt.
        out.append((slug, "atp"))
    return out[:200]  # Sanity-Cap


def _snapshot_player(name: str, tour: str, dry_run: bool) -> dict | None:
    from src.data.tennis_stats import fetch_aggregate
    try:
        agg = fetch_aggregate(name, last_n=20, tour=tour)
    except Exception as e:
        print(f"  ERR {name}: {e}")
        return None
    if agg.n_matches == 0:
        return None
    entry = {
        "snapshot_date": date.today().isoformat(),
        "player": name,
        "tour": tour,
        "n_matches": agg.n_matches,
        "dominance_rate": round(agg.dominance_rate, 4),
        "ace_rate": round(agg.ace_rate, 4),
        "df_rate": round(agg.df_rate, 4),
        "win_rate": round(agg.win_rate, 4),
        "ace_df_ratio": round(agg.ace_df_ratio, 4),
        "first_serve_pct": round(agg.first_serve_pct, 4),
        "first_serve_win_pct": round(agg.first_serve_win_pct, 4),
        "second_serve_win_pct": round(agg.second_serve_win_pct, 4),
        "bp_save_pct": round(agg.bp_save_pct, 4),
        "bp_conv_pct": round(agg.bp_conv_pct, 4),
    }
    if not dry_run:
        SNAPSHOT_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with SNAPSHOT_HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_snapshot_asof(player: str, asof_date: str, tour: str = "atp") -> dict | None:
    """Public API für WF: liefert den letzten Snapshot des Spielers VOR asof_date.
    Return None wenn kein Snapshot existiert."""
    if not SNAPSHOT_HISTORY.exists():
        return None
    best = None
    for line in SNAPSHOT_HISTORY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("player") != player:
            continue
        if r.get("tour", "atp") != tour:
            continue
        sd = r.get("snapshot_date", "")
        if sd and sd < asof_date:
            if best is None or sd > best["snapshot_date"]:
                best = r
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", nargs="*", default=None,
                    help="Explizite Player-Liste (sonst: aus Cache-Files ableiten)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep-s", type=float, default=1.0,
                    help="Rate-Limit-Pause zwischen Fetches (default 1s)")
    args = ap.parse_args()

    players = _known_players(args.players)
    print(f"[snapshot] {len(players)} Spieler zu prüfen")
    written = 0
    for i, (p, t) in enumerate(players):
        entry = _snapshot_player(p, t, args.dry_run)
        if entry:
            written += 1
            if written <= 5:
                print(f"  {p:20s} n={entry['n_matches']:3d} dom={entry['dominance_rate']:.3f} "
                      f"ace={entry['ace_rate']:.3f}")
        if i < len(players) - 1:
            time.sleep(args.sleep_s)
    print(f"\n[snapshot] {written}/{len(players)} Snapshots geschrieben")
    return 0


if __name__ == "__main__":
    sys.exit(main())

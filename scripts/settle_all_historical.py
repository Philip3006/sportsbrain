"""
One-time script: settle alle archivierten Ghost- und Pending-Signale.

- Football Ghosts: WM 2026 Ergebnisse hardcoded (aus Web-Research)
- Tennis Pending: Munar/Blockx Walkover + ESPN-Window
- BL2 Pending: ESPN-Fetch
- Unresolvable → bleiben ghost/pending

CLI: python3 scripts/settle_all_historical.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.settle_bets import settle_market
from src.scanner.output import SIGNAL_HISTORY
from src.betting.tennis_settlement import settle_tennis_market
from src.tennis.backfill_helpers import fetch_espn_window, lookup_tennis_score
from src.football.backfill_helpers import fetch_bl2_window

SIGNAL_PERF = ROOT / "data" / "cache" / "signal_performance.json"

# ---------------------------------------------------------------------------
# WM 2026 Ergebnisse (home_score, away_score) — aus Web-Research 2026-08-13
# Format: (home_canonical, away_canonical) → (home_score, away_score)
# ---------------------------------------------------------------------------
WM_RESULTS: dict[tuple[str, str], tuple[int, int]] = {
    # Gruppe J: Argentinien, Algerien, Österreich, Jordanien
    ("Algeria", "Austria"): (3, 3),
    ("Argentina", "Algeria"): (3, 0),
    ("Argentina", "Austria"): (2, 0),
    ("Argentina", "Jordan"): (3, 1),
    ("Jordan", "Argentina"): (1, 3),  # ghost hat Jordan=home
    ("Algeria", "Jordan"): (2, 1),

    # Gruppe A: Mexiko, Südafrika, Korea, Tschechien
    ("Czechia", "Mexico"): (0, 3),
    ("Mexico", "South Africa"): (2, 0),
    ("Korea Republic", "Czechia"): (2, 1),
    ("Czechia", "South Africa"): (1, 1),
    ("Mexico", "Korea Republic"): (1, 0),
    ("South Africa", "Korea Republic"): (1, 0),

    # Gruppe B: Kanada, Bosnien, Schweiz, Katar
    ("Bosnia and Herzegovina", "Qatar"): (3, 1),
    ("Canada", "Bosnia and Herzegovina"): (1, 1),
    ("Canada", "Qatar"): (6, 0),
    ("Switzerland", "Bosnia and Herzegovina"): (4, 1),
    ("Switzerland", "Qatar"): (1, 1),

    # Gruppe D/G: Belgien, Ägypten, Neuseeland, Iran
    ("Belgium", "Egypt"): (1, 1),
    ("Belgium", "New Zealand"): (5, 1),
    ("New Zealand", "Belgium"): (1, 5),  # ghost hat NZ=home
    ("Egypt", "New Zealand"): (3, 1),
    ("Egypt", "Iran"): (1, 1),
    ("Australia", "Egypt"): (1, 1),

    # Gruppe E: Deutschland, Curacao, Ecuador, Elfenbeinküste
    ("Curacao", "Cote d'Ivoire"): (0, 2),
    ("Germany", "Curacao"): (7, 1),
    ("Germany", "Cote d'Ivoire"): (2, 1),
    ("Cote d'Ivoire", "Ecuador"): (1, 0),
    ("Ecuador", "Curacao"): (0, 0),

    # Gruppe G/I: Frankreich, Senegal, Irak, Norwegen
    ("France", "Senegal"): (3, 1),
    ("France", "Iraq"): (3, 0),
    ("France", "Norway"): (4, 1),
    ("Norway", "France"): (1, 4),   # ghost hat Norway=home
    ("Norway", "Senegal"): (3, 2),
    ("Senegal", "Iraq"): (5, 0),
    ("Cote d'Ivoire", "Norway"): (1, 2),  # Norway 2-1 Cote d'Ivoire

    # Gruppe L: England, Kroatien, Ghana, Panama
    ("England", "Croatia"): (4, 2),
    ("England", "Ghana"): (0, 0),
    ("Panama", "England"): (0, 2),   # ghost hat Panama=home
    ("England", "Panama"): (2, 0),
    ("Croatia", "Ghana"): (2, 1),

    # Gruppe I: Spanien, Uruguay, Kap Verde, Saudi-Arabien
    ("Spain", "Cape Verde"): (0, 0),
    ("Uruguay", "Cape Verde"): (2, 2),
    ("Cape Verde", "Saudi Arabia"): (0, 0),

    # Gruppe K: Kolumbien, Portugal, DR Kongo, Usbekistan
    ("Colombia", "Ghana"): (1, 0),
    ("Colombia", "Portugal"): (0, 0),
    ("Colombia", "DR Congo"): (1, 0),
    ("Colombia", "Uzbekistan"): (1, 0),
    ("Portugal", "Uzbekistan"): (5, 0),

    # Knockout — R32
    ("Brazil", "Japan"): (2, 1),
    ("France", "Sweden"): (3, 0),
    ("England", "DR Congo"): (2, 1),
    ("Germany", "Paraguay"): (1, 1),   # PKs: Paraguay gewinnt
    ("Argentina", "Cape Verde"): (3, 2),
    ("Belgium", "Senegal"): (3, 2),
    ("Canada", "Morocco"): (0, 3),

    # Knockout — R16
    ("England", "Mexico"): (3, 2),
    ("Argentina", "Egypt"): (3, 2),
    ("France", "Paraguay"): (1, 0),
    ("Norway", "Brazil"): (2, 1),   # Norwegen schlägt Brasilien!
    ("Brazil", "Norway"): (1, 2),   # ghost hat Brazil=home
    ("Morocco", "Canada"): (3, 0),

    # Knockout — QF
    ("Argentina", "Switzerland"): (3, 1),
    ("France", "Morocco"): (2, 0),
    ("England", "Norway"): (2, 1),

    # Knockout — SF
    ("Argentina", "England"): (2, 1),
    ("England", "Argentina"): (1, 2),  # ghost hat England=home
    ("Spain", "France"): (2, 0),
    ("France", "Spain"): (0, 2),

    # Finale
    ("Spain", "Argentina"): (1, 0),  # AET

    # Fehlende/umgekehrte Einträge (aus 2. Web-Recherche 2026-08-13)
    ("Norway", "England"): (1, 2),   # QF England gewinnt 2-1
    ("Mexico", "England"): (2, 3),   # R16 England gewinnt 3-2
    ("Paraguay", "France"): (0, 1),  # R16 Frankreich gewinnt 1-0
    ("Morocco", "Canada"): (3, 0),   # R16
    ("Canada", "South Africa"): (1, 0),  # R32
    ("South Africa", "Canada"): (0, 1),  # umgekehrt
    ("Netherlands", "Tunisia"): (3, 1),  # Gruppe
    ("Tunisia", "Netherlands"): (1, 3),  # umgekehrt
    ("Portugal", "Croatia"): (2, 1),     # R32
    ("Portugal", "Spain"): (0, 1),       # R16 Spanien gewinnt 1-0
    ("Spain", "Portugal"): (1, 0),       # umgekehrt
    ("Scotland", "Brazil"): (0, 3),      # Gruppe
    ("Brazil", "Scotland"): (3, 0),      # umgekehrt
    ("Switzerland", "Algeria"): (3, 0),  # R32
    ("Algeria", "Switzerland"): (0, 3),  # umgekehrt
    ("Morocco", "Haiti"): (4, 2),        # Gruppe
    ("Haiti", "Morocco"): (2, 4),        # umgekehrt
    ("Spain", "Austria"): (3, 0),        # R32
    ("Austria", "Spain"): (0, 3),        # umgekehrt
    ("Mexico", "Ecuador"): (2, 0),       # Gruppe
    ("Ecuador", "Mexico"): (0, 2),       # umgekehrt
    ("United States", "Bosnia and Herzegovina"): (2, 0),  # Gruppe
    ("Bosnia and Herzegovina", "United States"): (0, 2),  # umgekehrt
    ("Switzerland", "Colombia"): (2, 1), # R16 (Schweiz schlug Kolumbien)
    ("Colombia", "Switzerland"): (1, 2), # umgekehrt
    ("Uruguay", "Spain"): (0, 2),        # Gruppe (Spanien schlug Uruguay)
    ("Spain", "Uruguay"): (2, 0),        # umgekehrt
    ("Ecuador", "Germany"): (0, 2),      # Gruppe E
    ("Germany", "Ecuador"): (2, 0),      # umgekehrt

    # BL2 alt (2025/26 Season — in Ghosts wegen fehlender Settlements)
    # Unbekannt — bleiben ghost
}

# Matches die nie stattgefunden haben → void
NEVER_HAPPENED = {
    ("Japan", "Sweden"),
    ("France", "England"),
    ("England", "France"),
    ("Sweden", "Japan"),
}

# Spezielle Tennis Walkovers (settled als Verlust für den Rückzieher)
TENNIS_WALKOVERS = {
    # match_id / (home, away) → winner side ("home" oder "away")
    ("Jaume Munar", "Alexander Blockx"): "away",   # Munar withdrew
}


def _norm(name: str) -> str:
    return name.strip()


def _lookup_wm(home: str, away: str) -> tuple[int, int] | None:
    key = (_norm(home), _norm(away))
    if key in WM_RESULTS:
        return WM_RESULTS[key]
    # Fuzzy: Teile des Namens
    for (h, a), score in WM_RESULTS.items():
        if h.lower() in home.lower() or home.lower() in h.lower():
            if a.lower() in away.lower() or away.lower() in a.lower():
                return score
    return None


import re as _re

def _settle_ou_fallback(market: str, home_score: int, away_score: int) -> str | None:
    """Settle o/u lines that settle_market doesn't handle (2.0, 2.25, 2.75, 3.25 etc.)."""
    m = _re.match(r"o/u(\d+(?:\.\d+)?)_(over|under)$", market)
    if not m:
        return None
    line = float(m.group(1))
    direction = m.group(2)
    total = home_score + away_score
    if direction == "over":
        if total > line:
            return "won"
        elif total < line:
            return "lost"
        else:
            return "void"
    else:
        if total < line:
            return "won"
        elif total > line:
            return "lost"
        else:
            return "void"


def _settle_wm_market(market: str, home_score: int, away_score: int) -> str | None:
    if market.startswith("scorer_"):
        return None  # kann nicht ohne Goal-Daten settled werden
    try:
        result = settle_market(market, home_score, away_score)
        if result is not None:
            return result
        return _settle_ou_fallback(market, home_score, away_score)
    except Exception:
        return _settle_ou_fallback(market, home_score, away_score)


def _load_signals() -> list[dict]:
    rows = []
    for line in SIGNAL_HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _save_signals(rows: list[dict]) -> None:
    SIGNAL_HISTORY.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def _aggregate_and_save(rows: list[dict]) -> dict:
    by_market: dict = defaultdict(lambda: {
        "n": 0, "n_placed": 0, "n_outcome": 0, "n_won": 0,
        "n_void": 0, "ev_sum": 0.0, "stake_sum": 0.0, "return_sum": 0.0,
    })
    by_conf: dict = defaultdict(lambda: {"n": 0, "n_won": 0, "n_outcome": 0})
    by_sport: dict = defaultdict(lambda: {"n": 0, "n_won": 0, "n_lost": 0, "n_void": 0,
                                          "n_ghost": 0, "n_pending": 0, "stake_sum": 0.0, "return_sum": 0.0})

    for r in rows:
        mkt = r.get("market", "unknown")
        conf = r.get("confidence", "UNKNOWN")
        sport = r.get("sport", "football")
        outcome = r.get("outcome")
        placed = r.get("placed", False)
        ev_pct = r.get("ev_pct", 0.0) or 0.0
        odds = r.get("decimal_odds", 0.0) or 0.0
        stake = r.get("stake_eur", 5.0) or 5.0

        by_market[mkt]["n"] += 1
        by_market[mkt]["ev_sum"] += ev_pct
        if placed:
            by_market[mkt]["n_placed"] += 1

        by_sport[sport]["n"] += 1

        if outcome == "won":
            by_market[mkt]["n_outcome"] += 1
            by_market[mkt]["n_won"] += 1
            by_market[mkt]["stake_sum"] += stake
            by_market[mkt]["return_sum"] += stake * odds
            by_conf[conf]["n"] += 1
            by_conf[conf]["n_won"] += 1
            by_conf[conf]["n_outcome"] += 1
            by_sport[sport]["n_won"] += 1
            by_sport[sport]["stake_sum"] += stake
            by_sport[sport]["return_sum"] += stake * odds
        elif outcome == "lost":
            by_market[mkt]["n_outcome"] += 1
            by_market[mkt]["stake_sum"] += stake
            by_conf[conf]["n"] += 1
            by_conf[conf]["n_outcome"] += 1
            by_sport[sport]["n_lost"] += 1
            by_sport[sport]["stake_sum"] += stake
        elif outcome == "void":
            by_market[mkt]["n_void"] += 1
            by_conf[conf]["n"] += 1
        elif outcome == "push":
            by_market[mkt]["n_void"] += 1
        elif outcome == "ghost":
            by_sport[sport]["n_ghost"] += 1
        elif outcome is None:
            by_sport[sport]["n_pending"] += 1

    def _safe_div(a, b):
        return round(a / b, 4) if b else None

    perf = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_signals_total": len(rows),
        "n_with_outcome": sum(1 for r in rows if r.get("outcome") is not None),
        "n_won": sum(1 for r in rows if r.get("outcome") == "won"),
        "n_lost": sum(1 for r in rows if r.get("outcome") == "lost"),
        "n_void": sum(1 for r in rows if r.get("outcome") in ("void", "push")),
        "n_ghost": sum(1 for r in rows if r.get("outcome") == "ghost"),
        "n_pending": sum(1 for r in rows if r.get("outcome") is None),
        "by_sport": {
            sp: {
                **d,
                "pnl": round(d["return_sum"] - d["stake_sum"], 2),
                "roi_pct": round((d["return_sum"] - d["stake_sum"]) / d["stake_sum"] * 100, 1) if d["stake_sum"] else None,
                "win_rate": round(d["n_won"] / (d["n_won"] + d["n_lost"]) * 100, 1) if (d["n_won"] + d["n_lost"]) else None,
            }
            for sp, d in sorted(by_sport.items())
        },
        "by_market": {
            mkt: {
                "n": d["n"],
                "n_placed": d["n_placed"],
                "n_outcome": d["n_outcome"],
                "n_won": d["n_won"],
                "n_void": d["n_void"],
                "accuracy": _safe_div(d["n_won"], d["n_outcome"]),
                "ev_mean": round(d["ev_sum"] / d["n"], 2) if d["n"] else None,
                "pnl": round(d["return_sum"] - d["stake_sum"], 2),
                "roi_pct": round((d["return_sum"] - d["stake_sum"]) / d["stake_sum"] * 100, 1) if d["stake_sum"] else None,
            }
            for mkt, d in sorted(by_market.items())
        },
        "by_confidence": {
            conf: {
                "n": d["n"],
                "n_outcome": d["n_outcome"],
                "accuracy": _safe_div(d["n_won"], d["n_outcome"]),
            }
            for conf, d in sorted(by_conf.items())
        },
    }

    SIGNAL_PERF.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_PERF.write_text(json.dumps(perf, indent=2, ensure_ascii=False), encoding="utf-8")
    return perf


def run(dry_run: bool = False) -> None:
    rows = _load_signals()
    now_ts = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()

    resolved = 0
    voided = 0
    still_ghost = 0

    # --- 1) Tennis ESPN für pending signals ---
    pending_tennis = [r for r in rows if r.get("outcome") is None and r.get("sport") == "tennis"]
    tennis_scores: dict = {}
    if pending_tennis:
        scan_dates = sorted(set(r.get("scan_date", "")[:10] for r in pending_tennis if r.get("scan_date")))
        tennis_scores, _ = fetch_espn_window(scan_dates, {}, window_days=14)
        print(f"[settle_all] Tennis ESPN: {len(tennis_scores)} Match-Keys geladen")

    # --- 2) BL2 ESPN für pending BL2 ---
    pending_bl2 = [r for r in rows if r.get("outcome") is None and r.get("league") == "bl2"
                   and not r.get("match_id", "").startswith("bl2_mock")]
    bl2_scores: dict = {}
    if pending_bl2:
        scan_dates_bl2 = sorted(set(r.get("scan_date", "")[:10] for r in pending_bl2 if r.get("scan_date")))
        bl2_scores, _ = fetch_bl2_window(scan_dates_bl2, {})
        print(f"[settle_all] BL2 ESPN: {len(bl2_scores)} Match-Keys geladen")

    # --- 3) Alle Signale durchgehen ---
    for r in rows:
        outcome = r.get("outcome")
        home = r.get("home", "")
        away = r.get("away", "")
        market = r.get("market", "")
        sport = r.get("sport", "football")
        scan_date = r.get("scan_date", "")

        # Bereits settled — überspringen
        if outcome in ("won", "lost", "push"):
            continue

        # Void bereits gesetzt — überspringen
        if outcome == "void":
            continue

        # Heute oder zukünftig — überspringen
        if scan_date >= today:
            continue

        # --- Tennis Walkover ---
        if sport == "tennis" and outcome is None:
            wo_key = (home, away)
            if wo_key in TENNIS_WALKOVERS:
                winner = TENNIS_WALKOVERS[wo_key]
                if market.startswith("o/u") or market.startswith("ah"):
                    new_outcome = "void"
                elif market == "home":
                    new_outcome = "won" if winner == "home" else "lost"
                elif market == "away":
                    new_outcome = "won" if winner == "away" else "lost"
                else:
                    new_outcome = "void"
                if not dry_run:
                    r["outcome"] = new_outcome
                    r["outcome_ts"] = now_ts
                print(f"  [walkover] {home} vs {away} [{market}] → {new_outcome}")
                if new_outcome != "void":
                    resolved += 1
                else:
                    voided += 1
                continue

        # --- Tennis ESPN ---
        if sport == "tennis" and outcome is None:
            sc = lookup_tennis_score(home, away, r.get("match_id", ""), tennis_scores)
            if sc:
                result = settle_tennis_market(market, sc)
                if result and result != "pending":
                    if not dry_run:
                        r["outcome"] = result
                        r["outcome_ts"] = now_ts
                    resolved += 1
                    continue

        # --- BL2 ESPN (echte match_id) ---
        if sport == "football" and r.get("league") == "bl2" and outcome is None:
            mid = r.get("match_id", "")
            sc = bl2_scores.get(mid)
            if sc and isinstance(sc, dict):
                result = _settle_wm_market(market, sc.get("home_score", 0), sc.get("away_score", 0))
                if result:
                    if not dry_run:
                        r["outcome"] = result
                        r["outcome_ts"] = now_ts
                    resolved += 1
                    continue

        # --- Football Ghosts: WM 2026 Lookup ---
        if sport == "football" and outcome == "ghost":
            # Match nie stattgefunden?
            key = (_norm(home), _norm(away))
            if key in NEVER_HAPPENED:
                if not dry_run:
                    r["outcome"] = "void"
                    r["outcome_ts"] = now_ts
                voided += 1
                continue

            scores = _lookup_wm(home, away)
            if scores is not None:
                h_score, a_score = scores
                result = _settle_wm_market(market, h_score, a_score)
                if result:
                    if not dry_run:
                        r["outcome"] = result
                        r["outcome_ts"] = now_ts
                    if dry_run:
                        print(f"  [wm] {home} vs {away} [{market}] {h_score}:{a_score} → {result}")
                    resolved += 1
                elif market.startswith("scorer_"):
                    # Scorer unresolvable → ghost bleibt
                    still_ghost += 1
                else:
                    still_ghost += 1
            else:
                still_ghost += 1

    print(f"[settle_all] {resolved} neu settled | {voided} void | {still_ghost} unresolvable (ghost bleibt)")

    if not dry_run:
        _save_signals(rows)
        perf = _aggregate_and_save(rows)

        # Ausgabe Zusammenfassung
        print()
        print("=== GESAMTBILANZ ALLER 1087 SIGNALE ===")
        print(f"Total:   {perf['n_signals_total']}")
        print(f"Won:     {perf['n_won']}")
        print(f"Lost:    {perf['n_lost']}")
        print(f"Void:    {perf['n_void']}")
        print(f"Ghost:   {perf['n_ghost']} (unresolvable: kein Score verfügbar)")
        print(f"Pending: {perf['n_pending']} (heute noch offen)")
        settled = perf['n_won'] + perf['n_lost']
        if settled:
            wr = perf['n_won'] / settled * 100
            print(f"Win Rate: {wr:.1f}% ({perf['n_won']}W/{perf['n_lost']}L)")

        print()
        print("=== NACH SPORT ===")
        for sp, d in perf["by_sport"].items():
            total = d["n_won"] + d["n_lost"]
            wr = f"{d['n_won']/total*100:.0f}%" if total else "n/a"
            pnl_str = f"{d['pnl']:+.2f}€" if d["stake_sum"] else "n/a"
            roi_str = f"{d['roi_pct']:+.1f}%" if d["roi_pct"] is not None else "n/a"
            print(f"  {sp:12s}: W{d['n_won']:3d} L{d['n_lost']:3d} V{d['n_void']:2d} Ghost{d['n_ghost']:3d} | WR {wr} | P&L {pnl_str} | ROI {roi_str}")

        print()
        print("=== NACH MARKT (settled, ≥5 Signale) ===")
        for mkt, d in sorted(perf["by_market"].items(), key=lambda x: -(x[1]["n_outcome"] or 0)):
            if (d["n_outcome"] or 0) < 5:
                continue
            acc = f"{d['accuracy']*100:.0f}%" if d["accuracy"] is not None else "n/a"
            pnl_str = f"{d['pnl']:+.2f}€" if d.get("pnl") is not None else "n/a"
            roi_str = f"{d['roi_pct']:+.1f}%" if d.get("roi_pct") is not None else "n/a"
            print(f"  {mkt:25s}: n={d['n_outcome']:3d} acc={acc} ev={d['ev_mean']:.0f}% | P&L {pnl_str} ROI {roi_str}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)

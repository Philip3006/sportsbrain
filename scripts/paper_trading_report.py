"""
Paper Trading Report — zeigt hypothetische P&L für alle archivierten Signale.

Signale mit placed=false werden mit dem gleichen Kelly-Stake bewertet als ob
sie platziert worden wären. Gibt einen klaren Überblick ob das Modell Geld
verdient hätte, ohne echtes Risiko.

CLI:
  python3 scripts/paper_trading_report.py             # letzte 30 Tage
  python3 scripts/paper_trading_report.py --days 7    # letzte 7 Tage
  python3 scripts/paper_trading_report.py --sport tennis
  python3 scripts/paper_trading_report.py --placed    # nur echte Bets (Kontrolle)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scanner.output import SIGNAL_HISTORY

UNIT_STAKE = 10.0  # €10 Fallback wenn stake_eur nicht gespeichert


def _load(days: int, sport: str | None, placed_only: bool) -> list[dict]:
    if not SIGNAL_HISTORY.exists():
        return []
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = []
    for line in SIGNAL_HISTORY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("scan_date", "") < cutoff:
            continue
        if sport and r.get("sport") != sport:
            continue
        if placed_only and not r.get("placed"):
            continue
        rows.append(r)
    return rows


def _pnl(row: dict) -> float | None:
    outcome = row.get("outcome")
    if outcome is None:
        return None
    stake = row.get("stake_eur") or UNIT_STAKE
    odds = row.get("decimal_odds", 2.0)
    if outcome == "won":
        return round(stake * (odds - 1), 2)
    if outcome == "lost":
        return round(-stake, 2)
    return 0.0  # push/void


def report(rows: list[dict], label: str) -> None:
    total = len(rows)
    with_outcome = [r for r in rows if r.get("outcome") is not None]
    pending = total - len(with_outcome)

    won = [r for r in with_outcome if r["outcome"] == "won"]
    lost = [r for r in with_outcome if r["outcome"] == "lost"]
    push = [r for r in with_outcome if r["outcome"] not in ("won", "lost")]

    pnl_vals = [p for r in with_outcome if (p := _pnl(r)) is not None]
    total_pnl = sum(pnl_vals)
    total_staked = sum(r.get("stake_eur") or UNIT_STAKE for r in with_outcome)
    roi = (total_pnl / total_staked * 100) if total_staked else 0.0

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Signale gesamt : {total}  (pending: {pending})")
    print(f"  Mit Outcome    : {len(with_outcome)}  ✅ {len(won)}W / ❌ {len(lost)}L / ↩️ {len(push)}P")
    print(f"  Gesetzt (€)    : {total_staked:.0f}€")
    print(f"  P&L            : {total_pnl:+.2f}€")
    print(f"  ROI            : {roi:+.1f}%")
    print(f"  Trefferquote   : {len(won)/len(with_outcome)*100:.1f}%" if with_outcome else "  Trefferquote   : —")

    # Aufschlüsselung nach Markt
    by_mkt: dict[str, list[dict]] = defaultdict(list)
    for r in with_outcome:
        mkt = r.get("market", "?")
        # Markt-Typ vereinfachen
        if mkt in ("home", "away", "draw"):
            key = "Match Winner"
        elif "ah" in mkt.lower():
            key = "Set AH +1.5"
        elif "o/u" in mkt.lower():
            key = "O/U Games"
        else:
            key = mkt
        by_mkt[key].append(r)

    if by_mkt:
        print(f"\n  {'Markt':<20} {'N':>4} {'W':>4} {'L':>4} {'P&L':>8} {'ROI':>7}")
        print(f"  {'-'*53}")
        for mkt, mkt_rows in sorted(by_mkt.items(), key=lambda x: -len(x[1])):
            mkt_won = sum(1 for r in mkt_rows if r["outcome"] == "won")
            mkt_pnl = sum(p for r in mkt_rows if (p := _pnl(r)) is not None)
            mkt_staked = sum(r.get("stake_eur") or UNIT_STAKE for r in mkt_rows)
            mkt_roi = (mkt_pnl / mkt_staked * 100) if mkt_staked else 0.0
            mkt_lost = sum(1 for r in mkt_rows if r["outcome"] == "lost")
            print(f"  {mkt:<20} {len(mkt_rows):>4} {mkt_won:>4} {mkt_lost:>4} "
                  f"{mkt_pnl:>+8.2f}€ {mkt_roi:>+6.1f}%")

    # Letzte 5 Ergebnisse
    recent = sorted(with_outcome, key=lambda r: r.get("scan_date", ""), reverse=True)[:5]
    if recent:
        print(f"\n  Letzte Ergebnisse:")
        for r in recent:
            p = _pnl(r)
            icon = "✅" if r["outcome"] == "won" else ("❌" if r["outcome"] == "lost" else "↩️")
            stake = r.get("stake_eur") or UNIT_STAKE
            print(f"  {icon} {r['home']} vs {r['away']} [{r['market']}]"
                  f"  @{r['decimal_odds']:.2f}  {p:+.2f}€  ({r['scan_date']})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper Trading Report")
    parser.add_argument("--days", type=int, default=30, help="Zeitraum in Tagen (default: 30)")
    parser.add_argument("--sport", default=None, help="Nur ein Sport: tennis / football")
    parser.add_argument("--placed", action="store_true",
                        help="Nur echte platzierte Bets (Kontrollgruppe)")
    args = parser.parse_args()

    rows = _load(args.days, args.sport, args.placed)
    if not rows:
        print(f"Keine Signale für die letzten {args.days} Tage gefunden.")
        return

    mode = "Echte Bets" if args.placed else "Paper Trading (alle Signale)"
    sport_label = f" · {args.sport.upper()}" if args.sport else ""
    label = f"{mode}{sport_label} — letzte {args.days} Tage"

    # Gesamt
    report(rows, label)

    # Split: paper (unplaced) vs. placed — nur wenn nicht bereits gefiltert
    if not args.placed:
        paper = [r for r in rows if not r.get("placed")]
        placed = [r for r in rows if r.get("placed")]
        if paper and placed:
            report(paper, f"  ↳ Nur Paper (nicht platziert){sport_label}")
            report(placed, f"  ↳ Nur Platziert (echte Bets){sport_label}")


if __name__ == "__main__":
    main()

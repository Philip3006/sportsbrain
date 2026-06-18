"""
Interactive bet confirmation from current signals.json.
Run this after reviewing signals on the dashboard:

  python3 scripts/confirm_bets.py

Shows all available signals with EV, lets you pick which to place.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.betting.ledger import append_bets, count_open_bets, LEDGER_PATH, ledger_summary
from src.notifications.web_dashboard import BetSignal


def _make_signal(d: dict) -> BetSignal:
    s = BetSignal.__new__(BetSignal)
    s.home = d["match"].split(" vs ")[0].strip()
    s.away = d["match"].split(" vs ")[1].strip()
    s.market = d["market"]
    s.decimal_odds = d["odds"]
    s.model_prob = d["model_prob"] / 100
    s.ev = d["ev_pct"] / 100
    s.confidence = d["confidence"]
    s.stake_eur = d["stake_eur"]
    s.match_id = d.get("match_id", f"{s.home}_{s.away}")
    s.b365_odds = d["odds"]
    return s


def main(bankroll: float = 100.0) -> None:
    sig_path = Path(__file__).parent.parent / "docs" / "data" / "signals.json"
    if not sig_path.exists():
        print("signals.json nicht gefunden. Zuerst Scan ausführen.")
        return

    data = json.loads(sig_path.read_text())
    signals = data.get("football", []) + data.get("tennis", [])
    signals = [s for s in signals if s.get("ev_pct", 0) >= 3]
    signals.sort(key=lambda s: s["ev_pct"], reverse=True)

    open_count = count_open_bets(LEDGER_PATH)
    max_bets = 5
    slots = max(0, max_bets - open_count)

    print(f"\n=== SportsBrain — Wetten bestätigen ===")
    print(f"Offene Wetten: {open_count}/{max_bets} | Freie Slots: {slots}")
    print(f"Signals aktualisiert: {data.get('updated', '—')}\n")

    if not signals:
        print("Keine Signale mit EV ≥ 3% gefunden.")
        return

    for i, s in enumerate(signals, 1):
        conf_mark = "🟢" if s["confidence"] == "HIGH" else "🟡"
        print(f"  [{i:2d}] {conf_mark} {s['match']:<35} {s['market']:<20} "
              f"@ {s['odds']:.2f}  EV +{s['ev_pct']:.1f}%  €{s['stake_eur']:.0f}")

    print(f"\nNummern eingeben (z.B. 1 3 5), leer = alle, q = abbrechen:")
    raw = input("> ").strip()

    if raw.lower() == "q" or raw.lower() == "quit":
        print("Abgebrochen.")
        return

    if raw == "":
        chosen = signals[:slots] if slots > 0 else []
    else:
        try:
            idxs = [int(x) - 1 for x in raw.split()]
            chosen = [signals[i] for i in idxs if 0 <= i < len(signals)]
        except ValueError:
            print("Ungültige Eingabe.")
            return

    if not chosen:
        print("Keine Wetten ausgewählt.")
        return

    if slots == 0:
        print(f"Portfolio voll ({open_count}/{max_bets}). Erst bestehende Wetten schließen.")
        return

    chosen = chosen[:slots]
    print(f"\nZu platzierende Wetten ({len(chosen)}):")
    for s in chosen:
        print(f"  {s['match']} | {s['market']} | @ {s['odds']:.2f} | EV +{s['ev_pct']:.1f}% | €{s['stake_eur']:.0f}")

    confirm = input("\nBestätigen? (j/n): ").strip().lower()
    if confirm != "j":
        print("Abgebrochen.")
        return

    n = 0
    for sd in chosen:
        sig = _make_signal(sd)
        match_date = sd.get("kickoff", "")[:10] if sd.get("kickoff") else ""
        n += append_bets([sig], bankroll, LEDGER_PATH, match_date=match_date)

    print(f"\n✓ {n} Wette(n) ins Ledger eingetragen.")
    summary = ledger_summary()
    print(f"Portfolio: {summary.get('n_open', 0)}/{max_bets} aktive Wetten")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--bankroll", type=float, default=100.0)
    args = parser.parse_args()
    main(args.bankroll)

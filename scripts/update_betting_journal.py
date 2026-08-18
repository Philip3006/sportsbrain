"""
Betting Journal — täglich auto-updated aus signal_history.jsonl.

Erzeugt results/betting_journal.md mit:
  1. Tages-Snapshots (append)
  2. Match-Log (vollständig, neueste oben)
  3. Breakdown-Tabellen (Kategorie / Surface / Markt / EV-Band)

CLI:
  python3 scripts/update_betting_journal.py
  python3 scripts/update_betting_journal.py --sport tennis
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIGNAL_HISTORY = ROOT / "data" / "cache" / "signal_history.jsonl"
# P0D-002: betting journal and journal snapshots live in the private ledger repo.
from src.config import (
    betting_journal_path as _betting_journal_path,
)
from src.config import (
    betting_journal_snapshot_path as _betting_journal_snapshot_path,
)

UNIT_STAKE = 10.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_signals(sport: str | None) -> list[dict]:
    if not SIGNAL_HISTORY.exists():
        return []
    rows = []
    for line in SIGNAL_HISTORY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if sport and r.get("sport") != sport:
            continue
        rows.append(r)
    return rows


def _pnl(r: dict) -> float | None:
    outcome = r.get("outcome")
    if outcome is None:
        return None
    stake = r.get("stake_eur") or UNIT_STAKE
    odds = r.get("decimal_odds", 2.0)
    if outcome == "won":
        return round(stake * (odds - 1), 2)
    if outcome == "lost":
        return round(-stake, 2)
    return 0.0


def _ev_band(ev_pct: float) -> str:
    if ev_pct >= 30:
        return "+30%+"
    if ev_pct >= 20:
        return "+20–30%"
    if ev_pct >= 10:
        return "+10–20%"
    return "<+10%"


def _mkt_label(market: str) -> str:
    m = market.lower()
    if m in ("home", "away", "draw"):
        return "Match Winner"
    if "ah" in m:
        return "Set AH +1.5"
    if "o/u_games" in m:
        return "O/U Games"
    if "o/u_sets" in m:
        return "O/U Sets"
    if m.startswith("score_"):
        return "Set Score"
    if m.startswith("first_set"):
        return "First Set"
    return market


def _stats(rows: list[dict]) -> dict:
    settled = [r for r in rows if r.get("outcome") is not None]
    won = [r for r in settled if r["outcome"] == "won"]
    lost = [r for r in settled if r["outcome"] == "lost"]
    pnl_vals = [p for r in settled if (p := _pnl(r)) is not None]
    staked = sum(r.get("stake_eur") or UNIT_STAKE for r in settled)
    pnl = sum(pnl_vals)
    roi = pnl / staked * 100 if staked else 0.0
    wr = len(won) / len(settled) * 100 if settled else 0.0
    return {
        "n": len(rows), "n_settled": len(settled),
        "w": len(won), "l": len(lost),
        "staked": staked, "pnl": pnl, "roi": roi, "wr": wr,
    }


def _breakdown_table(rows: list[dict], key_fn, header: str) -> list[str]:
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_key[key_fn(r)].append(r)
    if not by_key:
        return []
    lines = [
        f"\n### {header}\n",
        f"| {'Dimension':<22} | {'N':>4} | {'W':>4} | {'L':>4} | {'Win%':>6} | {'P&L':>9} | {'ROI':>7} |",
        f"|{'-'*24}|{'-'*6}|{'-'*6}|{'-'*6}|{'-'*8}|{'-'*11}|{'-'*9}|",
    ]
    for k, rs in sorted(by_key.items(), key=lambda x: -_stats(x[1])["n_settled"]):
        s = _stats(rs)
        if s["n_settled"] == 0:
            continue
        roi_str = f"{s['roi']:+.1f}%"
        pnl_str = f"{s['pnl']:+.2f}€"
        lines.append(
            f"| {k:<22} | {s['n_settled']:>4} | {s['w']:>4} | {s['l']:>4} "
            f"| {s['wr']:>5.1f}% | {pnl_str:>9} | {roi_str:>7} |"
        )
    return lines


# ---------------------------------------------------------------------------
# Tages-Snapshot
# ---------------------------------------------------------------------------

def _load_snapshots() -> list[dict]:
    snapshot_store = _betting_journal_snapshot_path()
    if not snapshot_store.exists():
        return []
    rows = []
    for line in snapshot_store.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _update_snapshot(rows: list[dict], sport: str | None) -> None:
    """Hängt einen Snapshot für heute an (einmal pro Tag)."""
    today = date.today().isoformat()
    existing = _load_snapshots()
    if any(s.get("date") == today and s.get("sport") == (sport or "all") for s in existing):
        return  # Heute schon gemacht
    s = _stats(rows)
    if s["n_settled"] == 0:
        return
    entry = {
        "date": today,
        "sport": sport or "all",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **s,
    }
    snapshot_store = _betting_journal_snapshot_path()
    snapshot_store.parent.mkdir(parents=True, exist_ok=True)
    with snapshot_store.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Journal bauen
# ---------------------------------------------------------------------------

def build_journal(rows: list[dict], sport: str | None) -> str:
    sport_label = sport.upper() if sport else "Alle Sportarten"
    settled = [r for r in rows if r.get("outcome") is not None]
    total_stats = _stats(rows)

    lines: list[str] = [
        f"# Betting Journal — {sport_label}",
        f"\n_Zuletzt aktualisiert: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n",
        "---\n",
    ]

    # ------------------------------------------------------------------ #
    # 1. Gesamt-KPIs
    # ------------------------------------------------------------------ #
    lines += [
        "## Gesamt-Performance\n",
        f"| Signale | Settled | W | L | Win% | P&L | ROI |",
        f"|---------|---------|---|---|------|-----|-----|",
        f"| {total_stats['n']} | {total_stats['n_settled']} "
        f"| {total_stats['w']} | {total_stats['l']} "
        f"| {total_stats['wr']:.1f}% "
        f"| {total_stats['pnl']:+.2f}€ "
        f"| {total_stats['roi']:+.1f}% |\n",
    ]

    # ------------------------------------------------------------------ #
    # 2. Tages-Snapshots
    # ------------------------------------------------------------------ #
    snapshots = [s for s in _load_snapshots() if s.get("sport") == (sport or "all")]
    if snapshots:
        lines += [
            "## Tages-Snapshots\n",
            "| Datum | W | L | Win% | P&L | ROI kum. |",
            "|-------|---|---|------|-----|----------|",
        ]
        cum_pnl = 0.0
        for s in sorted(snapshots, key=lambda x: x["date"]):
            cum_pnl += s.get("pnl", 0)
            lines.append(
                f"| {s['date']} | {s.get('w',0)} | {s.get('l',0)} "
                f"| {s.get('wr',0):.1f}% "
                f"| {s.get('pnl',0):+.2f}€ "
                f"| {cum_pnl:+.2f}€ |"
            )
        lines.append("")

    # ------------------------------------------------------------------ #
    # 3. Breakdown-Tabellen
    # ------------------------------------------------------------------ #
    lines += ["## Breakdown\n"]
    lines += _breakdown_table(
        settled, lambda r: r.get("category") or "unbekannt", "Nach Turnier-Kategorie"
    )
    lines += _breakdown_table(
        settled, lambda r: (r.get("surface") or "unbekannt").capitalize(), "Nach Surface"
    )
    lines += _breakdown_table(
        settled, lambda r: _mkt_label(r.get("market", "")), "Nach Markttyp"
    )
    lines += _breakdown_table(
        settled, lambda r: _ev_band(r.get("ev_pct", 0)), "Nach EV-Band"
    )
    lines += _breakdown_table(
        settled, lambda r: r.get("tournament") or "unbekannt", "Nach Turnier"
    )
    lines.append("")

    # ------------------------------------------------------------------ #
    # 4. Match-Log (neueste oben)
    # ------------------------------------------------------------------ #
    lines += ["## Match-Log\n"]
    if not settled:
        lines.append("_Noch keine abgeschlossenen Signale._\n")
    else:
        lines += [
            "| Datum | Turnier | Kat | Surf | Match | Markt | EV | @Odds | Outcome | P&L |",
            "|-------|---------|-----|------|-------|-------|----|-------|---------|-----|",
        ]
        icon = {"won": "✅", "lost": "❌", "push": "↩️"}
        for r in sorted(settled, key=lambda x: x.get("scan_date", ""), reverse=True):
            p = _pnl(r)
            p_str = f"{p:+.2f}€" if p is not None else "—"
            out_icon = icon.get(r["outcome"], "?")
            match_str = f"{r['home']} vs {r['away']}"
            # Kürzen damit Tabelle lesbar bleibt
            if len(match_str) > 32:
                parts = match_str.split(" vs ")
                match_str = f"{parts[0].split()[-1]} vs {parts[1].split()[-1]}"
            lines.append(
                f"| {r.get('scan_date','?')} "
                f"| {(r.get('tournament') or '?')[:18]} "
                f"| {(r.get('category') or '?')[:8]} "
                f"| {(r.get('surface') or '?')[:5].capitalize()} "
                f"| {match_str[:32]} "
                f"| {_mkt_label(r.get('market',''))[:14]} "
                f"| +{r.get('ev_pct',0):.0f}% "
                f"| @{r.get('decimal_odds',0):.2f} "
                f"| {out_icon} {r['outcome']} "
                f"| {p_str} |"
            )
        lines.append("")

    lines += ["---", "_SportsBrain Betting Journal — auto-generated_"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Betting Journal updaten")
    parser.add_argument("--sport", default="tennis", help="tennis / football / all")
    args = parser.parse_args()

    sport = None if args.sport == "all" else args.sport
    rows = _load_signals(sport)

    if not rows:
        print(f"[journal] Keine Signale für sport={sport!r}")
        return

    _update_snapshot(rows, sport)
    journal = build_journal(rows, sport)

    journal_path = _betting_journal_path()
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(journal, encoding="utf-8")
    s = _stats(rows)
    print(
        f"[journal] {journal_path.name} geschrieben — "
        f"{s['n_settled']} settled, {s['w']}W/{s['l']}L, "
        f"ROI {s['roi']:+.1f}%"
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Tennis-Odds-Coverage-Report — misst per-Turnier wie viele Matches
tatsächlich Marktquoten bekommen, wie viele nur Display-only (Modell-Implied)
und welche Quellen (TheOddsAPI / TE / OP / Betfair / WebSearch) liefern.

Nutzt den aktuellen Scanner-Output aus docs/data/signals.json (kein API-Call —
liest nur was der letzte Scan geschrieben hat).

Usage:
  python3 scripts/tennis_coverage_report.py
  python3 scripts/tennis_coverage_report.py --json
  python3 scripts/tennis_coverage_report.py --baseline results/tennis_coverage_baseline.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
SIGNALS = ROOT / "docs" / "data" / "signals.json"


def build_report() -> dict:
    if not SIGNALS.exists():
        raise SystemExit(f"signals.json fehlt: {SIGNALS}")
    data = json.loads(SIGNALS.read_text())
    schedule = [g for g in data.get("schedule", []) if g.get("sport") == "tennis"]

    per_tournament: dict[str, dict] = defaultdict(lambda: {
        "total": 0, "with_real_odds": 0, "display_only": 0, "sources": Counter(),
    })
    global_sources: Counter = Counter()
    total = len(schedule)
    with_real = 0
    display_only = 0

    for g in schedule:
        t = g.get("tournament", "?") or "?"
        src = g.get("odds_source") or "the_odds_api"
        d_only = bool(g.get("is_display_only", False))
        rec = per_tournament[t]
        rec["total"] += 1
        rec["sources"][src] += 1
        global_sources[src] += 1
        if d_only:
            rec["display_only"] += 1
            display_only += 1
        else:
            rec["with_real_odds"] += 1
            with_real += 1

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": {
            "matches": total,
            "with_real_odds": with_real,
            "display_only": display_only,
            "coverage_pct": round(100.0 * with_real / total, 1) if total else 0.0,
        },
        "sources": dict(global_sources.most_common()),
        "per_tournament": {
            t: {
                "total": r["total"],
                "with_real_odds": r["with_real_odds"],
                "display_only": r["display_only"],
                "coverage_pct": round(100.0 * r["with_real_odds"] / r["total"], 1),
                "sources": dict(r["sources"].most_common()),
            }
            for t, r in sorted(per_tournament.items(), key=lambda kv: -kv[1]["total"])
        },
    }


def render_text(rep: dict) -> str:
    tot = rep["totals"]
    lines = [
        f"Tennis-Coverage-Report ({rep['generated_at']})",
        f"═══════════════════════════════════════════════",
        f"Matches gesamt:    {tot['matches']}",
        f"Mit Marktquote:    {tot['with_real_odds']}  ({tot['coverage_pct']}%)",
        f"Display-only:      {tot['display_only']}",
        "",
        "Quellen-Verteilung:",
    ]
    for src, n in rep["sources"].items():
        lines.append(f"  {src:<20} {n}")
    lines += ["", "Pro Turnier:"]
    for t, r in rep["per_tournament"].items():
        lines.append(
            f"  {t[:38]:<38} {r['with_real_odds']:>3}/{r['total']:<3} "
            f"({r['coverage_pct']:>5.1f}%)  display-only={r['display_only']}"
        )
        for src, n in r["sources"].items():
            lines.append(f"      · {src}: {n}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true", help="JSON-Output statt Text")
    p.add_argument("--save", type=str, default="",
                   help="Report in Datei schreiben (z.B. results/tennis_coverage.json)")
    p.add_argument("--baseline", type=str, default="",
                   help="Delta gegen vorherigen JSON-Report anzeigen")
    args = p.parse_args()

    rep = build_report()

    if args.baseline:
        try:
            base = json.loads(Path(args.baseline).read_text())
            base_t = base["totals"]
            now_t = rep["totals"]
            print(f"Delta vs {args.baseline}:")
            print(f"  matches:        {base_t['matches']} → {now_t['matches']} "
                  f"({now_t['matches']-base_t['matches']:+d})")
            print(f"  with_real_odds: {base_t['with_real_odds']} → {now_t['with_real_odds']} "
                  f"({now_t['with_real_odds']-base_t['with_real_odds']:+d})")
            print(f"  coverage_pct:   {base_t['coverage_pct']:.1f} → {now_t['coverage_pct']:.1f} "
                  f"({now_t['coverage_pct']-base_t['coverage_pct']:+.1f})")
            print()
        except Exception as e:
            print(f"[baseline] Load failed: {e}\n")

    out = json.dumps(rep, indent=2, ensure_ascii=False) if args.json else render_text(rep)
    print(out)

    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(json.dumps(rep, indent=2, ensure_ascii=False))
        print(f"\nSaved → {args.save}")


if __name__ == "__main__":
    main()

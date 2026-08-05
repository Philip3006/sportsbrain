"""Automatischer Saisonbericht (N11) — P&L / Brier / CLV Report.

Generates results/season_report_{season}.md + results/season_report_{season}.json.

Usage:
  python3 scripts/generate_season_report.py [--season 2026] [--sport all|tennis|football]
  python3 scripts/generate_season_report.py --since 2026-06-01 --until 2026-10-31

Output:
  - Markdown report in results/
  - JSON summary for programmatic use
  - Prints a short digest to stdout

Intended to run at season end via GitHub Actions (schedule: yearly/manual dispatch).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import ledger_path_for, DEFAULT_USER


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_ledger(user: str = DEFAULT_USER) -> list[dict]:
    path = ledger_path_for(user)
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().split("T")[0]).date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _build_report(rows: list[dict], since: date | None, until: date | None, sport: str) -> dict[str, Any]:
    settled = []
    for r in rows:
        status = (r.get("status") or "").strip().lower()
        if status not in ("won", "lost", "void"):
            continue
        d = _parse_date(r.get("match_date") or r.get("placed_date"))
        if since and d and d < since:
            continue
        if until and d and d > until:
            continue
        if sport != "all":
            mkt = (r.get("market") or "").lower()
            src = (r.get("source") or "").lower()
            if sport == "tennis" and "tennis" not in src and "set" not in mkt and "games" not in mkt and "first_set" not in mkt:
                is_tennis = False
                src_field = (r.get("source") or "").lower()
                if "tennis" in src_field or "set" in mkt or "first_set" in mkt or "games" in mkt:
                    is_tennis = True
                if not is_tennis:
                    continue
            elif sport == "football" and ("tennis" in src or "set" in mkt or "games" in mkt):
                continue
        settled.append(r)

    n_won = sum(1 for r in settled if r.get("status","").lower() == "won")
    n_lost = sum(1 for r in settled if r.get("status","").lower() == "lost")
    n_void = sum(1 for r in settled if r.get("status","").lower() == "void")
    n_total = len(settled)

    def _f(v) -> float:
        try:
            return float(v) if v and v != "" else 0.0
        except (TypeError, ValueError):
            return 0.0

    total_staked = sum(_f(r.get("stake_amount")) for r in settled if r.get("status","").lower() != "void")
    total_pnl = sum(_f(r.get("pnl")) for r in settled)
    roi_pct = (total_pnl / total_staked * 100) if total_staked > 0 else 0.0
    win_rate = (n_won / (n_won + n_lost) * 100) if (n_won + n_lost) > 0 else 0.0

    # CLV stats
    clv_vals = [_f(r.get("clv")) for r in settled if r.get("clv") and r.get("clv") != "0.0" and r.get("clv") != ""]
    mean_clv = (sum(clv_vals) / len(clv_vals)) if clv_vals else None
    pct_positive_clv = (sum(1 for v in clv_vals if v > 0) / len(clv_vals) * 100) if clv_vals else None

    # Brier score (needs model_prob)
    brier_rows = []
    for r in settled:
        p = _f(r.get("model_prob"))
        if p <= 0 or p >= 1:
            continue
        outcome = 1.0 if r.get("status","").lower() == "won" else 0.0
        brier_rows.append((p - outcome) ** 2)
    brier_score = (sum(brier_rows) / len(brier_rows)) if brier_rows else None

    # By market
    by_market: dict[str, dict] = defaultdict(lambda: {"n": 0, "won": 0, "lost": 0, "staked": 0.0, "pnl": 0.0})
    for r in settled:
        mkt = r.get("market") or "unknown"
        bm = by_market[mkt]
        bm["n"] += 1
        st = r.get("status","").lower()
        if st == "won":
            bm["won"] += 1
        elif st == "lost":
            bm["lost"] += 1
        bm["staked"] += _f(r.get("stake_amount")) if st != "void" else 0.0
        bm["pnl"] += _f(r.get("pnl"))
    for mkt, bm in by_market.items():
        bm["roi_pct"] = (bm["pnl"] / bm["staked"] * 100) if bm["staked"] > 0 else 0.0
        bm["win_rate"] = (bm["won"] / (bm["won"] + bm["lost"]) * 100) if (bm["won"] + bm["lost"]) > 0 else 0.0

    # By confidence
    by_conf: dict[str, dict] = defaultdict(lambda: {"n": 0, "won": 0, "pnl": 0.0, "staked": 0.0})
    for r in settled:
        # infer confidence from stake_reason or source (best effort)
        conf = "MEDIUM"
        sr = (r.get("stake_reason") or "").upper()
        if "HIGH" in sr:
            conf = "HIGH"
        elif "LOW" in sr:
            conf = "LOW"
        bc = by_conf[conf]
        bc["n"] += 1
        st = r.get("status","").lower()
        if st == "won":
            bc["won"] += 1
        bc["pnl"] += _f(r.get("pnl"))
        bc["staked"] += _f(r.get("stake_amount")) if st != "void" else 0.0
    for conf, bc in by_conf.items():
        bc["roi_pct"] = (bc["pnl"] / bc["staked"] * 100) if bc["staked"] > 0 else 0.0

    # Weekly P&L timeline
    weekly: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "staked": 0.0})
    for r in settled:
        d = _parse_date(r.get("match_date") or r.get("placed_date"))
        if not d:
            continue
        iso_week = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        wk = weekly[iso_week]
        wk["n"] += 1
        wk["pnl"] += _f(r.get("pnl"))
        st = r.get("status","").lower()
        wk["staked"] += _f(r.get("stake_amount")) if st != "void" else 0.0
    # compute cumulative
    cum_pnl = 0.0
    for wk in sorted(weekly.keys()):
        cum_pnl += weekly[wk]["pnl"]
        weekly[wk]["cum_pnl"] = round(cum_pnl, 2)
        weekly[wk]["roi_pct"] = (
            weekly[wk]["pnl"] / weekly[wk]["staked"] * 100
            if weekly[wk]["staked"] > 0 else 0.0
        )

    return {
        "n_total": n_total,
        "n_won": n_won,
        "n_lost": n_lost,
        "n_void": n_void,
        "total_staked": round(total_staked, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round(roi_pct, 2),
        "win_rate": round(win_rate, 1),
        "mean_clv": round(mean_clv, 4) if mean_clv is not None else None,
        "pct_positive_clv": round(pct_positive_clv, 1) if pct_positive_clv is not None else None,
        "n_clv_readings": len(clv_vals),
        "brier_score": round(brier_score, 4) if brier_score is not None else None,
        "n_brier_readings": len(brier_rows),
        "by_market": {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv)
                          for kk, vv in v.items()}
                      for k, v in sorted(by_market.items(), key=lambda x: -x[1]["pnl"])},
        "by_confidence": dict(by_conf),
        "weekly": dict(sorted(weekly.items())),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _render_md(data: dict, season: str, sport: str, since: date | None, until: date | None) -> str:
    parts: list[str] = []
    parts.append(f"# Saisonbericht {season} — {sport.upper()}")
    if since or until:
        s = str(since) if since else "Anfang"
        u = str(until) if until else "heute"
        parts.append(f"**Zeitraum:** {s} → {u}")
    parts.append(f"_generiert: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_")
    parts.append("")

    # Overview
    parts.append("## Übersicht")
    parts.append(f"| Kennzahl | Wert |")
    parts.append(f"|----------|------|")
    parts.append(f"| Wetten total | {data['n_total']} |")
    parts.append(f"| Won / Lost / Void | {data['n_won']} / {data['n_lost']} / {data['n_void']} |")
    parts.append(f"| Win Rate | {data['win_rate']:.1f}% |")
    parts.append(f"| Total Staked | {data['total_staked']:+.2f} € |")
    parts.append(f"| Total P&L | {data['total_pnl']:+.2f} € |")
    parts.append(f"| ROI | {data['roi_pct']:+.2f}% |")
    if data.get("mean_clv") is not None:
        sign = "+" if data["mean_clv"] >= 0 else ""
        parts.append(f"| Ø CLV | {sign}{data['mean_clv']*100:.2f}% (n={data['n_clv_readings']}) |")
    if data.get("pct_positive_clv") is not None:
        parts.append(f"| CLV > 0 | {data['pct_positive_clv']:.1f}% der Wetten |")
    if data.get("brier_score") is not None:
        parts.append(f"| Brier Score | {data['brier_score']:.4f} (Baseline: 0.2500, n={data['n_brier_readings']}) |")
    parts.append("")

    # By market
    parts.append("## Ergebnisse nach Markt")
    parts.append("| Markt | n | W/L | ROI | P&L |")
    parts.append("|-------|---|-----|-----|-----|")
    for mkt, bm in data["by_market"].items():
        roi_str = f"{bm['roi_pct']:+.1f}%"
        wl = f"{bm['won']}/{bm['lost']}"
        parts.append(f"| {mkt} | {bm['n']} | {wl} | {roi_str} | {bm['pnl']:+.2f} € |")
    parts.append("")

    # By confidence
    parts.append("## Ergebnisse nach Konfidenz")
    parts.append("| Konfidenz | n | Won | ROI | P&L |")
    parts.append("|-----------|---|-----|-----|-----|")
    for conf in ["HIGH", "MEDIUM", "LOW"]:
        bc = data["by_confidence"].get(conf)
        if bc:
            parts.append(f"| {conf} | {bc['n']} | {bc['won']} | {bc.get('roi_pct',0):+.1f}% | {bc['pnl']:+.2f} € |")
    parts.append("")

    # Weekly timeline
    parts.append("## Wöchentliche Timeline")
    parts.append("| KW | n | P&L | Kumulativ |")
    parts.append("|----|---|-----|-----------|")
    for wk, wd in sorted(data["weekly"].items())[-20:]:  # last 20 weeks
        parts.append(f"| {wk} | {wd['n']} | {wd['pnl']:+.2f} € | {wd.get('cum_pnl',0):+.2f} € |")
    parts.append("")

    # Verdict
    parts.append("## Fazit")
    roi = data["roi_pct"]
    clv = data.get("mean_clv")
    if roi > 5 and (clv is None or clv > 0):
        verdict = "✅ Profitabler Saisonverlauf — Modell und Einsatzlogik funktionieren."
    elif roi > 0:
        verdict = "🟡 Leicht positiv — Stichprobengröße beachten, weiter tracken."
    elif roi > -5:
        verdict = "🟠 Leicht negativ — CLV/Brier prüfen, Gate-Parameter re-evaluieren."
    else:
        verdict = "🔴 Negativer Saisonverlauf — systematische Modell-Review empfohlen."
    parts.append(verdict)
    if clv is not None and clv < -0.02:
        parts.append("⚠️ Negative CLV — Odds-Timing prüfen (wir könnten nach smart money einsteigen).")
    if data.get("brier_score") is not None and data["brier_score"] > 0.27:
        parts.append("⚠️ Brier Score > 0.27 — Kalibrierung verschlechtert sich, Retrain prüfen.")

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="SportsBrain Saisonbericht (N11)")
    parser.add_argument("--season", default=str(datetime.now().year), help="Saison-Label (default: aktuelles Jahr)")
    parser.add_argument("--sport", choices=["all", "tennis", "football"], default="all")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD (inkl.)")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD (inkl.)")
    parser.add_argument("--user", default=DEFAULT_USER)
    args = parser.parse_args()

    since = _parse_date(args.since) if args.since else None
    until = _parse_date(args.until) if args.until else None

    rows = _load_ledger(args.user)
    if not rows:
        print("No ledger data found.")
        return 1

    data = _build_report(rows, since=since, until=until, sport=args.sport)
    md = _render_md(data, season=args.season, sport=args.sport, since=since, until=until)

    out_stem = f"season_report_{args.season}"
    if args.sport != "all":
        out_stem += f"_{args.sport}"

    md_path = ROOT / "results" / f"{out_stem}.md"
    json_path = ROOT / "results" / f"{out_stem}.json"

    md_path.write_text(md)
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    print(f"Saisonbericht {args.season} ({args.sport}) — {data['n_total']} Wetten")
    print(f"  P&L: {data['total_pnl']:+.2f} € | ROI: {data['roi_pct']:+.2f}% | WR: {data['win_rate']:.1f}%")
    if data.get("mean_clv") is not None:
        print(f"  Ø CLV: {data['mean_clv']*100:+.2f}% ({data['n_clv_readings']} Readings)")
    if data.get("brier_score") is not None:
        print(f"  Brier: {data['brier_score']:.4f}")
    print(f"  → {md_path.relative_to(ROOT)}")
    print(f"  → {json_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

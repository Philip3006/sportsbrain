"""Saisonbericht (N11 / O1-5 / O1-6) — P&L / Brier / LogLoss / CLV / ECE Report.

Generates results/season_report_{season}.md + results/season_report_{season}.json.

Usage:
  python3 scripts/generate_season_report.py [--season 2026] [--sport all|tennis|football]
  python3 scripts/generate_season_report.py --since 2026-06-01 --until 2026-10-31
  python3 scripts/generate_season_report.py --output-mode production   # production only

Output:
  - Markdown report in results/
  - JSON summary for programmatic use
  - Prints a short digest to stdout

Tier separation (O1-5 mandatory):
  production  — source='value', league != 'challenger_atp'
  manual      — source='manual'
  shadow      — league='challenger_atp' (excluded from all production metrics)

Calibration epoch: 2026-08-12 (SHA 57e93a68, P2 symmetric calibration)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import ledger_path_for, DEFAULT_USER

CALIBRATION_EPOCH = date(2026, 8, 12)  # P2 symmetric calibration — SHA 57e93a68
SIGNAL_HISTORY = ROOT / "data" / "cache" / "signal_history.jsonl"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_ledger(user: str = DEFAULT_USER) -> list[dict]:
    path = ledger_path_for(user)
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def _load_surface_map() -> dict[str, str]:
    """Returns {home|away|YYYY-MM-DD: surface} from signal_history.jsonl."""
    out: dict[str, str] = {}
    if not SIGNAL_HISTORY.exists():
        return out
    with SIGNAL_HISTORY.open() as f:
        for line in f:
            try:
                d = json.loads(line.strip())
                surface = d.get("surface", "")
                if surface:
                    dt = (d.get("match_date") or d.get("placed_date") or "")[:10]
                    key = f"{d.get('home', '')}|{d.get('away', '')}|{dt}"
                    out[key] = surface
            except (json.JSONDecodeError, AttributeError):
                pass
    return out


# ---------------------------------------------------------------------------
# Row classification helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().split("T")[0]).date()
    except ValueError:
        return None


def _f(v: Any) -> float:
    try:
        return float(v) if v and v != "" else 0.0
    except (TypeError, ValueError):
        return 0.0


def _bet_tier(row: dict) -> str:
    """Returns 'shadow', 'manual', or 'value' (production)."""
    if (row.get("league") or "").lower() == "challenger_atp":
        return "shadow"
    return "manual" if (row.get("source") or "").lower() == "manual" else "value"


def _is_tennis_row(row: dict) -> bool:
    mkt = (row.get("market") or "").lower()
    src = (row.get("source") or "").lower()
    league = (row.get("league") or "").lower()
    return (
        any(t in mkt for t in ("set", "games", "first_set", "score_"))
        or "tennis" in src
        or league in ("atp", "wta", "challenger_atp")
    )


def _surface_for(row: dict, surface_map: dict[str, str]) -> str:
    dt = (row.get("match_date") or row.get("placed_date") or "")[:10]
    return surface_map.get(f"{row.get('home', '')}|{row.get('away', '')}|{dt}", "unknown")


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _logloss(p: float, o: float) -> float:
    p = max(1e-7, min(1 - 1e-7, p))
    return -(o * math.log(p) + (1 - o) * math.log(1 - p))


def _ece(pairs: list[tuple[float, float]], n_bins: int = 10) -> float | None:
    if not pairs:
        return None
    bins: list[list] = [[] for _ in range(n_bins)]
    for p, o in pairs:
        bins[min(int(p * n_bins), n_bins - 1)].append((p, o))
    n = len(pairs)
    return sum(
        len(b) / n * abs(sum(x[0] for x in b) / len(b) - sum(x[1] for x in b) / len(b))
        for b in bins if b
    )


def _max_drawdown(pnl_seq: list[float]) -> float:
    peak = dd = cum = 0.0
    for v in pnl_seq:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


# ---------------------------------------------------------------------------
# Core metrics (operates on a pre-filtered list of settled rows)
# ---------------------------------------------------------------------------

def _compute_metrics(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"n_total": 0}
    n_won = sum(1 for r in rows if r.get("status", "").lower() == "won")
    n_lost = sum(1 for r in rows if r.get("status", "").lower() == "lost")
    n_void = sum(1 for r in rows if r.get("status", "").lower() == "void")
    non_void = [r for r in rows if r.get("status", "").lower() != "void"]

    total_staked = sum(_f(r.get("stake_amount")) for r in non_void)
    total_pnl = sum(_f(r.get("pnl")) for r in rows)
    roi_pct = (total_pnl / total_staked * 100) if total_staked > 0 else 0.0
    win_rate = (n_won / (n_won + n_lost) * 100) if (n_won + n_lost) > 0 else 0.0
    avg_odds = (sum(_f(r.get("decimal_odds")) for r in non_void) / len(non_void)) if non_void else None

    # CLV
    clv_rows = [r for r in non_void if r.get("clv") not in (None, "", "0.0", "0")]
    clv_vals = [_f(r.get("clv")) for r in clv_rows]
    mean_clv = sum(clv_vals) / len(clv_vals) if clv_vals else None
    clv_coverage = len(clv_rows) / len(non_void) * 100 if non_void else None
    clv_hit_rate = (sum(1 for v in clv_vals if v > 0) / len(clv_vals) * 100) if clv_vals else None

    # Calibration metrics (rows with valid model_prob only)
    cal_rows = [(r, _f(r.get("model_prob", ""))) for r in rows if 0 < _f(r.get("model_prob", "")) < 1]
    brier_vals, ll_vals, cal_pairs = [], [], []
    for r, p in cal_rows:
        o = 1.0 if r.get("status", "").lower() == "won" else 0.0
        brier_vals.append((p - o) ** 2)
        ll_vals.append(_logloss(p, o))
        cal_pairs.append((p, o))
    brier_score = sum(brier_vals) / len(brier_vals) if brier_vals else None
    log_loss = sum(ll_vals) / len(ll_vals) if ll_vals else None
    ece = _ece(cal_pairs)

    # Max drawdown (chronological P&L sequence)
    sorted_rows = sorted(rows, key=lambda r: r.get("match_date") or r.get("placed_date") or "")
    max_dd = _max_drawdown([_f(r.get("pnl")) for r in sorted_rows])

    return {
        "n_total": len(rows),
        "n_won": n_won,
        "n_lost": n_lost,
        "n_void": n_void,
        "total_staked": round(total_staked, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round(roi_pct, 2),
        "win_rate": round(win_rate, 1),
        "avg_odds": round(avg_odds, 3) if avg_odds is not None else None,
        "mean_clv": round(mean_clv, 4) if mean_clv is not None else None,
        "clv_coverage_pct": round(clv_coverage, 1) if clv_coverage is not None else None,
        "clv_hit_rate_pct": round(clv_hit_rate, 1) if clv_hit_rate is not None else None,
        "n_clv_readings": len(clv_vals),
        "brier_score": round(brier_score, 4) if brier_score is not None else None,
        "log_loss": round(log_loss, 4) if log_loss is not None else None,
        "ece": round(ece, 4) if ece is not None else None,
        "n_cal_readings": len(cal_rows),
        "max_drawdown": round(max_dd, 2),
    }


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_report(rows: list[dict], since: date | None, until: date | None,
                  sport: str, surface_map: dict) -> dict[str, Any]:
    # 1. Filter by date + sport
    filtered = []
    for r in rows:
        status = (r.get("status") or "").strip().lower()
        if status not in ("won", "lost", "void"):
            continue
        d = _parse_date(r.get("match_date") or r.get("placed_date"))
        if since and d and d < since:
            continue
        if until and d and d > until:
            continue
        if sport == "tennis" and not _is_tennis_row(r):
            continue
        if sport == "football" and _is_tennis_row(r):
            continue
        filtered.append(r)

    # 2. Tier separation
    production = [r for r in filtered if _bet_tier(r) == "value"]
    manual = [r for r in filtered if _bet_tier(r) == "manual"]
    shadow = [r for r in filtered if _bet_tier(r) == "shadow"]

    # 3. Calibration epoch split (production only)
    def _placed(r: dict) -> date:
        return _parse_date(r.get("placed_date")) or date.min
    pre_epoch = [r for r in production if _placed(r) < CALIBRATION_EPOCH]
    post_epoch = [r for r in production if _placed(r) >= CALIBRATION_EPOCH]

    # 4. By market (production)
    by_market: dict[str, dict] = defaultdict(lambda: {"n": 0, "won": 0, "lost": 0, "staked": 0.0, "pnl": 0.0})
    for r in production:
        mkt = r.get("market") or "unknown"
        bm = by_market[mkt]
        bm["n"] += 1
        st = r.get("status", "").lower()
        if st == "won":
            bm["won"] += 1
        elif st == "lost":
            bm["lost"] += 1
        bm["staked"] += _f(r.get("stake_amount")) if st != "void" else 0.0
        bm["pnl"] += _f(r.get("pnl"))
    for bm in by_market.values():
        bm["roi_pct"] = (bm["pnl"] / bm["staked"] * 100) if bm["staked"] > 0 else 0.0
        bm["win_rate"] = (bm["won"] / (bm["won"] + bm["lost"]) * 100) if (bm["won"] + bm["lost"]) > 0 else 0.0

    # 5. By confidence (production)
    by_conf: dict[str, dict] = defaultdict(lambda: {"n": 0, "won": 0, "pnl": 0.0, "staked": 0.0})
    for r in production:
        conf = "MEDIUM"
        sr = (r.get("stake_reason") or "").upper()
        if "HIGH" in sr:
            conf = "HIGH"
        elif "LOW" in sr:
            conf = "LOW"
        bc = by_conf[conf]
        bc["n"] += 1
        st = r.get("status", "").lower()
        if st == "won":
            bc["won"] += 1
        bc["pnl"] += _f(r.get("pnl"))
        bc["staked"] += _f(r.get("stake_amount")) if st != "void" else 0.0
    for bc in by_conf.values():
        bc["roi_pct"] = (bc["pnl"] / bc["staked"] * 100) if bc["staked"] > 0 else 0.0

    # 6. By surface — tennis production bets (via signal_history cross-ref)
    tennis_prod = [r for r in production if _is_tennis_row(r)]
    by_surface: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "staked": 0.0, "won": 0})
    for r in tennis_prod:
        surf = _surface_for(r, surface_map)
        bs = by_surface[surf]
        bs["n"] += 1
        bs["pnl"] += _f(r.get("pnl"))
        st = r.get("status", "").lower()
        bs["staked"] += _f(r.get("stake_amount")) if st != "void" else 0.0
        if st == "won":
            bs["won"] += 1
    for bs in by_surface.values():
        bs["roi_pct"] = (bs["pnl"] / bs["staked"] * 100) if bs["staked"] > 0 else 0.0

    # 7. Weekly timeline (production)
    weekly: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "staked": 0.0})
    for r in production:
        d = _parse_date(r.get("match_date") or r.get("placed_date"))
        if not d:
            continue
        iso_week = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        wk = weekly[iso_week]
        wk["n"] += 1
        wk["pnl"] += _f(r.get("pnl"))
        st = r.get("status", "").lower()
        wk["staked"] += _f(r.get("stake_amount")) if st != "void" else 0.0
    cum_pnl = 0.0
    for wk_key in sorted(weekly.keys()):
        cum_pnl += weekly[wk_key]["pnl"]
        weekly[wk_key]["cum_pnl"] = round(cum_pnl, 2)
        weekly[wk_key]["roi_pct"] = (
            weekly[wk_key]["pnl"] / weekly[wk_key]["staked"] * 100
            if weekly[wk_key]["staked"] > 0 else 0.0
        )

    return {
        "production": _compute_metrics(production),
        "manual": _compute_metrics(manual),
        "shadow": {"n_total": len(shadow), "note": "Shadow evaluations excluded from all production metrics."},
        "calibration_epoch": str(CALIBRATION_EPOCH),
        "pre_epoch": _compute_metrics(pre_epoch),
        "post_epoch": _compute_metrics(post_epoch),
        "n_no_model_prob": sum(1 for r in production if not _f(r.get("model_prob", ""))),
        "by_market": {
            k: {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()}
            for k, v in sorted(by_market.items(), key=lambda x: -x[1]["pnl"])
        },
        "by_confidence": dict(by_conf),
        "by_surface": {
            k: {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()}
            for k, v in sorted(by_surface.items(), key=lambda x: -x[1]["n"])
        },
        "weekly": dict(sorted(weekly.items())),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _metric_row(label: str, value: str) -> str:
    return f"| {label} | {value} |"


def _render_metrics_table(m: dict) -> list[str]:
    if not m.get("n_total"):
        return ["_(keine Daten)_"]
    parts = ["| Kennzahl | Wert |", "|----------|------|"]
    parts.append(_metric_row("n (gesamt)", str(m["n_total"])))
    parts.append(_metric_row("Won / Lost / Void", f"{m.get('n_won',0)} / {m.get('n_lost',0)} / {m.get('n_void',0)}"))
    parts.append(_metric_row("Win Rate", f"{m.get('win_rate', 0):.1f}%"))
    parts.append(_metric_row("Total Staked", f"{m.get('total_staked', 0):+.2f} €"))
    parts.append(_metric_row("Total P&L", f"{m.get('total_pnl', 0):+.2f} €"))
    parts.append(_metric_row("ROI", f"{m.get('roi_pct', 0):+.2f}%"))
    parts.append(_metric_row("Ø Odds", f"{m['avg_odds']:.3f}" if m.get("avg_odds") else "—"))
    parts.append(_metric_row("Max Drawdown", f"{m.get('max_drawdown', 0):.2f} €"))
    if m.get("mean_clv") is not None:
        sign = "+" if m["mean_clv"] >= 0 else ""
        parts.append(_metric_row("Ø CLV", f"{sign}{m['mean_clv']*100:.2f}% (n={m['n_clv_readings']})"))
    if m.get("clv_coverage_pct") is not None:
        parts.append(_metric_row("CLV Coverage", f"{m['clv_coverage_pct']:.1f}%"))
    if m.get("clv_hit_rate_pct") is not None:
        parts.append(_metric_row("CLV Hit Rate", f"{m['clv_hit_rate_pct']:.1f}%"))
    if m.get("brier_score") is not None:
        parts.append(_metric_row("Brier Score", f"{m['brier_score']:.4f} (baseline 0.2500, n={m['n_cal_readings']})"))
    if m.get("log_loss") is not None:
        parts.append(_metric_row("Log Loss", f"{m['log_loss']:.4f}"))
    if m.get("ece") is not None:
        parts.append(_metric_row("ECE", f"{m['ece']:.4f}"))
    return parts


def _render_md(data: dict, season: str, sport: str, since: date | None, until: date | None,
               output_mode: str = "full") -> str:
    parts: list[str] = []
    parts.append(f"# Saisonbericht {season} — {sport.upper()}")
    if since or until:
        s = str(since) if since else "Anfang"
        u = str(until) if until else "heute"
        parts.append(f"**Zeitraum:** {s} → {u}")
    parts.append(f"_generiert: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_")
    parts.append("")

    # Production
    parts.append("## 🟢 Production Bets")
    parts.extend(_render_metrics_table(data["production"]))
    parts.append("")

    # Calibration epoch split
    epoch = data["calibration_epoch"]
    m_pre = data["pre_epoch"]
    m_post = data["post_epoch"]
    parts.append(f"### Kalibrierungs-Epoch-Split (Grenze: {epoch} — SHA 57e93a68)")
    parts.append(f"| Kennzahl | Pre-Epoch (n={m_pre.get('n_total',0)}) | Post-Epoch (n={m_post.get('n_total',0)}) |")
    parts.append("|----------|---------|----------|")
    for label, key, fmt in [
        ("ROI", "roi_pct", "+.2f%"),
        ("Win Rate", "win_rate", ".1f%"),
        ("Brier", "brier_score", ".4f"),
        ("Log Loss", "log_loss", ".4f"),
        ("ECE", "ece", ".4f"),
        ("Ø CLV", "mean_clv", "×100+.2f%"),
        ("Max DD", "max_drawdown", ".2f€"),
    ]:
        def fmt_val(m: dict, k: str, f: str) -> str:
            v = m.get(k)
            if v is None:
                return "—"
            if f == "×100+.2f%":
                return f"{v*100:+.2f}%"
            if f.endswith("%"):
                return f"{v:{f[:-1]}}"
            if f.endswith("€"):
                return f"{v:.2f} €"
            return f"{v:{f}}"
        parts.append(f"| {label} | {fmt_val(m_pre, key, fmt)} | {fmt_val(m_post, key, fmt)} |")
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

    # By surface (tennis only, if data exists)
    if data["by_surface"]:
        parts.append("## Tennis — Ergebnisse nach Untergrund")
        parts.append("| Surface | n | Won | ROI | P&L |")
        parts.append("|---------|---|-----|-----|-----|")
        for surf, bs in sorted(data["by_surface"].items(), key=lambda x: -x[1]["n"]):
            parts.append(f"| {surf} | {bs['n']} | {bs['won']} | {bs.get('roi_pct',0):+.1f}% | {bs['pnl']:+.2f} € |")
        parts.append("")

    # Weekly
    parts.append("## Wöchentliche Timeline")
    parts.append("| KW | n | P&L | Kumulativ |")
    parts.append("|----|---|-----|-----------|")
    for wk, wd in sorted(data["weekly"].items())[-20:]:
        parts.append(f"| {wk} | {wd['n']} | {wd['pnl']:+.2f} € | {wd.get('cum_pnl',0):+.2f} € |")
    parts.append("")

    if output_mode == "full":
        # Manual bets
        m = data["manual"]
        if m.get("n_total"):
            parts.append(f"## 🔵 Manual Bets (n={m['n_total']}, excluded from production metrics)")
            parts.append(f"P&L: {m['total_pnl']:+.2f} € | ROI: {m['roi_pct']:+.2f}% | WR: {m['win_rate']:.1f}%")
            parts.append("")
        # Shadow
        sh = data["shadow"]
        if sh["n_total"]:
            parts.append(f"## 🟡 Shadow Evaluations (n={sh['n_total']}) — never mixed into production")
        else:
            parts.append("## 🟡 Shadow Evaluations — keine settled Bets (Challenger shadow program aktiv)")
        parts.append("")
        # Exclusions note
        n_no_mp = data.get("n_no_model_prob", 0)
        if n_no_mp:
            parts.append(f"_Hinweis: {n_no_mp} production rows ohne model_prob → aus Brier/LogLoss/ECE ausgeschlossen._")
        parts.append("")

    # Verdict
    parts.append("## Fazit")
    prod = data["production"]
    roi = prod.get("roi_pct", 0)
    clv = prod.get("mean_clv")
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
    brier = prod.get("brier_score")
    if brier is not None and brier > 0.27:
        parts.append("⚠️ Brier Score > 0.27 — Kalibrierung verschlechtert sich, Retrain prüfen.")
    ece = prod.get("ece")
    if ece is not None and ece > 0.05:
        parts.append(f"⚠️ ECE = {ece:.4f} (>5%) — Kalibrierungsfehler erhöht, Isotonic-Recalibrator prüfen.")

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="SportsBrain Saisonbericht (N11 / O1-5 / O1-6)")
    parser.add_argument("--season", default=str(datetime.now().year), help="Saison-Label (default: aktuelles Jahr)")
    parser.add_argument("--sport", choices=["all", "tennis", "football"], default="all")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD (inkl.)")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD (inkl.)")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--output-mode", choices=["full", "production"], default="full",
                        help="full: all tiers; production: production metrics only")
    args = parser.parse_args()

    since = _parse_date(args.since) if args.since else None
    until = _parse_date(args.until) if args.until else None

    rows = _load_ledger(args.user)
    if not rows:
        print("No ledger data found.")
        return 1

    surface_map = _load_surface_map()
    data = _build_report(rows, since=since, until=until, sport=args.sport, surface_map=surface_map)
    md = _render_md(data, season=args.season, sport=args.sport, since=since, until=until,
                    output_mode=args.output_mode)

    out_stem = f"season_report_{args.season}"
    if args.sport != "all":
        out_stem += f"_{args.sport}"
    if args.output_mode == "production":
        out_stem += "_production"

    md_path = ROOT / "results" / f"{out_stem}.md"
    json_path = ROOT / "results" / f"{out_stem}.json"
    md_path.write_text(md)
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    prod = data["production"]
    print(f"Saisonbericht {args.season} ({args.sport}) — {prod.get('n_total', 0)} production bets")
    print(f"  P&L: {prod.get('total_pnl', 0):+.2f} € | ROI: {prod.get('roi_pct', 0):+.2f}% | WR: {prod.get('win_rate', 0):.1f}%")
    if prod.get("mean_clv") is not None:
        print(f"  Ø CLV: {prod['mean_clv']*100:+.2f}% ({prod['n_clv_readings']} readings, "
              f"coverage={prod.get('clv_coverage_pct',0):.0f}%, hit={prod.get('clv_hit_rate_pct',0):.0f}%)")
    if prod.get("brier_score") is not None:
        print(f"  Brier: {prod['brier_score']:.4f} | LogLoss: {prod.get('log_loss','—')} | ECE: {prod.get('ece','—')}")
    print(f"  Max DD: {prod.get('max_drawdown', 0):.2f} € | Ø Odds: {prod.get('avg_odds','—')}")
    manual = data["manual"]
    if manual.get("n_total"):
        print(f"  Manual: {manual['n_total']} bets, P&L {manual['total_pnl']:+.2f} €")
    print(f"  → {md_path.relative_to(ROOT)}")
    print(f"  → {json_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

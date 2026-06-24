#!/usr/bin/env python3
"""Tennis-Gate-Review (Roadmap J2-F).

Liest results/ledger_{user}.csv → aggregiert Live-ROI pro Kategorie und
vergleicht gegen Backtest-Erwartung (results/audits/tennis_j2_backtest_*.md).
Output: results/audits/tennis_gate_review_<date>.md.

Verwendung nach 2 Wochen Live-Betrieb:
  python3 scripts/tennis_gate_review.py --user philip --backtest results/audits/tennis_j2_backtest_2026-06-23.md

Liefert Empfehlungen:
  - SHADOW → LIVE: Kategorie hat Backtest-Gate gerissen UND Live-ROI ≥ 3pp besser als erwartet
  - LIVE → SHADOW: Kategorie hat Backtest-Gate gerissen ABER Live-ROI ≥ 5pp schlechter
  - SHADOW → BLACKLIST: Live-ROI ≤ -5% nach n≥30 Bets
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from src.betting.ledger import _resolve_ledger_path
from src.config import DEFAULT_USER, TENNIS_CATEGORY_MODE


def load_live_bets(user: str, days: int = 30) -> pd.DataFrame:
    """Lädt settled Tennis-Bets der letzten N Tage aus Ledger."""
    path = _resolve_ledger_path(user)
    if not path.exists():
        print(f"[gate_review] Ledger {path} nicht vorhanden.")
        return pd.DataFrame()
    df = pd.read_csv(path)
    if df.empty:
        return df

    # Tennis-Filter: Markt-Pattern (kein Football-1x2/o/u2.5/btts/etc.)
    tennis_markets = {"home", "away", "ah-1.5_a", "ah+1.5_b",
                      "first_set_a", "first_set_b"}
    is_tennis = df["market"].isin(tennis_markets)
    is_tennis |= df["market"].str.startswith("o/u_sets_", na=False)
    is_tennis |= df["market"].str.startswith("o/u_games_", na=False)
    is_tennis |= df["market"].str.startswith("score_", na=False)
    df = df[is_tennis].copy()

    if "placed_date" in df.columns:
        cutoff = pd.Timestamp.today() - pd.Timedelta(days=days)
        df["placed_date"] = pd.to_datetime(df["placed_date"], errors="coerce")
        df = df[df["placed_date"] >= cutoff]

    df = df[df["status"].isin(["won", "lost", "void"])]
    return df


def parse_backtest_md(path: Path) -> dict[str, dict]:
    """Parsed Per-Category-Verdict-Tabelle aus tennis_j2_backtest_*.md."""
    if not path.exists():
        return {}
    text = path.read_text()
    # Tabellenformat: | Kategorie | Tour | N | Hit% | ROI | Brier | Verdict |
    out: dict[str, dict] = {}
    in_section = False
    for line in text.splitlines():
        if "Gate-Verdict pro Kategorie" in line:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 7 or cells[0] == "Kategorie" or "---" in cells[0]:
            continue
        cat = cells[0]
        try:
            n = int(cells[2])
            roi_pct = float(re.sub(r"[^\d.\-+]", "", cells[4]))
            out[cat] = {"n": n, "roi": roi_pct / 100.0, "verdict": cells[6]}
        except (ValueError, IndexError):
            continue
    return out


def build_review(
    live: pd.DataFrame,
    backtest_map: dict[str, dict],
) -> dict[str, dict]:
    """Pro Kategorie: Live-ROI, Backtest-ROI, Empfehlung."""
    review: dict[str, dict] = {}
    # Live: Live-Markt-→-Category-Lookup ist heuristisch (Markt-Prefix nicht 1:1
    # zu Registry-Kategorie). Pragmatisch: alle Live-Bets als ein Bucket bewerten.
    if "category" in live.columns:
        groups = live.groupby("category")
    else:
        groups = [("all", live)]

    for cat, grp in groups:
        n = len(grp)
        if n == 0:
            continue
        st = grp["stake"].sum() if "stake" in grp.columns else 0.0
        pnl = grp["pnl"].sum() if "pnl" in grp.columns else 0.0
        live_roi = pnl / st if st > 0 else 0.0
        bt = backtest_map.get(cat, {})
        current_mode = TENNIS_CATEGORY_MODE.get(cat, "shadow")
        recommendation = _recommend(cat, current_mode, n, live_roi, bt)
        review[cat] = {
            "n": n, "live_roi": live_roi,
            "backtest_roi": bt.get("roi"),
            "backtest_n": bt.get("n"),
            "current_mode": current_mode,
            "recommendation": recommendation,
        }
    return review


def _recommend(cat: str, current_mode: str, n: int, live_roi: float, bt: dict) -> str:
    """Heuristische Empfehlung. Konservativ — bevorzugt Status-Quo bei dünner Datenlage."""
    if n < 30:
        return "KEEP (n<30, zu wenig Daten)"

    if live_roi <= -0.05 and n >= 30:
        return "BLACKLIST (Live ROI ≤ -5%)"

    bt_roi = bt.get("roi")
    if bt_roi is None:
        return "KEEP (kein Backtest-Vergleich)"

    diff = live_roi - bt_roi
    if current_mode == "shadow":
        if live_roi >= 0.03 and diff >= -0.05:
            return "PROMOTE shadow→live (Live ROI ≥ 3% UND nicht schlechter als Backtest)"
        return f"KEEP shadow (Live ROI {live_roi*100:+.1f}% vs Backtest {bt_roi*100:+.1f}%)"
    # live
    if diff <= -0.05:
        return f"DEMOTE live→shadow (Live ROI {diff*100:+.1f}pp schlechter als Backtest)"
    return f"KEEP live (Live ROI {live_roi*100:+.1f}% vs Backtest {bt_roi*100:+.1f}%)"


def write_review_md(review: dict[str, dict], out_path: Path) -> None:
    lines = [
        f"# Tennis Gate-Review — {date.today().isoformat()}",
        "",
        "| Kategorie | N (live) | ROI live | ROI backtest | Mode | Empfehlung |",
        "|---|---:|---:|---:|---|---|",
    ]
    for cat, r in sorted(review.items()):
        bt_roi_str = f"{r['backtest_roi']*100:+.1f}%" if r["backtest_roi"] is not None else "—"
        lines.append(
            f"| {cat} | {r['n']} | {r['live_roi']*100:+.1f}% | {bt_roi_str} | "
            f"{r['current_mode']} | {r['recommendation']} |"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Tennis J2-F Gate-Review")
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--days", type=int, default=30,
                        help="Live-Bets der letzten N Tage betrachten (default 30)")
    parser.add_argument("--backtest", type=str, default=None,
                        help="Pfad zur tennis_j2_backtest_*.md Datei")
    args = parser.parse_args()

    live = load_live_bets(args.user, days=args.days)
    print(f"Live-Bets gefunden: {len(live)}")

    backtest_map = {}
    if args.backtest:
        backtest_map = parse_backtest_md(Path(args.backtest))
        print(f"Backtest-Categories: {len(backtest_map)}")

    review = build_review(live, backtest_map)
    out = ROOT / "results" / "audits" / f"tennis_gate_review_{date.today().isoformat()}.md"
    write_review_md(review, out)
    print(f"Review: {out}")

    for cat, r in review.items():
        print(f"  {cat:>12s} n={r['n']:>3d}  live={r['live_roi']*100:+.1f}%  → {r['recommendation']}")


if __name__ == "__main__":
    main()

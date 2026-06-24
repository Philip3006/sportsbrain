#!/usr/bin/env python3
"""Comprehensive Tennis Backtest (Roadmap J2-G).

Aggregiert in einem Lauf:
  1. Match-Winner ROI-Backtest (echte tennis-data.co.uk-Quoten)
  2. Set-Markt-Kalibrierung (Brier/Hit% — keine historischen Quoten verfügbar)
  3. Game-Markt-Kalibrierung (Brier/Hit% — synthetisch via simulate_match)

Output: results/audits/tennis_full_backtest_<DATUM>.md mit 4 Sektionen +
Empfehlung für TENNIS_CATEGORY_MODE.

Usage:
  python3 scripts/tennis_full_backtest.py --years 2019-2025 --tour both --save
  python3 scripts/tennis_full_backtest.py --dry-run   # nutzt cache, kein Netzwerk
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from scripts.tennis_backtest import (
    _build_walkforward_elo,
    _brier,
    _norm,
    _predict_from_snapshot,
    _category_verdict,
)
from src.betting.kelly import dynamic_stake_eur, expected_value, kelly_fraction
from src.config import (
    MAX_EV, MIN_EDGE, TENNIS_CATEGORY_MODE, TENNIS_MIN_EDGE_BY_CATEGORY,
)
from src.data.tennis_odds import fetch_full_tour_odds
from src.tennis.calibration import (
    evaluate_set_markets,
    evaluate_game_markets,
)


# ---------------------------------------------------------------------------
# Sackmann-Adapter: tennis-data XLSX → Sackmann-Schema
# (Sackmann GitHub-Repo ist nicht mehr öffentlich — 2026-06-24)
# ---------------------------------------------------------------------------

# Series-Spalte → tourney_level für Elo-K-Faktor
_SERIES_TO_LEVEL = {
    "Grand Slam": "G", "Masters 1000": "M", "Masters Cup": "F",
    "ATP Finals": "F", "Tour Championships": "F", "WTA Finals": "F",
    "ATP500": "A", "ATP250": "A", "International Gold": "A",
    "Premier Mandatory": "P", "Premier 5": "P", "Premier": "P",
    "WTA1000": "P", "WTA500": "P", "WTA250": "P", "International": "I",
}


def _xlsx_to_sackmann_schema(odds_df) -> "pd.DataFrame":
    """Konvertiert tennis-data XLSX-DataFrame in Sackmann-Spaltennamen.

    Sackmann nutzt: tourney_date, tourney_name, tourney_level, surface,
    winner_name, loser_name. Tennis-data hat: Date, Tournament, Series,
    Surface, Winner, Loser. Wir mappen 1:1, damit _build_walkforward_elo
    direkt funktioniert.
    """
    out = odds_df.rename(columns={
        "Date": "tourney_date",
        "Tournament": "tourney_name",
        "Winner": "winner_name",
        "Loser": "loser_name",
    }).copy()
    out["surface"] = out.get("surface_std", "hard").astype(str).str.lower()
    if "Series" in out.columns:
        out["tourney_level"] = out["Series"].map(
            lambda s: _SERIES_TO_LEVEL.get(str(s).strip(), "A")
        )
    else:
        out["tourney_level"] = "A"
    return out


def run_match_winner_backtest(
    tours: list[str],
    years: range,
    min_year: int = 2021,
    odds_source: str = "max",
    min_prob: float = 0.35,
) -> "pd.DataFrame":
    """Match-Winner-Backtest direkt aus tennis-data XLSX (ohne Sackmann)."""
    print("[match_winner] Lade Full-Tour-XLSX …")
    odds_df = fetch_full_tour_odds(tours=tours, years=years)
    if odds_df.empty:
        print("  Keine Odds-Daten verfügbar.")
        return pd.DataFrame()
    print(f"  {len(odds_df)} Match-Zeilen")

    print("[match_winner] Build walk-forward Elo aus XLSX …")
    sack_like = _xlsx_to_sackmann_schema(odds_df)
    snapshots = _build_walkforward_elo(sack_like, snapshot_all_events=True)
    print(f"  {len(snapshots)} Snapshots")

    # Filter Test-Window
    odds_df = odds_df[odds_df["Date"].dt.year >= min_year].copy()
    print(f"  {len(odds_df)} Matches im Test-Window (ab {min_year})")

    odds_cols = {"b365": ("B365W", "B365L"), "avg": ("AvgW", "AvgL"), "max": ("MaxW", "MaxL")}
    w_col, l_col = odds_cols.get(odds_source, odds_cols["max"])
    records = []
    matched = 0

    for _, row in odds_df.iterrows():
        winner = _norm(str(row["Winner"]))
        loser = _norm(str(row["Loser"]))
        try:
            odds_w = float(row[w_col]); odds_l = float(row[l_col])
        except (KeyError, ValueError, TypeError):
            continue
        if pd.isna(odds_w) or pd.isna(odds_l):
            continue
        surface = str(row.get("surface_std", "hard"))
        tournament_key = str(row.get("Tournament", "")).strip().lower()
        match_year = row["Date"].year
        snap = snapshots.get((winner, loser, tournament_key, match_year))
        if snap is None:
            continue
        matched += 1
        category = str(row.get("category", "atp250"))
        tour_r = str(row.get("tour", "ATP"))

        p_w, p_l = _predict_from_snapshot(snap, surface)
        cat_min_edge = TENNIS_MIN_EDGE_BY_CATEGORY.get(category, MIN_EDGE)

        for side, model_p, odds, won in [
            ("winner", p_w, odds_w, True),
            ("loser", p_l, odds_l, False),
        ]:
            if model_p < min_prob:
                continue
            ev = expected_value(model_p, odds)
            if ev < cat_min_edge or ev > MAX_EV:
                continue
            kf = kelly_fraction(model_p, odds)
            stake = dynamic_stake_eur(ev, "MEDIUM")
            pnl = stake * (odds - 1) if won else -stake
            records.append({
                "date": str(row["Date"].date()), "year": match_year,
                "tour": tour_r, "tournament": row.get("Tournament", ""),
                "category": category, "surface": surface,
                "winner": winner, "loser": loser, "side": side,
                "model_prob": round(model_p, 4), "market_odds": odds,
                "ev": round(ev, 4), "kelly_f": round(kf, 4),
                "stake": round(stake, 2), "won": int(won),
                "pnl": round(pnl, 2),
            })

    print(f"  Snapshot-Matches: {matched} / {len(odds_df)}")
    print(f"  Value-Bets: {len(records)}")
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Calibration-Loop (Sektionen 2 + 3 des Reports)
# ---------------------------------------------------------------------------

def run_calibration_loop(
    tours: list[str],
    years: range,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Iteriert über XLSX-Matches → leitet Modell-Wkten ab → vergleicht mit Outcomes.

    Returns: (set_market_df, game_market_df) je mit Spalten
        category, tour, surface, market, model_p, actual, brier_term
    """
    print("[calibration] Lade Full-Tour-XLSX …")
    odds_df = fetch_full_tour_odds(tours=tours, years=years)
    if odds_df.empty:
        return pd.DataFrame(), pd.DataFrame()
    print(f"  {len(odds_df)} Match-Zeilen")

    print("[calibration] Build walk-forward Elo aus XLSX …")
    sack_like = _xlsx_to_sackmann_schema(odds_df)
    snapshots = _build_walkforward_elo(sack_like, snapshot_all_events=True)
    print(f"  {len(snapshots)} snapshots")

    odds_df = odds_df[odds_df["Date"].dt.year >= years.start].copy()

    set_rows: list[dict] = []
    game_rows: list[dict] = []
    matched = 0

    for _, row in odds_df.iterrows():
        winner = _norm(str(row["Winner"]))
        loser = _norm(str(row["Loser"]))
        tournament_key = str(row.get("Tournament", "")).strip().lower()
        match_year = row["Date"].year
        snap = snapshots.get((winner, loser, tournament_key, match_year))
        if snap is None:
            continue
        matched += 1
        surface = str(row.get("surface_std", "hard"))
        try:
            best_of = int(row.get("Best of", 3))
        except (TypeError, ValueError):
            best_of = 3
        try:
            wsets = int(row["Wsets"])
            lsets = int(row["Lsets"])
        except (KeyError, ValueError, TypeError):
            continue
        if wsets + lsets < 2:  # Walkover / Retirement
            continue

        category = str(row.get("category", "atp250"))
        tour_r = str(row.get("tour", "ATP"))

        # Modell p_match aus Snapshot (Sicht: Winner = Spieler A)
        p_w, _ = _predict_from_snapshot(snap, surface)

        # Set-Märkte
        set_eval = evaluate_set_markets(p_w, best_of, wsets, lsets)
        for market, m in set_eval.items():
            set_rows.append({
                "category": category, "tour": tour_r, "surface": surface,
                "market": market, **m,
            })

        # Game-Märkte (nur wenn W1..L5 vorhanden)
        game_eval = evaluate_game_markets(p_w, best_of, tour_r.lower(), row)
        if game_eval is not None:
            for market, m in game_eval.items():
                game_rows.append({
                    "category": category, "tour": tour_r, "surface": surface,
                    "market": market, **m,
                })

    print(f"  Snapshot-Matches: {matched} / {len(odds_df)}")
    print(f"  Set-Markt-Rows: {len(set_rows)}")
    print(f"  Game-Markt-Rows: {len(game_rows)}")
    return pd.DataFrame(set_rows), pd.DataFrame(game_rows)


# ---------------------------------------------------------------------------
# Report-Aggregation
# ---------------------------------------------------------------------------

_BRIER_KALIBRIERT = 0.245


def _aggregate_match_winner(mw_df: pd.DataFrame) -> list[dict]:
    """Sektion 1: ROI pro (category, tour, surface)."""
    if mw_df.empty:
        return []
    out = []
    for (cat, tour, surf), grp in mw_df.groupby(["category", "tour", "surface"]):
        n = len(grp)
        st = grp["stake"].sum()
        pnl = grp["pnl"].sum()
        roi = pnl / st if st > 0 else 0
        hit = grp["won"].mean()
        br = _brier(grp)
        v = _category_verdict(n, roi)
        out.append({
            "category": cat, "tour": tour, "surface": surf,
            "n": n, "hit": hit, "roi": roi, "brier": br, "verdict": v,
        })
    return sorted(out, key=lambda r: (r["category"], r["tour"], r["surface"]))


def _aggregate_calibration(df: pd.DataFrame, group_market: bool = True) -> list[dict]:
    """Sektionen 2/3: Brier + Hit% pro (category, tour, surface, market)."""
    if df.empty:
        return []
    keys = ["category", "tour", "surface"]
    if group_market:
        keys.append("market")
    out = []
    for combo, grp in df.groupby(keys):
        if not isinstance(combo, tuple):
            combo = (combo,)
        n = len(grp)
        brier = grp["brier_term"].mean()
        # Hit-Rate nur sinnvoll für binäre Märkte (over/under). Für Scorelines
        # ist actual=1 sehr selten — reportieren wir Mean-Model-P statt Hit.
        if any("score_" in str(m) for m in grp["market"].unique()):
            mean_p = grp["model_p"].mean()
            hit = None
        else:
            mean_p = grp["model_p"].mean()
            hit = grp["actual"].mean()
        row = {k: v for k, v in zip(keys, combo)}
        row.update({
            "n": n,
            "hit": hit,
            "mean_p": mean_p,
            "brier": brier,
            "kalibriert": brier < _BRIER_KALIBRIERT,
        })
        out.append(row)
    return sorted(out, key=lambda r: tuple(r.get(k, "") for k in keys))


def _format_pct(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{x * 100:+.{digits}f}%" if digits == 1 else f"{x * 100:.{digits}f}%"


def write_report(
    mw_agg: list[dict],
    set_agg: list[dict],
    game_agg: list[dict],
    out_path: Path,
) -> None:
    lines: list[str] = []
    lines.append(f"# Tennis Full Backtest — {date.today().isoformat()}")
    lines.append("")
    lines.append("Generiert von `scripts/tennis_full_backtest.py`. "
                 "Datenbasis: tennis-data.co.uk Full-Tour-XLSX (Match-Outcomes + B365/Avg/Max-Odds + per-Set-Game-Scores). "
                 "Elo wird walk-forward aus denselben XLSX-Daten aufgebaut (Sackmann-Repos sind ab 2026-06 nicht mehr öffentlich verfügbar).")
    lines.append("")

    # --- Sektion 1: Match Winner ROI ---
    lines.append("## 1. Match Winner (ROI-validiert)")
    lines.append("")
    lines.append("Live-Gate: ROI≥3% bei n≥50 ODER ROI≥5% bei n≥30. BLACKLIST: ROI≤-5%.")
    lines.append("")
    lines.append("| Kategorie | Tour | Surface | N | Hit% | ROI | Brier | Verdict |")
    lines.append("|---|---|---|---:|---:|---:|---:|---|")
    for r in mw_agg:
        emoji = {"LIVE": "✅", "SHADOW": "⚠️", "BLACKLIST": "🚫"}.get(r["verdict"], "?")
        lines.append(
            f"| {r['category']} | {r['tour']} | {r['surface']} | {r['n']} | "
            f"{r['hit']*100:.1f}% | {r['roi']*100:+.1f}% | {r['brier']:.4f} | "
            f"{emoji} {r['verdict']} |"
        )

    # --- Sektion 2: Set-Märkte ---
    lines.append("")
    lines.append("## 2. Set-Märkte Kalibrierung (Brier, keine ROI — keine historischen Quoten)")
    lines.append("")
    lines.append(f"Kalibriert: Brier < {_BRIER_KALIBRIERT}")
    lines.append("")
    lines.append("| Kategorie | Tour | Surface | Markt | N | Hit% | Brier | Kalibriert? |")
    lines.append("|---|---|---|---|---:|---:|---:|---|")
    for r in set_agg:
        kal = "✅" if r["kalibriert"] else "⚠️"
        hit_s = f"{r['hit']*100:.1f}%" if r["hit"] is not None else "—"
        lines.append(
            f"| {r['category']} | {r['tour']} | {r['surface']} | {r['market']} | "
            f"{r['n']} | {hit_s} | {r['brier']:.4f} | {kal} |"
        )

    # --- Sektion 3: Game-Märkte ---
    lines.append("")
    lines.append("## 3. Game-Märkte Kalibrierung (Brier, MC-Sim)")
    lines.append("")
    if not game_agg:
        lines.append("_Keine Game-Score-Daten in den XLSX-Files gefunden (W1..L5 fehlen)._")
    else:
        lines.append("| Kategorie | Tour | Surface | Markt | N | Hit% | Brier | Kalibriert? |")
        lines.append("|---|---|---|---|---:|---:|---:|---|")
        for r in game_agg:
            kal = "✅" if r["kalibriert"] else "⚠️"
            hit_s = f"{r['hit']*100:.1f}%" if r["hit"] is not None else "—"
            lines.append(
                f"| {r['category']} | {r['tour']} | {r['surface']} | {r['market']} | "
                f"{r['n']} | {hit_s} | {r['brier']:.4f} | {kal} |"
            )

    # --- Sektion 4: Empfehlung TENNIS_CATEGORY_MODE ---
    lines.append("")
    lines.append("## 4. Empfehlung TENNIS_CATEGORY_MODE")
    lines.append("")
    lines.append("| Kategorie | Aktuell | Empfehlung | Quelle |")
    lines.append("|---|---|---|---|")
    # Aggregiere Match-Winner pro category (über Tour+Surface gemittelt)
    by_cat: dict[str, list[dict]] = {}
    for r in mw_agg:
        by_cat.setdefault(r["category"], []).append(r)
    for cat in sorted(set(list(TENNIS_CATEGORY_MODE.keys()) + list(by_cat.keys()))):
        current = TENNIS_CATEGORY_MODE.get(cat, "shadow")
        rows = by_cat.get(cat, [])
        n_total = sum(r["n"] for r in rows)
        if n_total == 0:
            rec = "KEEP shadow (keine Backtest-Daten)"
            src = "—"
        else:
            staked = sum(r["n"] for r in rows)
            roi_w = sum(r["roi"] * r["n"] for r in rows) / staked
            verdicts = [r["verdict"] for r in rows]
            if "LIVE" in verdicts and roi_w >= 0.03:
                rec = "PROMOTE → live" if current != "live" else "KEEP live"
            elif roi_w <= -0.05:
                rec = "BLACKLIST"
            else:
                rec = "KEEP shadow"
            src = f"n={n_total}, gewichtete ROI={roi_w*100:+.1f}%"
        lines.append(f"| {cat} | {current} | {rec} | {src} |")

    # --- Sektion 4b: Surface-aware Live-Liste ---
    lines.append("")
    lines.append("### 4b. Surface-aware LIVE-Kombinationen (für künftige TENNIS_CATEGORY_SURFACE_MODE)")
    lines.append("")
    lines.append("| Kategorie | Tour | Surface | N | ROI | Verdict |")
    lines.append("|---|---|---|---:|---:|---|")
    for r in mw_agg:
        if r["verdict"] == "LIVE":
            lines.append(
                f"| {r['category']} | {r['tour']} | {r['surface']} | {r['n']} | "
                f"{r['roi']*100:+.1f}% | ✅ LIVE |"
            )
    lines.append("")
    lines.append("**Hinweis**: Aktuelle `TENNIS_CATEGORY_MODE` gruppiert nur nach Kategorie. "
                 "Für surface-präzise Schaltung müsste ein neues `TENNIS_CATEGORY_SURFACE_MODE` "
                 "eingeführt werden (Roadmap-Item, vermutlich J2-H).")

    # --- Sektion 5: Markt-Aktivierungs-Heuristik ---
    lines.append("")
    lines.append("## 5. Markt-Aktivierungs-Heuristik")
    lines.append("")
    lines.append("- **Match Winner**: Live = Sektion-1-Verdict pro (cat, tour, surface).")
    lines.append("- **Set-Märkte** (O/U Sets, Set Betting): bleiben SHADOW solange Brier-Kalibrierung nicht via 30+ Live-Bets bestätigt (siehe `scripts/tennis_gate_review.py`).")
    lines.append("- **Game-Märkte** (O/U Games): wie Set-Märkte, konservativer da MC-Sim Hold-Approximation nutzt.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_years(spec: str) -> range:
    if "-" in spec:
        a, b = spec.split("-")
        return range(int(a), int(b) + 1)
    return range(int(spec), int(spec) + 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tennis J2-G Full Backtest")
    parser.add_argument("--tour", default="both", choices=["atp", "wta", "both"])
    parser.add_argument("--years", default="2019-2025",
                        help="z.B. 2019-2025 oder 2024")
    parser.add_argument("--from-year", type=int, default=2021,
                        help="Match-Winner-Backtest Test-Window-Start")
    parser.add_argument("--save", action="store_true",
                        help="Schreibt Report nach results/audits/")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skippt Match-Winner-Backtest, nur Calibration (schneller)")
    args = parser.parse_args()

    tours = ["atp", "wta"] if args.tour == "both" else [args.tour]
    years = _parse_years(args.years)

    # Sektion 1
    mw_agg: list[dict] = []
    if not args.dry_run:
        print("\n========== SEKTION 1: Match Winner ROI-Backtest ==========")
        mw_df = run_match_winner_backtest(
            tours=tours, years=years,
            min_year=args.from_year, odds_source="max", min_prob=0.35,
        )
        mw_agg = _aggregate_match_winner(mw_df)

    # Sektionen 2+3
    print("\n========== SEKTIONEN 2+3: Markt-Kalibrierung ==========")
    set_df, game_df = run_calibration_loop(tours, years)
    set_agg = _aggregate_calibration(set_df)
    game_agg = _aggregate_calibration(game_df)

    # Report
    out_path = ROOT / "results" / "audits" / f"tennis_full_backtest_{date.today().isoformat()}.md"
    if args.save or not args.dry_run:
        write_report(mw_agg, set_agg, game_agg, out_path)
        print(f"\nReport: {out_path}")

    # Konsolen-Zusammenfassung
    print("\n========== ZUSAMMENFASSUNG ==========")
    print(f"Match-Winner-Verdicts: {sum(1 for r in mw_agg if r['verdict']=='LIVE')} LIVE / "
          f"{sum(1 for r in mw_agg if r['verdict']=='SHADOW')} SHADOW / "
          f"{sum(1 for r in mw_agg if r['verdict']=='BLACKLIST')} BLACKLIST")
    print(f"Set-Markt-Kalibrierungen: {sum(1 for r in set_agg if r['kalibriert'])} / {len(set_agg)} kalibriert")
    print(f"Game-Markt-Kalibrierungen: {sum(1 for r in game_agg if r['kalibriert'])} / {len(game_agg)} kalibriert")


if __name__ == "__main__":
    main()

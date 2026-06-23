#!/usr/bin/env python3
"""
Walk-forward backtest for tennis Elo model.
Data: Jeff Sackmann (match history) + tennis-data.co.uk (historical B365 odds)
Covers: All 4 Grand Slams, ATP + WTA, 2019-2025.

Usage:
  python3 scripts/tennis_backtest.py [--tour atp|wta|both] [--surface grass|clay|hard|all]
  python3 scripts/tennis_backtest.py --tour both --surface all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.config import MIN_EDGE, MAX_EV, TENNIS_MIN_EDGE_BY_CATEGORY
from src.betting.kelly import dynamic_stake_eur, kelly_fraction, expected_value
from src.data.tennis_data import fetch_atp_matches, fetch_wta_matches
from src.data.tennis_odds import fetch_grand_slam_odds, fetch_full_tour_odds, categorize_series
from src.models.tennis_elo import TennisEloRatings, _k, _expected, _apply_decay, _DEFAULT_RATING


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """
    Normalises player name to 'Surname Initial' for cross-source matching.
      Sackmann:         "Frances Tiafoe"          → "Tiafoe F"
      tennis-data.co.uk "Tiafoe F."               → "Tiafoe F"
      Compound surname: "Botic Van De Zandschulp" → "Van De Zandschulp B"
      Compound tennis:  "Van De Zandschulp B."    → "Van De Zandschulp B"
    """
    parts = name.strip().split()
    if len(parts) == 1:
        return parts[0]
    last = parts[-1].rstrip(".")
    if len(last) <= 2:
        # Format: "Surname F." — everything before last token is the surname
        surname = " ".join(parts[:-1])
        return f"{surname} {last}"
    else:
        # Format: "Firstname [Mid] Surname"
        first_initial = parts[0][0]
        surname = " ".join(parts[1:])
        return f"{surname} {first_initial}"


# ---------------------------------------------------------------------------
# Tournament name normalisation (Sackmann → tennis-data slug)
# ---------------------------------------------------------------------------

_SACK_TO_SLUG: dict[str, str] = {
    "wimbledon":      "wimbledon",
    "us open":        "usopen",
    "u.s. open":      "usopen",
    "australian open":"ausopen",
    "roland garros":  "frenchopen",
    "french open":    "frenchopen",
}


def _tourney_slug(name: str) -> str | None:
    """Maps Sackmann tourney_name to tennis-data.co.uk tournament slug, or None."""
    return _SACK_TO_SLUG.get(name.lower().strip())


# ---------------------------------------------------------------------------
# Walk-forward Elo (no lookahead)
# ---------------------------------------------------------------------------

def _build_walkforward_elo(
    matches: pd.DataFrame,
    snapshot_all_events: bool = False,
) -> dict[tuple[str, str, str, int], dict[str, float]]:
    """
    Returns a dict keyed by (_norm(winner), _norm(loser), tourney_key, year) →
    pre-match ratings snapshot. Iterates chronologically, updating AFTER snapshot.

    Sackmann stores the tournament START date for all matches in a tournament,
    not the individual match date — so we key on (winner, loser, tournament, year)
    instead of the actual date. Single-elimination ensures uniqueness per pair.

    snapshot_all_events: wenn True, wird für JEDES Match ein Snapshot gespeichert
    (key = normalized tourney_name); sonst nur Grand Slams (J2-B-Modus für
    full-tour-Backtest).
    """
    overall: dict[str, float] = {}
    by_surface: dict[str, dict[str, float]] = {}
    snapshots: dict[tuple, dict[str, float]] = {}
    current_year: int | None = None

    df = matches.dropna(subset=["winner_name", "loser_name"]).sort_values("tourney_date")

    for _, row in df.iterrows():
        match_year = row["tourney_date"].year
        winner = _norm(str(row["winner_name"]))
        loser  = _norm(str(row["loser_name"]))
        surface = str(row.get("surface", "hard")).lower()
        level = str(row.get("tourney_level", "")).strip()
        tourney_name = str(row.get("tourney_name", "")).strip()
        slug = _tourney_slug(tourney_name)

        if match_year != current_year:
            if current_year is not None:
                _apply_decay(overall)
                for s_pool in by_surface.values():
                    _apply_decay(s_pool)
            current_year = match_year

        # Record pre-match snapshot
        snap_key = None
        if slug is not None:
            snap_key = (winner, loser, slug, match_year)
        elif snapshot_all_events and tourney_name:
            # Full-tour-Backtest: tourney_name in lowercased Form als Key
            snap_key = (winner, loser, tourney_name.lower().strip(), match_year)

        if snap_key is not None:
            snapshots[snap_key] = {
                "r_w_overall": overall.get(winner, _DEFAULT_RATING),
                "r_l_overall": overall.get(loser, _DEFAULT_RATING),
                "r_w_surface": by_surface.get(surface, {}).get(winner, _DEFAULT_RATING),
                "r_l_surface": by_surface.get(surface, {}).get(loser, _DEFAULT_RATING),
                "surface": surface,
            }

        # Update ratings (ALL matches, not just Grand Slams)
        k = _k(level)
        r_w = overall.get(winner, _DEFAULT_RATING)
        r_l = overall.get(loser, _DEFAULT_RATING)
        e_w = _expected(r_w, r_l)
        overall[winner] = r_w + k * (1.0 - e_w)
        overall[loser] = r_l + k * (0.0 - (1.0 - e_w))

        if surface not in by_surface:
            by_surface[surface] = {}
        rws = by_surface[surface].get(winner, _DEFAULT_RATING)
        rls = by_surface[surface].get(loser, _DEFAULT_RATING)
        ews = _expected(rws, rls)
        by_surface[surface][winner] = rws + k * (1.0 - ews)
        by_surface[surface][loser] = rls + k * (0.0 - (1.0 - ews))

    return snapshots


_SURFACE_WEIGHTS = {"grass": 0.60, "clay": 0.70, "hard": 0.70}


def _predict_from_snapshot(
    snap: dict,
    surface: str,
    w_surface: float | None = None,
) -> tuple[float, float]:
    """Returns (p_winner, p_loser) from a pre-match rating snapshot."""
    if w_surface is None:
        w_surface = _SURFACE_WEIGHTS.get(surface.lower(), 0.70)
    r_w = w_surface * snap["r_w_surface"] + (1 - w_surface) * snap["r_w_overall"]
    r_l = w_surface * snap["r_l_surface"] + (1 - w_surface) * snap["r_l_overall"]
    p_w = _expected(r_w, r_l)
    return p_w, 1.0 - p_w


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------

_ODDS_COLS = {
    "b365": ("B365W", "B365L"),
    "avg":  ("AvgW",  "AvgL"),
    "max":  ("MaxW",  "MaxL"),
}


def run_backtest(
    tour: str = "both",
    surface_filter: str = "all",
    min_year: int = 2021,
    bankroll: float = 100.0,
    w_surface: float | None = None,
    odds_source: str = "max",
    min_prob: float = 0.35,
    full_tour: bool = False,
    use_category_min_edge: bool = False,
) -> pd.DataFrame:
    """
    Runs walk-forward backtest. Returns DataFrame with one row per bet.

    odds_source: 'b365' | 'avg' | 'max'  — which odds column to use.
        'max' uses best-available market odds (matches live scanner behaviour).
    min_prob: minimum model probability to place a bet (filters extreme underdogs).
        Backtest shows p >= 0.35 optimal: grass +3.8% ROI, WTA +6.3% (Max odds).
    w_surface: surface Elo blend weight. None = use surface-specific defaults
        (grass=0.60, clay/hard=0.70 — calibrated from backtest).
    min_year: first year used for testing (2019-2020 are warm-up only).
    """
    w_col, l_col = _ODDS_COLS.get(odds_source.lower(), _ODDS_COLS["max"])
    print("Loading match history (Sackmann)...")
    tours = ["atp", "wta"] if tour == "both" else [tour]
    sack_frames = []
    for t in tours:
        try:
            df = fetch_atp_matches() if t == "atp" else fetch_wta_matches()
            df["_tour"] = t.upper()
            sack_frames.append(df)
        except Exception as e:
            print(f"  WARNING: Could not load {t} data: {e}")
    if not sack_frames:
        raise RuntimeError("No match data available.")
    all_matches = pd.concat(sack_frames, ignore_index=True).sort_values("tourney_date")
    print(f"  {len(all_matches)} total matches loaded")

    print("Building walk-forward Elo snapshots...")
    snapshots = _build_walkforward_elo(all_matches, snapshot_all_events=full_tour)
    print(f"  {len(snapshots)} snapshots built")

    if full_tour:
        print("Loading FULL-TOUR odds (tennis-data.co.uk annual XLSX)...")
        odds_df = fetch_full_tour_odds(tours=[t for t in tours])
    else:
        print("Loading Grand Slam odds (tennis-data.co.uk)...")
        odds_df = fetch_grand_slam_odds(tours=[t for t in tours])
    print(f"  {len(odds_df)} odds rows loaded")

    if odds_df.empty:
        raise RuntimeError("No odds data available. Check network connection.")

    # Filter test years
    odds_df = odds_df[odds_df["Date"].dt.year >= min_year].copy()

    # Surface filter
    if surface_filter != "all":
        odds_df = odds_df[odds_df["surface_std"] == surface_filter].copy()

    print(f"  {len(odds_df)} matches in test window (from {min_year}, odds={odds_source.upper()}, min_prob={min_prob:.0%})")

    records = []
    matched = skipped_no_snap = skipped_no_edge = 0

    for _, row in odds_df.iterrows():
        winner = _norm(str(row["Winner"]))
        loser  = _norm(str(row["Loser"]))
        try:
            odds_w = float(row[w_col])
            odds_l = float(row[l_col])
        except (KeyError, ValueError):
            skipped_no_snap += 1
            continue
        if pd.isna(odds_w) or pd.isna(odds_l):
            skipped_no_snap += 1
            continue
        surface = str(row.get("surface_std", "hard"))
        tour_r = str(row.get("tour", "ATP"))
        match_year = row["Date"].year
        rnd = str(row.get("Round", ""))

        # Full-tour: tournament-name aus Spalte 'Tournament', Category aus 'category'
        # Slam-only: tournament=slug (wimbledon/usopen/...), Category=grand_slam
        if full_tour:
            tournament_name = str(row.get("Tournament", "")).strip()
            tournament_key = tournament_name.lower()
            category = str(row.get("category", "atp250"))
        else:
            tournament_name = str(row.get("tournament", ""))
            tournament_key = tournament_name
            category = "grand_slam"

        # Lookup pre-match Elo snapshot
        snap = snapshots.get((winner, loser, tournament_key, match_year))
        if snap is None:
            skipped_no_snap += 1
            continue
        matched += 1

        p_w, p_l = _predict_from_snapshot(snap, surface, w_surface)

        # Per-Category-Edge wenn aktiviert, sonst globaler MIN_EDGE
        cat_min_edge = (
            TENNIS_MIN_EDGE_BY_CATEGORY.get(category, MIN_EDGE)
            if use_category_min_edge else MIN_EDGE
        )

        # Check each side for value (skip extreme underdogs)
        for side, model_p, odds, outcome in [
            ("winner", p_w, odds_w, True),
            ("loser",  p_l, odds_l, False),
        ]:
            if model_p < min_prob:
                skipped_no_edge += 1
                continue
            ev = expected_value(model_p, odds)
            if ev < cat_min_edge or ev > MAX_EV:
                skipped_no_edge += 1
                continue

            kf = kelly_fraction(model_p, odds)
            stake = dynamic_stake_eur(ev, "MEDIUM")
            won = outcome
            pnl = stake * (odds - 1) if won else -stake

            records.append({
                "date":        str(row["Date"].date()),
                "year":        match_year,
                "tour":        tour_r,
                "tournament":  tournament_name,
                "category":    category,
                "surface":     surface,
                "round":       rnd,
                "winner":      winner,
                "loser":       loser,
                "side":        side,
                "model_prob":  round(model_p, 4),
                "market_odds": odds,
                "ev":          round(ev, 4),
                "kelly_f":     round(kf, 4),
                "stake":       round(stake, 2),
                "won":         int(won),
                "pnl":         round(pnl, 2),
            })

    print(f"\nMatched: {matched} / {len(odds_df)}  (skipped no-snap: {skipped_no_snap})")
    print(f"Value bets found: {len(records)}  (skipped no-edge: {skipped_no_edge})")
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _brier(df: pd.DataFrame) -> float:
    return float(((df["model_prob"] - df["won"]) ** 2).mean())


def print_results(df: pd.DataFrame) -> None:
    if df.empty:
        print("No bets found.")
        return

    total_staked = df["stake"].sum()
    total_pnl = df["pnl"].sum()
    roi = total_pnl / total_staked * 100 if total_staked > 0 else 0
    wins = int(df["won"].sum())
    n = len(df)
    wr = wins / n * 100
    brier = _brier(df)

    SEP = "─" * 50
    print(f"\n{SEP}")
    print(f"TENNIS BACKTEST RESULTS")
    print(SEP)
    print(f"Bets:        {n}  ({wins}W / {n-wins}L)")
    print(f"Win Rate:    {wr:.1f}%")
    print(f"Staked:      {total_staked:.0f} EUR")
    print(f"P&L:         {total_pnl:+.0f} EUR")
    print(f"ROI:         {roi:+.1f}%")
    print(f"Brier Score: {brier:.4f}  (0.25 = random, lower = better)")

    # By tour
    print(f"\n--- By Tour ---")
    for t, grp in df.groupby("tour"):
        st = grp["stake"].sum()
        pnl = grp["pnl"].sum()
        r = pnl / st * 100 if st > 0 else 0
        w = int(grp["won"].sum())
        print(f"  {t:<5} {len(grp):>4} bets  {pnl:+7.0f} EUR  ROI:{r:+6.1f}%  {w}W/{len(grp)-w}L")

    # By tournament
    print(f"\n--- By Tournament ---")
    for t, grp in df.groupby("tournament"):
        st = grp["stake"].sum()
        pnl = grp["pnl"].sum()
        r = pnl / st * 100 if st > 0 else 0
        w = int(grp["won"].sum())
        print(f"  {t:<12} {len(grp):>4} bets  {pnl:+7.0f} EUR  ROI:{r:+6.1f}%  {w}W/{len(grp)-w}L")

    # By surface
    print(f"\n--- By Surface ---")
    for s, grp in df.groupby("surface"):
        st = grp["stake"].sum()
        pnl = grp["pnl"].sum()
        r = pnl / st * 100 if st > 0 else 0
        w = int(grp["won"].sum())
        print(f"  {s:<8} {len(grp):>4} bets  {pnl:+7.0f} EUR  ROI:{r:+6.1f}%  {w}W/{len(grp)-w}L")

    # By year
    print(f"\n--- By Year ---")
    for yr, grp in df.groupby("year"):
        st = grp["stake"].sum()
        pnl = grp["pnl"].sum()
        r = pnl / st * 100 if st > 0 else 0
        w = int(grp["won"].sum())
        print(f"  {yr}  {len(grp):>4} bets  {pnl:+7.0f} EUR  ROI:{r:+6.1f}%  {w}W/{len(grp)-w}L")

    # By side (favourite vs underdog)
    print(f"\n--- Favourite vs Underdog ---")
    for side, grp in df.groupby("side"):
        st = grp["stake"].sum()
        pnl = grp["pnl"].sum()
        r = pnl / st * 100 if st > 0 else 0
        w = int(grp["won"].sum())
        avg_odds = grp["market_odds"].mean()
        print(f"  {side:<8} {len(grp):>4} bets  {pnl:+7.0f} EUR  ROI:{r:+6.1f}%  avg odds:{avg_odds:.2f}")

    # By round
    print(f"\n--- By Round ---")
    round_order = ["1st Round", "2nd Round", "3rd Round", "4th Round",
                   "Quarterfinals", "Semifinals", "The Final"]
    for rnd in round_order:
        grp = df[df["round"] == rnd]
        if grp.empty:
            continue
        st = grp["stake"].sum()
        pnl = grp["pnl"].sum()
        r = pnl / st * 100 if st > 0 else 0
        w = int(grp["won"].sum())
        print(f"  {rnd:<16} {len(grp):>3} bets  {pnl:+6.0f} EUR  ROI:{r:+6.1f}%")

    print(SEP)


_VERDICT_LIVE_N_HIGH = 50
_VERDICT_LIVE_ROI_HIGH = 0.03  # 3%
_VERDICT_LIVE_N_LOW = 30
_VERDICT_LIVE_ROI_LOW = 0.05   # 5%
_VERDICT_BLACKLIST_ROI = -0.05  # -5%


def _category_verdict(n: int, roi: float) -> str:
    """Live-Gate aus Plan: ROI≥3% bei n≥50 ODER ROI≥5% bei n≥30 → LIVE.
    ROI ≤ -5% → BLACKLIST. Sonst SHADOW.
    """
    if n >= _VERDICT_LIVE_N_HIGH and roi >= _VERDICT_LIVE_ROI_HIGH:
        return "LIVE"
    if n >= _VERDICT_LIVE_N_LOW and roi >= _VERDICT_LIVE_ROI_LOW:
        return "LIVE"
    if roi <= _VERDICT_BLACKLIST_ROI:
        return "BLACKLIST"
    return "SHADOW"


def write_j2_report(df: pd.DataFrame, out_path: Path) -> None:
    """Schreibt Markdown-Bericht mit Per-Category-Verdicts (Roadmap J2-B)."""
    from datetime import date
    lines = [
        f"# Tennis J2 Backtest — {date.today().isoformat()}",
        "",
        f"**Bets:** {len(df)} · **Staked:** {df['stake'].sum():.0f} EUR · "
        f"**P&L:** {df['pnl'].sum():+.0f} EUR · "
        f"**ROI:** {df['pnl'].sum() / df['stake'].sum() * 100:+.1f}% · "
        f"**Brier:** {_brier(df):.4f}",
        "",
        "## Gate-Verdict pro Kategorie",
        "",
        "Live-Gate: ROI≥3% bei n≥50 ODER ROI≥5% bei n≥30. BLACKLIST: ROI≤-5%. Sonst SHADOW.",
        "",
        "| Kategorie | Tour | N | Hit% | ROI | Brier | Verdict |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    if "category" not in df.columns:
        df = df.copy()
        df["category"] = "grand_slam"

    for (cat, tour), grp in df.groupby(["category", "tour"]):
        n = len(grp)
        st = grp["stake"].sum()
        roi = grp["pnl"].sum() / st if st > 0 else 0
        wr = grp["won"].mean()
        br = _brier(grp)
        v = _category_verdict(n, roi)
        emoji = {"LIVE": "✅", "SHADOW": "⚠️", "BLACKLIST": "🚫"}[v]
        lines.append(f"| {cat} | {tour} | {n} | {wr*100:.1f}% | {roi*100:+.1f}% | {br:.4f} | {emoji} {v} |")

    # Surface × Category Breakdown
    lines += ["", "## Surface × Kategorie", "",
              "| Kategorie | Surface | N | ROI | Verdict |", "|---|---|---:|---:|---|"]
    for (cat, surf), grp in df.groupby(["category", "surface"]):
        n = len(grp)
        st = grp["stake"].sum()
        roi = grp["pnl"].sum() / st if st > 0 else 0
        v = _category_verdict(n, roi)
        emoji = {"LIVE": "✅", "SHADOW": "⚠️", "BLACKLIST": "🚫"}[v]
        lines.append(f"| {cat} | {surf} | {n} | {roi*100:+.1f}% | {emoji} |")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Tennis Elo walk-forward backtest")
    parser.add_argument("--tour", default="both", choices=["atp", "wta", "both"])
    parser.add_argument("--surface", default="all",
                        choices=["all", "grass", "clay", "hard"])
    parser.add_argument("--from-year", type=int, default=2021,
                        help="First year used for testing (default: 2021)")
    parser.add_argument("--bankroll", type=float, default=100.0)
    parser.add_argument("--odds", default="max", choices=["b365", "avg", "max"],
                        help="Odds source: b365|avg|max (default: max = best available, matches live scanner)")
    parser.add_argument("--min-prob", type=float, default=0.35,
                        help="Min model probability to bet (default: 0.35, filters extreme underdogs)")
    parser.add_argument("--save", action="store_true",
                        help="Save results CSV to results/tennis_backtest.csv")
    parser.add_argument("--full-tour", action="store_true",
                        help="J2-B: use annual XLSX (all tour events) instead of Slam-only CSVs")
    parser.add_argument("--use-category-edge", action="store_true",
                        help="J2-B: per-category min_edge from TENNIS_MIN_EDGE_BY_CATEGORY")
    parser.add_argument("--j2-report", action="store_true",
                        help="J2-B: write Per-Category-Verdict markdown to results/audits/")
    args = parser.parse_args()

    results = run_backtest(
        tour=args.tour,
        surface_filter=args.surface,
        min_year=args.from_year,
        bankroll=args.bankroll,
        odds_source=args.odds,
        min_prob=args.min_prob,
        full_tour=args.full_tour,
        use_category_min_edge=args.use_category_edge,
    )

    print_results(results)

    if args.save and not results.empty:
        out = ROOT / "results" / "tennis_backtest.csv"
        results.to_csv(out, index=False)
        print(f"\nSaved: {out}")

    if args.j2_report and not results.empty:
        from datetime import date
        report_path = ROOT / "results" / "audits" / f"tennis_j2_backtest_{date.today().isoformat()}.md"
        write_j2_report(results, report_path)
        print(f"J2-Report: {report_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""A/B-Backtest: Elo pur vs. Elo + Hebel 1-5.

Nutzt tennis-data.co.uk XLSX (W1-W5 Game-Scores + B365/Avg/Max Odds).
Baut walk-forward:
  - Elo-Snapshots (unverändert wie tennis_full_backtest)
  - PlayerHebelState (Games-Historie, Set-1-Historie) für Hebel 2 + 5
  - Sharp-Signal aus (Avg - B365) / Avg für Hebel 4
  - Bayesian + Altitude via elo_hebels für Hebel 1 + 3

Vergleicht beide Läufe auf denselben Matches + Odds.

Usage:
  python3 scripts/tennis_hebel_backtest.py --tour both --from-year 2022
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from scripts.tennis_backtest import _norm, _predict_from_snapshot, _build_walkforward_elo
from scripts.tennis_full_backtest import _xlsx_to_sackmann_schema
from src.betting.kelly import dynamic_stake_eur, expected_value, kelly_fraction
from src.config import MAX_EV, MIN_EDGE, TENNIS_MIN_EDGE_BY_CATEGORY
from src.data.tennis_odds import fetch_full_tour_odds
from src.tennis.elo_hebels import apply_elo_hebels
from src.tennis.backtest_hebels import (
    PlayerHebelState, apply_all_hebels, sharp_signal, sharp_confirms_bet,
)


def _parse_sets(row) -> tuple[int, int, bool | None]:
    """Return (winner_total_games, loser_total_games, winner_won_set1)."""
    w_total = 0; l_total = 0
    for i in range(1, 6):
        w = row.get(f"W{i}"); l = row.get(f"L{i}")
        try:
            wi = int(w) if pd.notna(w) else None
            li = int(l) if pd.notna(l) else None
        except (ValueError, TypeError):
            continue
        if wi is not None:
            w_total += wi
        if li is not None:
            l_total += li
    # won_set1: W1 > L1
    try:
        w1 = int(row.get("W1")); l1 = int(row.get("L1"))
        won_set1 = w1 > l1
    except (ValueError, TypeError):
        won_set1 = None
    return w_total, l_total, won_set1


def run_ab(
    tours: list[str],
    years: range,
    min_year: int = 2022,
    odds_source: str = "max",
    min_prob: float = 0.35,
    enable_bayesian: bool = True,
    enable_altitude: bool = True,
    enable_style_momentum: bool = True,
    enable_sharp: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    print("[hebel-ab] Lade XLSX ...")
    odds_df = fetch_full_tour_odds(tours=tours, years=years)
    if odds_df.empty:
        raise RuntimeError("Keine Odds-Daten.")
    print(f"  {len(odds_df)} Match-Zeilen")

    print("[hebel-ab] Build walk-forward Elo ...")
    sack_like = _xlsx_to_sackmann_schema(odds_df)
    snapshots = _build_walkforward_elo(sack_like, snapshot_all_events=True)
    print(f"  {len(snapshots)} Snapshots")

    # Walk-forward über odds_df sortiert nach Datum → für Hebel-State
    odds_df = odds_df.sort_values("Date").reset_index(drop=True)
    hebel_state: dict[str, PlayerHebelState] = defaultdict(PlayerHebelState)

    odds_cols = {"b365": ("B365W", "B365L"), "avg": ("AvgW", "AvgL"), "max": ("MaxW", "MaxL")}
    w_col, l_col = odds_cols.get(odds_source, odds_cols["max"])

    records_base: list[dict] = []
    records_hebel: list[dict] = []
    n_test = 0

    for _, row in odds_df.iterrows():
        try:
            odds_w = float(row[w_col]); odds_l = float(row[l_col])
            b365_w = float(row.get("B365W", 0) or 0)
            b365_l = float(row.get("B365L", 0) or 0)
            avg_w = float(row.get("AvgW", 0) or 0)
            avg_l = float(row.get("AvgL", 0) or 0)
        except (ValueError, TypeError):
            continue
        if pd.isna(odds_w) or pd.isna(odds_l):
            continue

        winner_raw = str(row["Winner"]); loser_raw = str(row["Loser"])
        winner = _norm(winner_raw); loser = _norm(loser_raw)
        surface = str(row.get("surface_std", "hard"))
        tournament_key = str(row.get("Tournament", "")).strip().lower()
        match_year = row["Date"].year
        category = str(row.get("category", "atp250"))
        tour_r = str(row.get("tour", "ATP"))

        # Test-Window
        in_test = row["Date"].year >= min_year

        if in_test:
            n_test += 1
            snap = snapshots.get((winner, loser, tournament_key, match_year))
            if snap is not None:
                # BASE Prediction (Elo pur)
                p_w_base, p_l_base = _predict_from_snapshot(snap, surface)

                # HEBEL 1+3 (Elo-level Adjustments)
                p_w_hebel = apply_elo_hebels(
                    p_w_base,
                    surface_count_a=snap.get("n_w_surface", 0),
                    surface_count_b=snap.get("n_l_surface", 0),
                    tourney_slug=tournament_key,
                    enable_bayesian=enable_bayesian,
                    enable_altitude=enable_altitude,
                )
                p_l_hebel = 1.0 - p_w_hebel

                # HEBEL 2+5 (Player-State Bias)
                if enable_style_momentum:
                    state_w = hebel_state[winner]; state_l = hebel_state[loser]
                    p_w_hebel, _dbg_w = apply_all_hebels(
                        p_w_hebel, state_a=state_w, state_b=state_l,
                    )
                p_l_hebel = 1.0 - p_w_hebel

                # Sharp-Signal (Hebel 4) — pro Player
                sig_w = sharp_signal(b365_w, avg_w) * -1  # Odds fielen ⇒ Sharp backt Player
                # Sign convention: sharp_signal returns +1 wenn (avg-b365)/avg >0 → odds fielen → backer
                sig_w = sharp_signal(b365_w, avg_w)
                sig_l = sharp_signal(b365_l, avg_l)

                cat_min_edge = TENNIS_MIN_EDGE_BY_CATEGORY.get(category, MIN_EDGE)

                # Beide Modelle bewerten
                for side, mp_base, mp_hebel, odds, won, sig in [
                    ("winner", p_w_base, p_w_hebel, odds_w, True, sig_w),
                    ("loser",  p_l_base, p_l_hebel, odds_l, False, sig_l),
                ]:
                    if mp_base >= min_prob:
                        ev = expected_value(mp_base, odds)
                        if cat_min_edge <= ev <= MAX_EV:
                            stake = dynamic_stake_eur(ev, "MEDIUM")
                            pnl = stake * (odds - 1) if won else -stake
                            records_base.append({
                                "date": str(row["Date"].date()), "year": match_year,
                                "tour": tour_r, "category": category, "surface": surface,
                                "side": side, "model_prob": mp_base, "market_odds": odds,
                                "ev": ev, "stake": stake, "won": int(won), "pnl": pnl,
                            })
                    if mp_hebel >= min_prob:
                        ev_h = expected_value(mp_hebel, odds)
                        if cat_min_edge <= ev_h <= MAX_EV:
                            # HEBEL 4: Sharp-Filter — überspringen wenn Sharp entgegengesetzt
                            market_p = 1.0 / odds if odds > 1 else 0.5
                            if enable_sharp and not sharp_confirms_bet(mp_hebel, market_p, sig):
                                stake_mult = 0.5
                            else:
                                stake_mult = 1.0
                            stake = dynamic_stake_eur(ev_h, "MEDIUM") * stake_mult
                            pnl = stake * (odds - 1) if won else -stake
                            records_hebel.append({
                                "date": str(row["Date"].date()), "year": match_year,
                                "tour": tour_r, "category": category, "surface": surface,
                                "side": side, "model_prob": mp_hebel, "market_odds": odds,
                                "ev": ev_h, "stake": stake, "won": int(won), "pnl": pnl,
                            })

        # Update Hebel-State NACH prediction (walk-forward safe)
        w_games, l_games, won_set1 = _parse_sets(row)
        hebel_state[winner].add_match(w_games, l_games, won_set1, True)
        hebel_state[loser].add_match(l_games, w_games,
                                     None if won_set1 is None else not won_set1, False)

    print(f"  Test-Matches: {n_test}")
    print(f"  Bets BASE:  {len(records_base)}")
    print(f"  Bets HEBEL: {len(records_hebel)}")
    return pd.DataFrame(records_base), pd.DataFrame(records_hebel)


def _summarize(df: pd.DataFrame, label: str) -> dict:
    if df.empty:
        return {"label": label, "n": 0, "roi": 0.0, "pnl": 0.0, "hit": 0.0}
    total_stake = df["stake"].sum()
    total_pnl = df["pnl"].sum()
    hit = df["won"].mean() * 100
    roi = (total_pnl / total_stake * 100) if total_stake > 0 else 0.0
    return {
        "label": label, "n": len(df), "roi": roi, "pnl": total_pnl,
        "stake": total_stake, "hit": hit,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tour", default="both", choices=["atp", "wta", "both"])
    ap.add_argument("--from-year", type=int, default=2022)
    ap.add_argument("--years", default="2019-2025")
    args = ap.parse_args()

    tours = ["atp", "wta"] if args.tour == "both" else [args.tour]
    a, b = args.years.split("-") if "-" in args.years else (args.years, args.years)
    years = range(int(a), int(b) + 1)

    base, hebel = run_ab(tours=tours, years=years, min_year=args.from_year)

    print("\n=== SUMMARY ===")
    for df, label in [(base, "BASE (Elo pur)"), (hebel, "HEBEL (1-5)")]:
        s = _summarize(df, label)
        print(f"  {label:<22} n={s['n']:>5}  hit={s['hit']:5.1f}%  "
              f"pnl={s['pnl']:+8.1f}€  stake={s.get('stake',0):8.1f}€  ROI={s['roi']:+6.2f}%")

    # Per-Kategorie
    print("\n=== PER KATEGORIE ===")
    print(f"{'Kategorie/Tour':<24} {'BASE n':>7} {'BASE ROI':>10}  {'HEBEL n':>8} {'HEBEL ROI':>10}  {'Δ ROI':>8}")
    keys = set()
    if not base.empty: keys |= set(zip(base["category"], base["tour"]))
    if not hebel.empty: keys |= set(zip(hebel["category"], hebel["tour"]))
    for cat, tour in sorted(keys):
        b_sub = base[(base["category"] == cat) & (base["tour"] == tour)] if not base.empty else pd.DataFrame()
        h_sub = hebel[(hebel["category"] == cat) & (hebel["tour"] == tour)] if not hebel.empty else pd.DataFrame()
        b_s = _summarize(b_sub, "b"); h_s = _summarize(h_sub, "h")
        delta = h_s["roi"] - b_s["roi"]
        print(f"{cat + '/' + tour:<24} {b_s['n']:>7} {b_s['roi']:>+9.1f}%  "
              f"{h_s['n']:>8} {h_s['roi']:>+9.1f}%  {delta:>+7.1f}pp")

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Walk-forward backtest for special markets: O/U 2.5, O/U 3.0, AH ±0.5, BTTS, Goals 2-4.

WICHTIG — Methodik:
  Keine historischen Marktquoten für O/U/AH/BTTS verfügbar (football-data.co.uk hat nur 1x2).
  Simulation verwendet repräsentative Durchschnittsquoten (Bet365 WM-Historisch).
  → EV vs. echtem Markt NICHT validiert. Kalibrierung (model_prob vs. Ist-Win-Rate) validiert.

Quoten-Annahmen (feste Referenz, nicht per-Match):
  O/U 2.5:   1.87 / 1.87  (Overround ~7%)
  O/U 3.0:   1.90 / 2.00  (Overround ~6%)
  AH -0.5:   1.88 / 1.88  (Overround ~6%)
  AH +0.5:   1.88 / 1.88  (Overround ~6%, Mirror zu -0.5)
  BTTS Yes:  1.80, No: 1.90 (Overround ~8%)
  Goals 2-4: 1.75 / 2.10  (geschätzt aus O/U-Komposition)

Usage:
  python3 scripts/backtest_special_markets.py
  python3 scripts/backtest_special_markets.py --events WC2018 WC2022
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.backtest.walk_forward import TOURNAMENT_EVENTS
from src.config import MIN_EDGE, RESULTS_DIR
from src.data.international import fetch_international_results, filter_competitive, filter_before
from src.models import dixon_coles as dc
from src.models.elo import compute_elo_series
from src.betting.kelly import expected_value, kelly_fraction, dynamic_stake_eur
from src.betting.odds_utils import remove_margin_shin

# ---------------------------------------------------------------------------
# Repräsentative Quoten (Bet365 WM-Historisch — nicht per-Match)
# ---------------------------------------------------------------------------
SYNTHETIC_ODDS: dict[str, tuple[float, float]] = {
    "ou_2.5_over":   (1.87, 1.87),
    "ou_2.5_under":  (1.87, 1.87),
    "ou_3.0_over":   (1.90, 2.00),
    "ou_3.0_under":  (1.90, 2.00),
    "ah_neg0.5_home": (1.88, 1.88),
    "ah_neg0.5_away": (1.88, 1.88),
    "ah_pos0.5_home": (1.88, 1.88),
    "ah_pos0.5_away": (1.88, 1.88),
    "btts_yes":      (1.80, 1.90),
    "btts_no":       (1.80, 1.90),
    "goals_2_4":     (1.75, 2.10),
    "goals_2_4_no":  (1.75, 2.10),
}

BANKROLL = 1000.0


def _get_odds(market_key: str) -> tuple[float, float]:
    """Returns (odds_for_this_side, odds_for_other_side)."""
    pair = SYNTHETIC_ODDS.get(market_key)
    if pair is None:
        return (1.85, 1.85)
    # For symmetric pairs, return first element as the bet side
    return pair


def _actual_outcome(row: pd.Series, market: str, line: float = 0.0) -> int | None:
    """Returns 1 (won), 0 (lost), or None (push) for a given market."""
    hg = int(row.get("home_score", -1))
    ag = int(row.get("away_score", -1))
    if hg < 0 or ag < 0:
        return None
    total = hg + ag
    diff = hg - ag

    if market == "ou_2.5_over":
        return int(total > 2.5)
    if market == "ou_2.5_under":
        return int(total < 2.5)
    if market == "ou_3.0_over":
        if total == 3:
            return None  # push
        return int(total > 3)
    if market == "ou_3.0_under":
        if total == 3:
            return None
        return int(total < 3)
    if market == "ah_neg0.5_home":
        return int(diff > 0)
    if market == "ah_neg0.5_away":
        return int(diff <= 0)
    if market == "ah_pos0.5_home":
        return int(diff >= 0)
    if market == "ah_pos0.5_away":
        return int(diff < 0)
    if market == "btts_yes":
        return int(hg >= 1 and ag >= 1)
    if market == "btts_no":
        return int(hg == 0 or ag == 0)
    if market == "goals_2_4":
        return int(2 <= total <= 4)
    if market == "goals_2_4_no":
        return int(total < 2 or total > 4)
    return None


def _model_probs(home: str, away: str, params, neutral: bool = True) -> dict[str, float]:
    """Compute all special market probabilities from DC model."""
    out = {}

    totals_25 = dc.predict_totals(home, away, params, line=2.5, neutral=neutral)
    out["ou_2.5_over"]  = totals_25["p_over"]
    out["ou_2.5_under"] = totals_25["p_under"]

    totals_30 = dc.predict_totals(home, away, params, line=3.0, neutral=neutral)
    out["ou_3.0_over"]  = totals_30["p_over"]
    out["ou_3.0_under"] = totals_30["p_under"]

    ah_neg05 = dc.predict_asian_handicap(home, away, params, line=-0.5, neutral=neutral)
    out["ah_neg0.5_home"] = ah_neg05["p_ah_home"]
    out["ah_neg0.5_away"] = ah_neg05["p_ah_away"]

    ah_pos05 = dc.predict_asian_handicap(home, away, params, line=0.5, neutral=neutral)
    out["ah_pos0.5_home"] = ah_pos05["p_ah_home"]
    out["ah_pos0.5_away"] = ah_pos05["p_ah_away"]

    btts = dc.predict_btts(home, away, params, neutral=neutral)
    out["btts_yes"] = btts["p_btts_yes"]
    out["btts_no"]  = btts["p_btts_no"]

    gr = dc.predict_goals_range(home, away, params, min_g=2, max_g=4, neutral=neutral)
    out["goals_2_4"]    = gr["p_in"]
    out["goals_2_4_no"] = gr["p_out"]

    return out


def run_event_backtest(
    event: dict,
    all_matches: pd.DataFrame,
    min_edge: float = MIN_EDGE,
    bankroll: float = BANKROLL,
) -> list[dict]:
    start = pd.Timestamp(event["start"])
    end   = pd.Timestamp(event["end"])

    train = filter_before(all_matches, start)
    if len(train) < 50:
        return []

    print(f"  [{event['name']}] Training on {len(train)} matches ...")
    params = dc.fit(train, phi=0.0065, today=start, max_iter=1000)

    event_matches = all_matches[
        (all_matches["date"] >= start) & (all_matches["date"] <= end)
    ].copy()

    rows = []
    for _, row in event_matches.iterrows():
        home = row["home_team"]
        away = row["away_team"]
        neutral = bool(row.get("neutral", True))

        try:
            probs = _model_probs(home, away, params, neutral=neutral)
        except Exception:
            continue

        for market, model_p in probs.items():
            # Synthetic market odds for this market
            odds_pair = _get_odds(market)
            # Use the bet-side odds (first element for both symmetric and asymmetric)
            if "under" in market or "away" in market or "no" in market:
                odds = odds_pair[1]
            else:
                odds = odds_pair[0]

            # Fair probability (remove margin from synthetic odds)
            total_inv = 1.0 / odds_pair[0] + 1.0 / odds_pair[1]
            fair_p = (1.0 / odds) / total_inv

            ev = expected_value(model_p, odds)
            kf = kelly_fraction(model_p, odds)
            stake = dynamic_stake_eur(ev, "MEDIUM", bankroll) if ev >= min_edge else 0.0

            actual = _actual_outcome(row, market)
            won = None
            pnl = None
            if actual is not None and stake > 0:
                if actual is None:
                    pnl = 0.0  # push
                else:
                    won = actual
                    pnl = stake * (odds - 1) * won - stake * (1 - won)

            rows.append({
                "event":      event["name"],
                "match_date": str(row["date"].date()),
                "home":       home,
                "away":       away,
                "market":     market,
                "model_prob": round(model_p, 4),
                "fair_prob":  round(fair_p, 4),
                "synth_odds": odds,
                "ev":         round(ev, 4),
                "stake":      round(stake, 2),
                "actual":     actual,
                "won":        won,
                "pnl":        round(pnl, 2) if pnl is not None else None,
                "has_bet":    stake > 0 and actual is not None,
            })

    return rows


def compute_metrics(rows: list[dict], market: str) -> dict:
    mrows = [r for r in rows if r["market"] == market]
    n_total = len(mrows)
    if n_total == 0:
        return {}

    # Calibration: all matches (not just bets)
    actuals = [r["actual"] for r in mrows if r["actual"] is not None]
    model_ps = [r["model_prob"] for r in mrows if r["actual"] is not None]
    n_with_outcome = len(actuals)
    if n_with_outcome == 0:
        return {}

    actual_win_rate = float(np.mean(actuals))
    avg_model_p = float(np.mean(model_ps))
    brier = float(np.mean([(mp - a) ** 2 for mp, a in zip(model_ps, actuals)]))
    calibration_gap = avg_model_p - actual_win_rate

    # Bet metrics (only where we'd bet)
    bet_rows = [r for r in mrows if r["has_bet"]]
    n_bets = len(bet_rows)
    if n_bets > 0:
        total_stake = sum(r["stake"] for r in bet_rows)
        total_pnl   = sum(r["pnl"] for r in bet_rows if r["pnl"] is not None)
        bet_wins    = sum(r["won"] for r in bet_rows if r["won"] is not None)
        bet_win_rate = bet_wins / n_bets
        roi = total_pnl / total_stake if total_stake > 0 else 0.0
    else:
        total_stake = total_pnl = bet_win_rate = roi = 0.0

    return {
        "n_matches":       n_total,
        "n_with_outcome":  n_with_outcome,
        "actual_win_rate": round(actual_win_rate, 4),
        "avg_model_prob":  round(avg_model_p, 4),
        "brier_score":     round(brier, 4),
        "calibration_gap": round(calibration_gap, 4),
        "n_bets":          n_bets,
        "bet_win_rate":    round(bet_win_rate, 4),
        "total_stake":     round(total_stake, 2),
        "total_pnl":       round(total_pnl, 2),
        "roi":             round(roi, 4),
    }


def print_summary(all_rows: list[dict]) -> None:
    markets = [
        "ou_2.5_over", "ou_2.5_under",
        "ou_3.0_over", "ou_3.0_under",
        "ah_neg0.5_home", "ah_neg0.5_away",
        "ah_pos0.5_home", "ah_pos0.5_away",
        "btts_yes", "btts_no",
        "goals_2_4", "goals_2_4_no",
    ]

    print("\n=== SPECIAL MARKETS BACKTEST (KALIBRIERUNG) ===")
    print("⚠️  Synthetic Odds — Kein echter Markt-EV-Test. Kalibrierung validiert.")
    print(f"\n{'Markt':<22} {'n_match':>7} {'win_rate':>9} {'model_p':>8} {'gap':>7} {'brier':>7} {'n_bets':>7} {'ROI':>8}")
    print("-" * 85)

    for market in markets:
        m = compute_metrics(all_rows, market)
        if not m:
            continue
        gap_str = f"{m['calibration_gap']:+.3f}"
        roi_str = f"{m['roi']*100:+.1f}%" if m['n_bets'] > 0 else "  n/a"
        flag = " ⚠️" if abs(m["calibration_gap"]) > 0.05 else ""
        print(
            f"  {market:<20} {m['n_matches']:>7} {m['actual_win_rate']:>9.3f} "
            f"{m['avg_model_prob']:>8.3f} {gap_str:>7} {m['brier_score']:>7.4f} "
            f"{m['n_bets']:>7} {roi_str:>8}{flag}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", nargs="+", help="Filter tournaments (e.g. WC2018 WC2022)")
    parser.add_argument("--min-edge", type=float, default=MIN_EDGE)
    args = parser.parse_args()

    events = TOURNAMENT_EVENTS
    if args.events:
        names = set(args.events)
        events = [e for e in events if e["name"] in names]
        if not events:
            print(f"No matching events for: {args.events}")
            sys.exit(1)

    print("Loading match data ...")
    df = filter_competitive(fetch_international_results())
    print(f"  {len(df)} competitive matches loaded.")

    all_rows: list[dict] = []
    for event in events:
        rows = run_event_backtest(event, df, min_edge=args.min_edge)
        all_rows.extend(rows)
        print(f"  [{event['name']}] {len(rows)} market-match rows generated.")

    if not all_rows:
        print("No results generated.")
        sys.exit(1)

    print_summary(all_rows)

    # Per-event breakdown
    print("\n=== PER TURNIER (O/U 2.5) ===")
    for event in events:
        ev_rows = [r for r in all_rows if r["event"] == event["name"]]
        m = compute_metrics(ev_rows, "ou_2.5_over")
        if m:
            print(f"  {event['name']}: win_rate={m['actual_win_rate']:.3f}, "
                  f"model_p={m['avg_model_prob']:.3f}, brier={m['brier_score']:.4f}, "
                  f"n_bets={m['n_bets']}, ROI={m['roi']*100:+.1f}%")

    print("\n=== PER TURNIER (BTTS YES) ===")
    for event in events:
        ev_rows = [r for r in all_rows if r["event"] == event["name"]]
        m = compute_metrics(ev_rows, "btts_yes")
        if m:
            print(f"  {event['name']}: win_rate={m['actual_win_rate']:.3f}, "
                  f"model_p={m['avg_model_prob']:.3f}, n_bets={m['n_bets']}, "
                  f"ROI={m['roi']*100:+.1f}%")

    # Save JSON
    audit_dir = RESULTS_DIR / "audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    out_path = audit_dir / f"special_markets_backtest_{date_str}.json"

    markets_summary = {}
    for market in [
        "ou_2.5_over", "ou_2.5_under", "ou_3.0_over", "ou_3.0_under",
        "ah_neg0.5_home", "ah_neg0.5_away", "ah_pos0.5_home", "ah_pos0.5_away",
        "btts_yes", "btts_no", "goals_2_4", "goals_2_4_no",
    ]:
        m = compute_metrics(all_rows, market)
        if m:
            markets_summary[market] = m

    result = {
        "as_of": datetime.utcnow().isoformat() + "Z",
        "events": [e["name"] for e in events],
        "methodology": (
            "Calibration backtest — no real market odds for O/U/AH/BTTS. "
            "Synthetic odds used: see SYNTHETIC_ODDS in script. "
            "EV vs real market NOT validated."
        ),
        "synthetic_odds": SYNTHETIC_ODDS,
        "markets": markets_summary,
    }

    out_path.write_text(json.dumps(result, indent=2))
    print(f"\nResults saved: {out_path}")


if __name__ == "__main__":
    main()

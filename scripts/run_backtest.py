"""
Walk-forward backtest over WC2018, WC2022, EURO2020, EURO2024, Copa America 2024.
Automatically loads tournament odds from data/raw/tournament_odds.csv if available.

Run:
  python scripts/run_backtest.py
  python scripts/run_backtest.py --no-odds   # skip odds, only calibration metrics
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.international import fetch_international_results, filter_competitive
from src.data.betexplorer import load_odds_lookup
from src.data.football_data_intl import fetch_wc_odds
from src.backtest.walk_forward import run_all_backtests, TOURNAMENT_EVENTS, compute_backtest_metrics
from src.config import RESULTS_DIR

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-odds", action="store_true", help="Skip odds lookup (calibration only)")
    args = parser.parse_args()

    print("Loading match data...")
    df = filter_competitive(fetch_international_results())
    print(f"  {len(df)} competitive matches loaded.")

    # Load tournament odds (merge football-data.co.uk WC data + Betexplorer EURO/Copa)
    odds_lookup = None
    if not args.no_odds:
        # Primary source: football-data.co.uk (WC2018 + WC2022, all 64 matches each)
        print("  Loading WC odds from football-data.co.uk ...")
        wc_odds = fetch_wc_odds()

        # Secondary source: Betexplorer (EURO2020/2024, CA2024 knockout stage)
        be_odds = load_odds_lookup()
        be_odds = be_odds[~be_odds["tournament"].isin(["WC2018", "WC2022"])] if not be_odds.empty else be_odds

        frames = [df for df in [wc_odds, be_odds] if not df.empty]
        if frames:
            import pandas as _pd
            odds_lookup = _pd.concat(frames, ignore_index=True)
            print(f"  Odds loaded: {len(odds_lookup)} matches across "
                  f"{odds_lookup['tournament'].nunique()} tournaments")
            print("  " + odds_lookup.groupby("tournament").size().to_string().replace("\n", "\n  "))
        else:
            print("  No tournament odds found. Run: python scripts/fetch_tournament_odds.py")
            print("  Continuing calibration-only backtest ...")

    print("\nRunning walk-forward backtest...")
    results, metrics = run_all_backtests(df, odds_lookup=odds_lookup)

    print("\n=== Backtest Summary ===")
    for k, v in metrics.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")

    out_dir = RESULTS_DIR / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not results.empty:
        csv_path = out_dir / "walkforward_results.csv"
        results.to_csv(csv_path, index=False)
        print(f"\nResults saved: {csv_path}")

        print("\n=== Per Tournament ===")
        for event in TOURNAMENT_EVENTS:
            ev_rows = results[results["event"] == event["name"]]
            if ev_rows.empty:
                continue
            m = compute_backtest_metrics(ev_rows)
            has_odds = "has_bet" in ev_rows.columns and ev_rows["has_bet"].any()
            roi_str = f"roi={m.get('roi', 0):.3f}" if has_odds else "roi=n/a (no odds)"
            print(f"  {event['name']}: n={len(ev_rows)}, bets={m.get('n_bets', 0)}, {roi_str}")

    if odds_lookup is not None and not odds_lookup.empty and metrics.get("n_bets", 0) > 0:
        roi = metrics.get("roi", 0)
        n_hmax = metrics.get("n_with_hmax", 0)

        print(f"\n=== Performance Gate ===")
        print(f"  ROI at bet price:        {roi:+.3f} ({'✅ positive' if roi > 0 else '⚠️ negative'})")
        print(f"  Sharpe ratio:            {metrics.get('sharpe', 0):.3f}")

        if n_hmax > 0:
            gap  = metrics.get("mean_odds_gap", 0)
            proj = metrics.get("projected_roi_hmax", 0)
            print(f"\n  Best-Odds Analysis ({n_hmax} bets with H-Max data):")
            print(f"  Mean odds gap (H-Max vs bet365):  {gap:+.2%}")
            print(f"  Projected ROI at H-Max:           {proj:+.3f}")
            print()
            print("  Interpretation:")
            print(f"  By using bet365 you pay ~{gap:.1%} above the tightest market price (H-Max).")
            print(f"  Seeking best available odds would lift projected ROI from {roi:+.1%} → {proj:+.1%}.")
            print()
            print("  Note: True CLV (opening vs closing from same source) cannot be computed")
            print("  from this dataset. Track live bets with Pinnacle opening/closing for CLV.")

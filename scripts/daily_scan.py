"""
CLI entry point for daily WM 2026 value scan.
Usage:
  python scripts/daily_scan.py              # interactive confirmation before logging
  python scripts/daily_scan.py --mock       # dry-run with mock data
  python scripts/daily_scan.py --auto-log   # skip confirmation, log all signals
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scanner.daily_scan import run_daily_scan
from src.betting.ledger import append_bets, ledger_summary, LEDGER_PATH
from src.notifications.web_dashboard import write_signals_json
from src.data.odds_api import fetch_upcoming_matches


def _confirm_bets(selected_signals: list, bankroll: float) -> list:
    """Shows top signals and asks for confirmation before logging."""
    if not selected_signals:
        return []

    # Detect non-interactive context early — avoid mid-loop EOFError confusion.
    if not sys.stdin.isatty():
        print(
            "\n  [Kein interaktives Terminal — Bestätigung übersprungen.]"
            "\n  Nutze '--auto-log' um alle Signals automatisch einzutragen, "
            "oder '! python3 scripts/daily_scan.py' im Terminal für interaktive Bestätigung."
        )
        return []

    print("\n=== Offene Slots — Bestätigung erforderlich ===")
    confirmed = []
    for s in selected_signals:
        stake = s.stake_pct * bankroll
        print(
            f"\n  {s.home} vs {s.away} | {s.market.upper()} | "
            f"@ {s.decimal_odds:.2f} | EV +{s.ev*100:.1f}% | "
            f"€{stake:.2f} | {s.confidence}"
        )
        try:
            ans = input("  Wette eingehen? (j/n): ").strip().lower()
        except EOFError:
            print("\n  [Stdin geschlossen — Bestätigung abgebrochen.]")
            break
        if ans == "j":
            confirmed.append(s)
            print("  ✓ Eingetragen.")
        else:
            print("  – Übersprungen.")

    return confirmed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SportsBrain daily value scan")
    parser.add_argument("--bankroll", type=float, default=100.0, help="Bankroll in EUR")
    parser.add_argument("--mock", action="store_true", help="Use mock data (no API call)")
    parser.add_argument("--output", type=str, default=None, help="Output path for report")
    parser.add_argument("--auto-log", action="store_true",
                        help="Skip confirmation, log all signals automatically")
    parser.add_argument("--retrain", action="store_true",
                        help="Auto-retrain DC + LightGBM before scanning")
    parser.add_argument("--horizon", type=int, default=None,
                        help="Only scan matches starting within HORIZON hours from now")
    parser.add_argument("--date", type=str, default=None,
                        help="Only scan matches on this date, e.g. 2026-06-11")
    parser.add_argument("--force", action="store_true",
                        help="Bypass WM date guard (scan even before June 11 or after July 19)")
    args = parser.parse_args()

    if args.retrain:
        import subprocess
        print("--- Auto-Retraining ---")
        subprocess.run([sys.executable, "scripts/auto_retrain.py"], check=True)
        print("--- Scan ---")

    signals_df, all_signals, selected_signals, match_date_lookup, _match_contexts = run_daily_scan(
        bankroll=args.bankroll,
        mock=args.mock,
        output_path=Path(args.output) if args.output else None,
        auto_log=args.auto_log,
        horizon_hours=args.horizon,
        scan_date_filter=args.date,
        force=args.force,
    )

    if not signals_df.empty:
        print("\n=== Value Bets ===")
        print(signals_df.to_string(index=False))
    else:
        print("\nNo value bets found today.")

    # Write web dashboard JSON — show ALL signals (pre-cap) so user sees full picture
    portfolio = ledger_summary()
    kickoff_map = {
        mid: ctx.get("commence_time", "")
        for mid, ctx in _match_contexts.items()
    }
    # Build schedule from ALL raw API matches (not just those the model processed)
    try:
        import os
        raw_all = fetch_upcoming_matches(api_key=os.getenv("ODDS_API_KEY", ""))
        schedule = [
            {
                "sport": "football",
                "home": m["home_team"],
                "away": m["away_team"],
                "kickoff": m.get("commence_time", ""),
            }
            for m in raw_all
        ]
    except Exception:
        schedule = [
            {"sport": "football", "home": ctx["home"], "away": ctx["away"],
             "kickoff": ctx.get("commence_time", "")}
            for ctx in _match_contexts.values()
        ]
    # Build all_odds from real Odds API data (all 72 WM matches available)
    # Prefer Bet365, fall back to first available bookmaker. Never use DC model for display odds.
    all_odds = {}
    try:
        import os as _os
        _api_key = _os.getenv("ODDS_API_KEY", "")
        import requests as _req
        _r = _req.get(
            "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds",
            params={"apiKey": _api_key, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"},
            timeout=15,
        )
        if _r.ok:
            for _m in _r.json():
                _home, _away = _m["home_team"], _m["away_team"]
                _bk = next((b for b in _m.get("bookmakers", []) if "bet365" in b["key"]), None)
                if not _bk and _m.get("bookmakers"):
                    _bk = _m["bookmakers"][0]
                if not _bk:
                    continue
                _oc = {o["name"]: o["price"] for o in _bk["markets"][0]["outcomes"]}
                all_odds[f"{_home} vs {_away}"] = {
                    "home": round(_oc.get(_home, 0), 2),
                    "draw": round(_oc.get("Draw", 0), 2),
                    "away": round(_oc.get(_away, 0), 2),
                }
            print(f"  Odds API: {len(all_odds)} matches with real bookmaker odds")
    except Exception as _e:
        print(f"  Odds API fetch failed: {_e} — keeping existing odds")
        # Fall back to match_contexts odds if API unavailable
        all_odds = {
            f"{ctx['home']} vs {ctx['away']}": {
                "home": round(ctx.get("odds_home", 0), 2),
                "draw": round(ctx.get("odds_draw", 0), 2),
                "away": round(ctx.get("odds_away", 0), 2),
            }
            for ctx in _match_contexts.values()
            if ctx.get("odds_home", 0) > 1.0
        }
    # Build DC model tips for all schedule games (win/draw/loss probs + xG)
    model_tips = {}
    try:
        import os as _os2
        from src.scanner.daily_scan import _load_latest_dc_params
        from src.models.dixon_coles import predict_match, predict_xg, predict_btts
        from src.config import canonical_name
        _dc_params = _load_latest_dc_params()
        if _dc_params:
            for _g in schedule:
                _h_raw, _a_raw = _g.get("home", ""), _g.get("away", "")
                try:
                    _h = canonical_name(_h_raw)
                    _a = canonical_name(_a_raw)
                    _probs = predict_match(_h, _a, _dc_params, neutral=True)
                    _xgh, _xga = predict_xg(_h, _a, _dc_params, neutral=True)
                    _btts = predict_btts(_h, _a, _dc_params, neutral=True)
                    model_tips[f"{_h_raw} vs {_a_raw}"] = {
                        "p_home": round(_probs["p_home"], 3),
                        "p_draw": round(_probs["p_draw"], 3),
                        "p_away": round(_probs["p_away"], 3),
                        "xg_home": round(_xgh, 2),
                        "xg_away": round(_xga, 2),
                        "p_btts_yes": round(_btts["p_btts_yes"], 3),
                        "p_btts_no": round(_btts["p_btts_no"], 3),
                    }
                except Exception:
                    pass
            print(f"  Model tips: {len(model_tips)} matches computed")
        else:
            print("  Model tips: no DC params found — skipping")
    except Exception as _e:
        print(f"  Model tips failed: {_e}")

    write_signals_json(
        football=all_signals,
        portfolio=portfolio,
        kickoff_map=kickoff_map,
        schedule=schedule,
        all_odds=all_odds,
        model_tips=model_tips if model_tips else None,
    )
    print("Dashboard: docs/data/signals.json updated.")

    # Interactive confirmation (skipped with --auto-log)
    if not args.auto_log and selected_signals:
        confirmed = _confirm_bets(selected_signals, args.bankroll)
        if confirmed:
            n = 0
            for s in confirmed:
                md = match_date_lookup.get(s.match_id, "")
                n += append_bets([s], args.bankroll, LEDGER_PATH, match_date=md)
            print(f"\n{n} Wette(n) ins Ledger eingetragen.")
        else:
            print("\nKeine Wetten eingetragen.")

#!/usr/bin/env python3
"""
Daily tennis scanner — Wimbledon 2026 (30 June – 13 July).

Usage:
  python3 scripts/tennis_scan.py [--mock] [--bankroll 100] [--surface grass] [--tour atp|wta|both]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import requests

from src.data.tennis_data import fetch_atp_matches, fetch_wta_matches
from src.models.tennis_elo import compute_tennis_elo, predict_winner, top_players
from src.betting.tennis_detector import detect_value_tennis
from src.betting.ledger import append_bets, ledger_summary
from src.notifications.telegram import _post
from src.notifications.web_dashboard import write_signals_json

_SPORT_WIMBLEDON = "tennis_atp_wimbledon"
_SPORT_WTA_WIMBLEDON = "tennis_wta_wimbledon"
_ODDS_API_URL = "https://api.the-odds-api.com/v4"

_ATP_MIN_EDGE = 0.10  # ATP Wimbledon backtest: -6.4% ROI → tight market requires 10% edge


def min_edge_for(match_tour: str) -> float:
    """Returns minimum EV edge threshold per tour.
    ATP: 10% (backtest -6.4% ROI — market is efficient)
    WTA: 3%  (backtest +8.5% ROI — market is less efficient)
    """
    from src.config import MIN_EDGE
    return _ATP_MIN_EDGE if match_tour.lower() == "atp" else MIN_EDGE


def _fetch_both_tours() -> "pd.DataFrame":
    import pandas as pd
    try:
        atp = fetch_atp_matches()
    except Exception:
        atp = pd.DataFrame()
    try:
        wta = fetch_wta_matches()
    except Exception:
        wta = pd.DataFrame()
    if atp.empty:
        return wta
    if wta.empty:
        return atp
    return pd.concat([atp, wta], ignore_index=True).sort_values("tourney_date").reset_index(drop=True)


def _fetch_wimbledon_odds(api_key: str, tour: str = "atp") -> list[dict]:
    """
    Fetches live Wimbledon match odds from TheOddsAPI.
    Markets: h2h (match winner) + spreads (set handicap).
    Returns list of match dicts.
    """
    sport = _SPORT_WTA_WIMBLEDON if tour.lower() == "wta" else _SPORT_WIMBLEDON
    url = f"{_ODDS_API_URL}/sports/{sport}/odds"
    params = {
        "apiKey": api_key,
        "regions": "eu",
        "markets": "h2h,spreads,set_winner",
        "oddsFormat": "decimal",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()

    remaining = int(resp.headers.get("x-requests-remaining", 999))
    used = int(resp.headers.get("x-requests-used", 0))
    print(f"  API quota: {used} used / {remaining} remaining")

    if remaining < 20:
        try:
            from src.notifications.telegram import send_quota_alert
            send_quota_alert(remaining)
        except Exception:
            pass

    matches = []
    for event in resp.json():
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        match_id = event.get("id", f"{home}_vs_{away}")
        commence = event.get("commence_time", "")

        best_h2h_home = best_h2h_away = 0.0
        best_spread_home = best_spread_away = 0.0
        best_fs_home = best_fs_away = 0.0

        for bm in event.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                key = mkt.get("key")
                if key == "h2h":
                    for o in mkt.get("outcomes", []):
                        if o["name"] == home:
                            best_h2h_home = max(best_h2h_home, o["price"])
                        elif o["name"] == away:
                            best_h2h_away = max(best_h2h_away, o["price"])
                elif key == "spreads":
                    for o in mkt.get("outcomes", []):
                        if abs(abs(o.get("point", 0)) - 1.5) < 0.1:  # ±1.5 set handicap
                            if o["name"] == home:
                                best_spread_home = max(best_spread_home, o["price"])
                            elif o["name"] == away:
                                best_spread_away = max(best_spread_away, o["price"])
                elif key == "set_winner":
                    for o in mkt.get("outcomes", []):
                        desc = o.get("description", "").lower()
                        if "set 1" in desc or "1st set" in desc:
                            if o["name"] == home:
                                best_fs_home = max(best_fs_home, o["price"])
                            elif o["name"] == away:
                                best_fs_away = max(best_fs_away, o["price"])

        if not best_h2h_home or not best_h2h_away:
            continue

        matches.append({
            "match_id": match_id,
            "commence_time": commence,
            "player_a": home,
            "player_b": away,
            "odds_a": best_h2h_home,
            "odds_b": best_h2h_away,
            "ah_odds_a": best_spread_home,
            "ah_odds_b": best_spread_away,
            "first_set_odds_a": best_fs_home,
            "first_set_odds_b": best_fs_away,
            "tour": tour.lower(),
        })

    return matches


def _mock_wimbledon_matches() -> list[dict]:
    """Synthetic Wimbledon matches for dry-run testing (ATP + WTA)."""
    return [
        {
            "match_id": "mock_alcaraz_djokovic",
            "commence_time": "2026-07-06T13:00:00Z",
            "player_a": "Carlos Alcaraz",
            "player_b": "Novak Djokovic",
            "odds_a": 1.75,
            "odds_b": 2.10,
            "ah_odds_a": 2.00,
            "ah_odds_b": 1.85,
            "first_set_odds_a": 1.72,
            "first_set_odds_b": 2.10,
            "tour": "atp",
        },
        {
            "match_id": "mock_sinner_medvedev",
            "commence_time": "2026-07-07T11:00:00Z",
            "player_a": "Jannik Sinner",
            "player_b": "Daniil Medvedev",
            "odds_a": 1.55,
            "odds_b": 2.55,
            "ah_odds_a": 2.20,
            "ah_odds_b": 1.65,
            "first_set_odds_a": 1.68,
            "first_set_odds_b": 2.20,
            "tour": "atp",
        },
        {
            "match_id": "mock_swiatek_sabalenka",
            "commence_time": "2026-07-06T11:00:00Z",
            "player_a": "Iga Swiatek",
            "player_b": "Aryna Sabalenka",
            "odds_a": 1.90,
            "odds_b": 2.00,
            # WTA BO3: ah-1.5_a = wins 2:0 straight sets (~23% for near-even match)
            # Realistic: odds ~4.00/1.30 (not ATP-style 2.10/1.80)
            "ah_odds_a": 4.00,
            "ah_odds_b": 1.30,
            "first_set_odds_a": 1.92,
            "first_set_odds_b": 1.95,
            "tour": "wta",
        },
    ]


def _tennis_market_label(market: str, player_a: str, player_b: str) -> str:
    labels = {
        "home":        f"Match Winner: {player_a}",
        "away":        f"Match Winner: {player_b}",
        "ah-1.5_a":   f"{player_a} gewinnt 3:0 oder 3:1 (Set AH -1.5)",
        "ah+1.5_b":   f"{player_b} gewinnt oder verliert max. 1 Satz (Set AH +1.5)",
        "first_set_a": f"1. Satz: {player_a} gewinnt",
        "first_set_b": f"1. Satz: {player_b} gewinnt",
    }
    return labels.get(market, market)


def _format_report(
    signals: list,
    scan_date: str,
    surface: str,
    top_grass: list,
) -> str:
    lines = [
        f"# Tennis Scan — Wimbledon {scan_date}",
        f"Surface: {surface.upper()}",
        "",
    ]

    if not signals:
        lines.append("*No value bets found today.*")
    else:
        for s in sorted(signals, key=lambda x: x.ev, reverse=True):
            ev_pct = s.ev * 100
            lines += [
                f"## {s.home} vs {s.away}",
                f"Market:  {_tennis_market_label(s.market, s.home, s.away)}",
                f"Odds:    {s.decimal_odds:.2f}",
                f"Model:   {s.model_prob*100:.1f}%  EV: +{ev_pct:.1f}%  ({s.confidence})",
                f"Stake:   {s.stake_eur:.2f} EUR",
                "",
            ]

    lines += [
        "---",
        "## Top Grass Elo",
    ]
    for name, rating in top_grass:
        lines.append(f"  {name}: {rating:.0f}")

    lines += [
        "",
        "---",
        "## Backtest (2021-2025, Max Odds, p≥35%)",
        "  WTA Wimbledon:   +8.5% ROI  ← primary focus (216 Bets, 2021-2025)",
        "  ATP Wimbledon:   -6.4% ROI",
        "  WTA overall:     +6.9% ROI",
        "  Clay/FO:         -7.8% ROI  ← avoid",
    ]

    return "\n".join(lines)


def _send_tennis_alert(signals: list, scan_date: str, summary: dict, tour: str = "atp") -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    SEP = "─────────────────────"
    tour_label = {"atp": "Herren", "wta": "Damen", "both": "Herren + Damen"}.get(tour, "")
    lines = [f"<b>🎾 Wimbledon {tour_label} — {scan_date}</b>", SEP]

    if not signals:
        lines.append("<i>Keine Tennis Value Bets heute.</i>")
    else:
        for s in sorted(signals, key=lambda x: x.ev, reverse=True)[:5]:
            mkt_label = _tennis_market_label(s.market, s.home, s.away)

            lines += [
                f"<b>{s.home} vs {s.away}</b>",
                f"Tipp:    {mkt_label}",
                f"Quote:   {s.decimal_odds:.2f}",
                f"Modell:  {s.model_prob*100:.1f}%  EV: +{s.ev*100:.1f}%",
                f"Einsatz: {s.stake_eur:.2f} EUR",
                SEP,
            ]

    n_open = summary.get("n_open", 0)
    pnl = summary.get("total_pnl", 0.0)
    roi = summary.get("roi_pct", 0.0)
    lines.append(f"<b>Portfolio:</b> {n_open} aktiv   G/V: {pnl:+.2f} EUR   ROI: {roi:+.1f}%")

    _post(token, chat_id, "\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="Tennis value bet scanner (Wimbledon)")
    parser.add_argument("--mock", action="store_true", help="Use mock data (no API call)")
    parser.add_argument("--bankroll", type=float, default=100.0)
    parser.add_argument("--surface", default="grass", choices=["grass", "clay", "hard"])
    parser.add_argument("--no-ledger", action="store_true", help="Skip writing to ledger")
    parser.add_argument("--no-telegram", action="store_true", help="Skip Telegram notification")
    parser.add_argument("--tour", default="both", choices=["atp", "wta", "both"],
                        help="ATP, WTA oder beide (default: both — WTA hat +8.5%% ROI Wimbledon)")
    args = parser.parse_args()

    scan_date = datetime.now().strftime("%Y-%m-%d")
    print(f"Tennis Scan — {scan_date} (surface: {args.surface}, tour: {args.tour})")

    # Wimbledon date guard
    today = date.today()
    wimbledon_active = date(2026, 6, 29) <= today <= date(2026, 7, 13)
    if not wimbledon_active and not args.mock:
        print(f"Wimbledon nicht aktiv (heute: {today}). Nutze --mock für Tests.")

    # 1. Load historical match data and compute Elo ratings
    tour_label_load = "ATP" if args.tour == "atp" else ("WTA" if args.tour == "wta" else "ATP+WTA")
    print(f"Loading {tour_label_load} match data...")
    try:
        if args.tour == "both":
            matches = _fetch_both_tours()
        else:
            from src.data.tennis_data import fetch_matches
            matches = fetch_matches(args.tour)
        print(f"  {len(matches)} matches loaded")
    except Exception as e:
        print(f"  ERROR loading match data: {e}")
        matches = None

    if matches is not None and not matches.empty:
        print("Computing surface-adjusted Elo ratings (recency-weighted)...")
        ratings = compute_tennis_elo(matches, reference_date=datetime.now())
        top_grass = top_players(ratings, surface="grass", n=10)
        print(f"  Top grass Elo: {top_grass[0][0] if top_grass else 'n/a'}")
    else:
        print("  WARNING: No match data — using default Elo ratings.")
        from src.models.tennis_elo import TennisEloRatings
        ratings = TennisEloRatings()
        top_grass = []

    # 2. Fetch upcoming Wimbledon odds
    if args.mock:
        print("Loading mock Wimbledon matches...")
        upcoming = _mock_wimbledon_matches()
    elif args.tour == "both":
        api_key = os.getenv("ODDS_API_KEY", "")
        if not api_key:
            print("ERROR: ODDS_API_KEY not set.")
            sys.exit(1)
        print("Fetching Wimbledon odds (ATP + WTA) from TheOddsAPI...")
        try:
            upcoming_atp = _fetch_wimbledon_odds(api_key, tour="atp")
            upcoming_wta = _fetch_wimbledon_odds(api_key, tour="wta")
            upcoming = upcoming_atp + upcoming_wta
        except Exception as e:
            print(f"ERROR fetching odds: {e}")
            sys.exit(1)
    else:
        api_key = os.getenv("ODDS_API_KEY", "")
        if not api_key:
            print("ERROR: ODDS_API_KEY not set.")
            sys.exit(1)
        print(f"Fetching Wimbledon odds ({args.tour.upper()}) from TheOddsAPI...")
        try:
            upcoming = _fetch_wimbledon_odds(api_key, tour=args.tour)
        except Exception as e:
            print(f"ERROR fetching odds: {e}")
            sys.exit(1)

    print(f"  {len(upcoming)} upcoming matches found")

    # 3. Predict and detect value
    all_signals = []
    for m in upcoming:
        pa, pb = m["player_a"], m["player_b"]
        probs = predict_winner(pa, pb, ratings, args.surface)
        match_tour = m.get("tour", args.tour)

        signals = detect_value_tennis(
            player_a=pa,
            player_b=pb,
            probs=probs,
            odds_a=m["odds_a"],
            odds_b=m["odds_b"],
            bankroll=args.bankroll,
            match_id=m["match_id"],
            ah_odds_a=m.get("ah_odds_a", 0.0),
            ah_odds_b=m.get("ah_odds_b", 0.0),
            first_set_odds_a=m.get("first_set_odds_a", 0.0),
            first_set_odds_b=m.get("first_set_odds_b", 0.0),
            min_edge=min_edge_for(match_tour),
            tour=match_tour,
        )

        if signals:
            for s in signals:
                print(f"  VALUE: {pa} vs {pb} — {s.market}  EV:{s.ev*100:.1f}%  odds:{s.decimal_odds:.2f}")
        else:
            print(f"  No value: {pa} vs {pb}  (p_a={probs['p_a']*100:.1f}%)")

        all_signals.extend(signals)

    print(f"\n{len(all_signals)} value signal(s) found.")

    # 4. Write report
    report_path = ROOT / "results" / f"tennis_scan_{scan_date}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = _format_report(all_signals, scan_date, args.surface, top_grass)
    report_path.write_text(report)
    print(f"Report: {report_path}")

    # 5. Write to ledger
    if all_signals and not args.no_ledger:
        n = append_bets(all_signals, args.bankroll)
        print(f"Ledger: {n} new bet(s) recorded.")

    # 6. Telegram notification
    if not args.no_telegram:
        summary = ledger_summary()
        _send_tennis_alert(all_signals, scan_date, summary, tour=args.tour)
        print("Telegram: notification sent.")

    # 7. Write web dashboard JSON (with per-match tour info + kickoff times)
    dashboard_summary = ledger_summary()
    match_tour_map = {m["match_id"]: m.get("tour", args.tour) for m in upcoming}
    kickoff_map = {m["match_id"]: m.get("commence_time", "") for m in upcoming}
    tennis_schedule = [
        {
            "sport": "tennis",
            "home": m["player_a"],
            "away": m["player_b"],
            "kickoff": m.get("commence_time", ""),
            "tour": m.get("tour", ""),
        }
        for m in upcoming
    ]
    write_signals_json(
        tennis=all_signals,
        portfolio=dashboard_summary,
        top_elo=top_grass,
        tennis_tour_map=match_tour_map,
        kickoff_map=kickoff_map,
        schedule=tennis_schedule,
    )
    print("Dashboard: docs/data/signals.json updated.")

    # Print summary
    print("\n--- Top Grass Elo ---")
    for name, rating in top_grass[:5]:
        print(f"  {name}: {rating:.0f}")


if __name__ == "__main__":
    main()

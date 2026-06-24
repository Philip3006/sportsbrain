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
from src.notifications.web_dashboard import write_signals_json, write_signals_json_all_users
from src.data.odds_api import fetch_upcoming_matches, fetch_wm_scores
from src.data.football_discovery import discover_active_leagues


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


ROOT = Path(__file__).parent.parent

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

    # I11: Football Liga Auto-Discovery — loggt aktive Ligen (post-WM genutzt für Multi-Liga-Scan)
    import os as _os_disc
    _active_leagues = discover_active_leagues(api_key=_os_disc.getenv("ODDS_API_KEY", ""))
    if _active_leagues:
        print(f"Football Discovery: {len(_active_leagues)} aktive Ligen — {', '.join(_active_leagues[:5])}{'...' if len(_active_leagues) > 5 else ''}")

    signals_df, all_signals, selected_signals, match_date_lookup, _match_contexts = run_daily_scan(
        bankroll=args.bankroll,
        mock=args.mock,
        output_path=Path(args.output) if args.output else None,
        auto_log=args.auto_log,
        horizon_hours=args.horizon,
        scan_date_filter=args.date,
        force=args.force,
    )

    from datetime import date as _date
    from src.scanner.output import archive_signals
    _scan_ts = args.date or _date.today().isoformat()
    _selected_ids = {(s.match_id, s.market) for s in selected_signals}
    _n_archived = archive_signals(all_signals, _selected_ids, _scan_ts, sport="football")
    if _n_archived:
        print(f"Archive: {_n_archived} neue Football-Signals archiviert.")

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
    # Build all_odds from already-fetched raw_all (reuse API call, no extra quota cost).
    # Seed from existing signals.json so single-match failures don't wipe previous data.
    all_odds = {}
    try:
        import json as _json0
        _existing_sig = _json0.loads((ROOT / "docs" / "data" / "signals.json").read_text()) if (ROOT / "docs" / "data" / "signals.json").exists() else {}
        all_odds = _existing_sig.get("all_odds", {})
    except Exception:
        pass
    try:
        for _m in raw_all:
            _home, _away = _m["home_team"], _m["away_team"]
            _mk = f"{_home} vs {_away}"
            _tl = _m.get("totals_lines", {})
            _sp = _m.get("spreads", {})
            _entry: dict = {
                "home":     round(_m.get("home_odds", 0), 2),
                "draw":     round(_m.get("draw_odds", 0), 2),
                "away":     round(_m.get("away_odds", 0), 2),
                "over25":   round(_m.get("over_odds", 0), 2),
                "under25":  round(_m.get("under_odds", 0), 2),
                "over15":   round(_m.get("over15_odds", 0), 2),
                "under15":  round(_m.get("under15_odds", 0), 2),
                "over35":   round(_m.get("over35_odds", 0), 2),
                "under35":  round(_m.get("under35_odds", 0), 2),
                "btts_yes": round(_m.get("btts_yes_odds", 0), 2),
                "btts_no":  round(_m.get("btts_no_odds", 0), 2),
            }
            # Quarter-ball O/U lines (1.75, 2.25, 2.75, etc.)
            for _pt, _sides in _tl.items():
                _key_o = f"over{str(float(_pt)).replace('.', '')}"
                _key_u = f"under{str(float(_pt)).replace('.', '')}"
                if _sides.get("over", 0) > 1: _entry[_key_o] = round(_sides["over"], 2)
                if _sides.get("under", 0) > 1: _entry[_key_u] = round(_sides["under"], 2)
            # AH lines
            for _line, _sides in _sp.items():
                if _sides.get("home", 0) > 1: _entry[f"ah{_line}_home"] = round(_sides["home"], 2)
                if _sides.get("away", 0) > 1: _entry[f"ah{_line}_away"] = round(_sides["away"], 2)
            # Per-Bookmaker h2h (Bookie-Matrix) — Top 10
            _bm_h2h = _m.get("bookmakers_h2h", []) or []
            if _bm_h2h:
                _entry["bookmakers_h2h"] = _bm_h2h[:10]
            if _entry["home"] > 1:
                all_odds[_mk] = _entry
        print(f"  Odds API: {len(all_odds)} matches with enriched bookmaker odds")
    except Exception as _e:
        print(f"  Odds API odds enrichment failed: {_e} — keeping existing odds")

    # Log daily odds snapshot for line movement tracking
    try:
        import json as _json2
        from datetime import datetime as _dt2
        _hist_path = ROOT / "data" / "odds_history.json"
        _today_snap = {
            "ts": _dt2.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date": _dt2.utcnow().strftime("%Y-%m-%d"),
            "odds": {k: {"home": v["home"], "draw": v["draw"], "away": v["away"]} for k, v in all_odds.items()},
        }
        # Load existing history, append today's snapshot (keep last 14 entries)
        _hist = []
        if _hist_path.exists():
            try:
                _hist = _json2.loads(_hist_path.read_text())
            except Exception:
                _hist = []
        # Remove duplicate for today
        _hist = [s for s in _hist if s.get("date") != _today_snap["date"]]
        _hist.append(_today_snap)
        _hist = _hist[-14:]  # keep last 14 days
        _hist_path.write_text(_json2.dumps(_hist, ensure_ascii=False, indent=2))
        print(f"  Odds history: {len(_hist)} snapshots logged")
    except Exception as _e:
        print(f"  Odds history log failed: {_e}")

    # Build odds_history dict for dashboard: {match_key: [{date, home, draw, away}, ...]}
    _odds_hist_for_dashboard = {}
    try:
        _hist_data = _json2.loads((ROOT / "data" / "odds_history.json").read_text()) if (ROOT / "data" / "odds_history.json").exists() else []
        for _snap in _hist_data:
            for _mk, _od in _snap.get("odds", {}).items():
                if _mk not in _odds_hist_for_dashboard:
                    _odds_hist_for_dashboard[_mk] = []
                _odds_hist_for_dashboard[_mk].append({
                    "date": _snap["date"],
                    "home": _od.get("home", 0),
                    "draw": _od.get("draw", 0),
                    "away": _od.get("away", 0),
                })
    except Exception:
        _odds_hist_for_dashboard = {}
    # Build DC model tips for all schedule games (win/draw/loss probs + xG + goalscorers)
    model_tips = {}
    try:
        import os as _os2
        import re as _re
        import unicodedata as _ud
        import json as _jsc
        from src.scanner.prep import _load_latest_dc_params
        from src.models.dixon_coles import predict_match, predict_xg, predict_btts, predict_totals, predict_scoreline
        from src.analysis.monte_carlo import scoreline_distribution
        from src.config import canonical_name, DATA_CACHE
        from src.betting.goalscorer import get_top_goalscorer_predictions
        import pickle as _pkl
        _dc_params = _load_latest_dc_params()

        # Load player xG cache for goalscorer predictions
        _pxg_path = DATA_CACHE / "statsbomb_player_xg.pkl"
        _player_xg_df = None
        if _pxg_path.exists():
            try:
                _player_xg_df = _pkl.load(open(_pxg_path, "rb"))
            except Exception:
                pass

        # Load squads.json for current-squad filtering and clean name lookup
        _squads_data = {}
        try:
            _sq_path = ROOT / "docs" / "data" / "squads.json"
            if _sq_path.exists():
                _squads_data = _jsc.loads(_sq_path.read_text()).get("teams", {})
        except Exception:
            pass

        def _asciify(s: str) -> list:
            """ASCII-normalize and split into lowercase word tokens."""
            s = _ud.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
            return _re.split(r"[\s\-]+", s)

        def _match_player(sb_name: str, eligible: dict) -> str | None:
            """
            Match a StatsBomb full name to a squad name using two-tier priority:
            1. Last name (≥4 chars) in StatsBomb tokens, PLUS first name if ≥4 chars also matches
               → prevents same-surname false positives (e.g. Moteb Al-Harbi ≠ Fahad Al-Harbi)
            2. All squad tokens (≥3 chars) appear in StatsBomb name (handles short last names)
            Returns squad name on match, None otherwise.
            """
            sb_parts = _asciify(sb_name)
            sb_set = set(sb_parts)
            for sq_name, sq_parts in eligible.items():
                last = sq_parts[-1] if sq_parts else ""
                first = sq_parts[0] if len(sq_parts) >= 2 else ""
                # 1. Last name match (most reliable) — also require first name if it's long enough
                if last and len(last) >= 4 and last in sb_set:
                    if not first or len(first) < 4 or first in sb_set:
                        return sq_name
                # 2. All squad tokens (≥3 chars) appear in StatsBomb name (e.g. Korean names, short surnames)
                sq_short = {t for t in sq_parts if len(t) >= 3}
                if len(sq_short) >= 2 and sq_short <= sb_set:
                    return sq_name
            return None

        def _filter_by_squad(preds: list, team: str) -> list:
            """
            Keeps only players in the current squad (not injured/out).
            Uses squad name for display (shorter, cleaner than StatsBomb full name).
            """
            sq_info = _squads_data.get(team, {})
            sq_players = sq_info.get("players", [])
            if not sq_players:
                return [{"name": p["player"], "p": round(p["p_score"], 3)} for p in preds]
            eligible = {
                p["name"]: _asciify(p["name"])
                for p in sq_players
                if p.get("status") not in ("injured", "out")
                and p.get("pos") != "GK"
            }
            seen: dict[str, float] = {}
            for pred in preds:
                sq_name = _match_player(pred["player"], eligible)
                if sq_name and sq_name not in seen:
                    seen[sq_name] = round(pred["p_score"], 3)
            return [{"name": n, "p": p} for n, p in seen.items()]

        import pandas as _pd_tip
        _now_ts = _pd_tip.Timestamp.now()

        if _dc_params:
            for _g in schedule:
                _h_raw, _a_raw = _g.get("home", ""), _g.get("away", "")
                try:
                    _h = canonical_name(_h_raw)
                    _a = canonical_name(_a_raw)
                    _probs = predict_match(_h, _a, _dc_params, neutral=True)
                    _xgh, _xga = predict_xg(_h, _a, _dc_params, neutral=True)
                    _btts = predict_btts(_h, _a, _dc_params, neutral=True)
                    _totals = predict_totals(_h, _a, _dc_params, neutral=True)

                    # Top scorelines via Poisson/DC matrix
                    _top_scores = []
                    try:
                        _matrix = predict_scoreline(_h, _a, _dc_params, neutral=True)
                        _sl = scoreline_distribution(_matrix)
                        _top_scores = _sl["top_scores"]
                    except Exception:
                        pass

                    # Goalscorer predictions — squad-filtered, opponent-adjusted xG
                    _home_sc, _away_sc = [], []
                    if _player_xg_df is not None:
                        try:
                            _raw_h = get_top_goalscorer_predictions(
                                _h, _now_ts, _player_xg_df,
                                n_games=5, top_n=8, dc_params=_dc_params,
                            )
                            _raw_a = get_top_goalscorer_predictions(
                                _a, _now_ts, _player_xg_df,
                                n_games=5, top_n=8, dc_params=_dc_params,
                            )
                            _home_sc = _filter_by_squad(_raw_h, _h)[:3]
                            _away_sc = _filter_by_squad(_raw_a, _a)[:3]
                        except Exception:
                            pass

                    model_tips[f"{_h_raw} vs {_a_raw}"] = {
                        "p_home": round(_probs["p_home"], 3),
                        "p_draw": round(_probs["p_draw"], 3),
                        "p_away": round(_probs["p_away"], 3),
                        "xg_home": round(_xgh, 2),
                        "xg_away": round(_xga, 2),
                        "p_btts_yes": round(_btts["p_btts_yes"], 3),
                        "p_btts_no": round(_btts["p_btts_no"], 3),
                        "p_over25": round(_totals["p_over"], 3),
                        "p_under25": round(_totals["p_under"], 3),
                        "top_scorers_home": _home_sc,
                        "top_scorers_away": _away_sc,
                        "top_scores": _top_scores,
                    }
                except Exception:
                    pass
            print(f"  Model tips: {len(model_tips)} matches computed")
        else:
            print("  Model tips: no DC params found — skipping")
    except Exception as _e:
        print(f"  Model tips failed: {_e}")

    # Build open_bets section from ledger
    import csv as _csv
    _open_bets = []
    try:
        from src.betting.ledger import LEDGER_PATH as _ledger_path
        if _ledger_path.exists():
            with open(_ledger_path) as _f:
                for _row in _csv.DictReader(_f):
                    if _row.get("status") != "open":
                        continue
                    _home, _away = _row["home"], _row["away"]
                    _mk = f"{_home} vs {_away}"
                    _market = _row["market"]
                    _entry = float(_row["decimal_odds"])
                    _stake = float(_row["stake_amount"])
                    # Current odds lookup
                    _cur_odds_block = all_odds.get(_mk, {})
                    _cur = None
                    if _market in ("home", "draw", "away"):
                        _cur = _cur_odds_block.get(_market)
                    # Drift
                    _drift = round((_cur - _entry) / _entry * 100, 1) if _cur else None
                    # CLV signal: quote fällt = gut (Markt bestätigt uns)
                    _clv = "good" if (_drift is not None and _drift < 0) else ("bad" if _drift is not None else None)
                    # Model prob
                    _tip = (model_tips or {}).get(_mk, {})
                    _model_p = _tip.get(f"p_{_market}") if _market in ("home", "draw", "away") else None
                    _model_edge = round((_model_p * _cur - 1) * 100, 1) if (_model_p and _cur) else None
                    _open_bets.append({
                        "match": _mk,
                        "home": _home,
                        "away": _away,
                        "market": _market,
                        "entry_odds": _entry,
                        "current_odds": _cur,
                        "drift_pct": _drift,
                        "clv_signal": _clv,
                        "stake": _stake,
                        "match_date": _row.get("match_date", ""),
                        "model_edge_pct": _model_edge,
                    })
        print(f"  Open bets: {len(_open_bets)} loaded")
    except Exception as _e:
        print(f"  Open bets build failed: {_e}")

    # Fetch completed WM 2026 scores (cached 30 min, graceful fail)
    _wm_results = []
    if not args.mock:
        try:
            import os as _os3
            _wm_results = fetch_wm_scores(api_key=_os3.getenv("ODDS_API_KEY", ""))
            if _wm_results:
                print(f"  Scores: {len(_wm_results)} completed match(es) fetched")
        except Exception as _e:
            print(f"  Scores fetch failed: {_e}")

    write_signals_json_all_users(
        football=all_signals,
        portfolio=portfolio,
        kickoff_map=kickoff_map,
        schedule=schedule,
        all_odds=all_odds,
        model_tips=model_tips if model_tips else None,
        # open_bets pinned to philip's view at scan time; per-user write reads
        # each user's own ledger inside write_signals_json when None.
        open_bets=None,
        odds_history=_odds_hist_for_dashboard,
        wm_results=_wm_results if _wm_results else None,
    )
    print("Dashboard: docs/data/signals_{user}.json updated (all known users).")

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

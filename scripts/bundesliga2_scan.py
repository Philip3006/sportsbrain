"""Phase D — 2. Bundesliga Prematch-Scanner.

Fetcht Fixtures via TheOddsAPI (`soccer_germany_bundesliga2`), rechnet
DC+Elo-Ensemble, detektiert Value auf 1X2 / AH / O/U / BTTS / DC / goals_2_4,
schreibt Ledger-Zeilen mit league='bl2' und pusht Signals.

Unknown-Team-Gate (analog Tennis): Team muss ≥ 5 Matches in Universe haben.
Coverage-Gate: min 3 Bookies für 1X2, sonst no_bet_flag.

Usage:
  python scripts/bundesliga2_scan.py --bankroll 100
  python scripts/bundesliga2_scan.py --mock                # synthetische Fixtures (Testpfad)
  python scripts/bundesliga2_scan.py --auto-log            # schreibt direkt in Ledger
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.config import (
    DATA_CACHE, MODELS_DIR, RESULTS_DIR, canonical_name, MAX_ACTIVE_BETS,
    league_config, MIN_EDGE,
)
from src.models import dixon_coles
from src.models.elo import ELO_DEFAULT
from src.betting.value_detector import (
    detect_value, detect_value_ah, detect_value_totals, detect_value_btts,
    detect_value_double_chance, detect_value_goals_range,
)
from src.betting.ledger import append_bets, count_open_bets, LEDGER_PATH, ledger_summary
from src.data.odds_api import fetch_upcoming_matches

SPORT_KEY = "soccer_germany_bundesliga2"
LEAGUE_SHORT = "bl2"
MIN_TEAM_MATCHES = 5
MIN_BOOKIES = 3


def _load_dc_params():
    p = MODELS_DIR / "dc_bundesliga2" / "params_latest.pkl"
    if not p.exists():
        raise FileNotFoundError(f"DC-Model fehlt: {p}. Erst train_dc_bundesliga2.py laufen lassen.")
    return dixon_coles.load(p)


def _load_elo() -> dict[str, float]:
    p = DATA_CACHE / "elo_ratings_bl2.json"
    if not p.exists():
        raise FileNotFoundError(f"Elo fehlt: {p}. Erst train_elo_bundesliga2.py laufen lassen.")
    return {k: float(v) for k, v in json.loads(p.read_text()).items()}


def _load_universe() -> dict[str, dict]:
    p = DATA_CACHE / "bundesliga2_universe.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _team_matches_in_universe(team: str, universe: dict) -> int:
    """Anzahl Saisons + rough Anzahl Matches (~34 pro Saison)."""
    info = universe.get(team)
    if not info:
        return 0
    return len(info.get("seasons_played", [])) * 34


def _consensus_odds(bookmakers: list, market_key: str, outcome_name: str) -> tuple[float, int]:
    """Median-Quote + Bookie-Count für ein Outcome eines Marktes."""
    prices: list[float] = []
    for bm in bookmakers or []:
        for m in bm.get("markets", []):
            if m.get("key") != market_key:
                continue
            for o in m.get("outcomes", []):
                if o.get("name") == outcome_name:
                    price = float(o.get("price", 0))
                    if price > 1.0:
                        prices.append(price)
    if not prices:
        return 0.0, 0
    return float(np.median(prices)), len(prices)


def _mock_matches() -> list[dict]:
    """Synthetische Testfixtures — Spieltag 1 26/27 (approximiert aus Team-Universe)."""
    teams = json.loads((DATA_CACHE / "bundesliga2_current_teams.json").read_text())["teams"]
    if len(teams) < 4:
        return []
    kickoff = datetime.now(timezone.utc).replace(hour=13, minute=30, second=0, microsecond=0)
    pairs = list(zip(teams[::2], teams[1::2]))
    matches = []
    for i, (h, a) in enumerate(pairs[:9]):
        matches.append({
            "match_id": f"bl2_mock_{i}",
            "home_team": h,
            "away_team": a,
            "commence_time": (kickoff + pd.Timedelta(hours=i * 2)).isoformat(),
            "bookmakers": [
                {"key": "mock1", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": h, "price": 2.10},
                        {"name": "Draw", "price": 3.40},
                        {"name": a, "price": 3.10},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": 1.95, "point": 2.5},
                        {"name": "Under", "price": 1.85, "point": 2.5},
                    ]},
                ]},
                {"key": "mock2", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": h, "price": 2.05},
                        {"name": "Draw", "price": 3.50},
                        {"name": a, "price": 3.20},
                    ]},
                ]},
                {"key": "mock3", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": h, "price": 2.15},
                        {"name": "Draw", "price": 3.30},
                        {"name": a, "price": 3.00},
                    ]},
                ]},
            ],
        })
    return matches


def _scan_match(
    match: dict,
    params,
    elo: dict,
    universe: dict,
    bankroll: float,
    min_edge: float,
) -> tuple[list, dict]:
    """Rechnet Signals + Diagnostics-Info für ein Match. Returns (signals, meta)."""
    home_raw = str(match.get("home_team", ""))
    away_raw = str(match.get("away_team", ""))
    home = canonical_name(home_raw)
    away = canonical_name(away_raw)
    mid = match.get("match_id", f"{home}_vs_{away}")
    meta: dict = {"home": home, "away": away, "match_id": mid}

    # Unknown-Team-Gate
    h_matches = _team_matches_in_universe(home, universe)
    a_matches = _team_matches_in_universe(away, universe)
    if h_matches < MIN_TEAM_MATCHES or a_matches < MIN_TEAM_MATCHES:
        meta["skip"] = "unknown_team_gate"
        meta["h_matches"] = h_matches
        meta["a_matches"] = a_matches
        return [], meta

    # DC-Prediction mit Elo-Adjustment
    if home not in params.attack or away not in params.attack:
        meta["skip"] = "team_not_in_dc"
        return [], meta
    elo_h = elo.get(home, ELO_DEFAULT)
    elo_a = elo.get(away, ELO_DEFAULT)
    dc_probs = dixon_coles.predict_match(
        home, away, params, elo_home=elo_h, elo_away=elo_a,
    )
    meta["dc_probs"] = dc_probs
    meta["elo"] = {"home": elo_h, "away": elo_a}

    # Odds-Extraktion + Coverage-Gate
    h_price, h_n = _consensus_odds(match.get("bookmakers", []), "h2h", home_raw)
    d_price, d_n = _consensus_odds(match.get("bookmakers", []), "h2h", "Draw")
    a_price, a_n = _consensus_odds(match.get("bookmakers", []), "h2h", away_raw)
    n_bookies = max(h_n, d_n, a_n)
    meta["n_bookies_1x2"] = n_bookies
    meta["odds_1x2"] = (h_price, d_price, a_price)
    no_bet_flag = n_bookies < MIN_BOOKIES

    signals = []
    if h_price > 0 and d_price > 0 and a_price > 0:
        model_probs = np.array([dc_probs["p_away"], dc_probs["p_draw"], dc_probs["p_home"]])
        s1x2 = detect_value(
            home, away, model_probs, (h_price, d_price, a_price),
            bankroll=bankroll, min_edge=min_edge, match_id=mid, dc_probs=dc_probs,
        )
        signals.extend(s1x2)

    # O/U 2.5 (mit DC-Sim)
    try:
        totals = dixon_coles.predict_totals(home, away, params, 2.5,
                                             elo_home=elo_h, elo_away=elo_a)
        ou_over, ou_over_n = _consensus_odds(match.get("bookmakers", []), "totals", "Over")
        ou_under, ou_under_n = _consensus_odds(match.get("bookmakers", []), "totals", "Under")
        if ou_over > 0 and ou_under > 0:
            s_ou = detect_value_totals(
                home, away, totals, ou_over, ou_under,
                bankroll=bankroll, min_edge=min_edge, match_id=mid, line=2.5,
            )
            signals.extend(s_ou)
    except Exception as exc:
        meta["totals_err"] = str(exc)

    # AH -0.5 (Standard-Line)
    try:
        ah = dixon_coles.predict_asian_handicap(home, away, params, -0.5,
                                                 elo_home=elo_h, elo_away=elo_a)
        # TheOddsAPI liefert spreads mit `point` — Konsens auf line=-0.5
        ah_home_price, ah_away_price = 0.0, 0.0
        for bm in match.get("bookmakers", []):
            for m in bm.get("markets", []):
                if m.get("key") != "spreads":
                    continue
                for o in m.get("outcomes", []):
                    point = float(o.get("point", 0))
                    if abs(point + 0.5) < 0.01 and o.get("name") == home_raw:
                        ah_home_price = float(o.get("price", 0))
                    elif abs(point - 0.5) < 0.01 and o.get("name") == away_raw:
                        ah_away_price = float(o.get("price", 0))
        if ah_home_price > 0 and ah_away_price > 0:
            s_ah = detect_value_ah(
                home, away, ah, ah_home_price, ah_away_price,
                bankroll=bankroll, min_edge=min_edge, match_id=mid, line=-0.5,
            )
            signals.extend(s_ah)
    except Exception as exc:
        meta["ah_err"] = str(exc)

    # BTTS + Double Chance: TheOddsAPI liefert das je nach Provider — best effort
    try:
        btts = dixon_coles.predict_btts(home, away, params,
                                         elo_home=elo_h, elo_away=elo_a)
        btts_yes, _ = _consensus_odds(match.get("bookmakers", []), "btts", "Yes")
        btts_no, _ = _consensus_odds(match.get("bookmakers", []), "btts", "No")
        if btts_yes > 0 and btts_no > 0:
            s_btts = detect_value_btts(
                home, away, btts, btts_yes, btts_no,
                bankroll=bankroll, min_edge=min_edge, match_id=mid,
            )
            signals.extend(s_btts)
    except Exception as exc:
        meta["btts_err"] = str(exc)

    # goals_2_4 (Poisson-Sim aus scoreline-Matrix)
    try:
        matrix = dixon_coles.predict_scoreline(home, away, params,
                                                 elo_home=elo_h, elo_away=elo_a)
        # P(total ∈ {2,3,4})
        p_range = 0.0
        for i in range(matrix.shape[0]):
            for j in range(matrix.shape[1]):
                if 2 <= i + j <= 4:
                    p_range += float(matrix[i, j])
        # goals_2_4 Odds: nicht Standard-Markt bei TheOddsAPI — skip wenn keine
        # Datenquelle vorhanden. Signale kommen erst mit WebSearch-Fallback (Phase D2 backlog)
        meta["p_goals_2_4"] = p_range
    except Exception as exc:
        meta["goals_range_err"] = str(exc)

    # Coverage-Gate: alle Signale mit no_bet_flag markieren
    if no_bet_flag:
        for s in signals:
            s.no_bet_flag = True
            s.conflict_reason = f"low_coverage_{n_bookies}bookies"
        meta["coverage_gate"] = f"<{MIN_BOOKIES}_bookies"

    # League-Tag setzen (Ledger-Filter)
    for s in signals:
        s.league = LEAGUE_SHORT

    meta["n_signals"] = len(signals)
    return signals, meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll", type=float, default=100.0)
    ap.add_argument("--mock", action="store_true", help="synthetische Fixtures (kein API-Call)")
    ap.add_argument("--auto-log", action="store_true", help="schreibt Signale direkt in Ledger")
    ap.add_argument("--min-edge", type=float, default=None, help="Override min_edge aus Registry")
    args = ap.parse_args()

    cfg = league_config(SPORT_KEY) or {}
    min_edge = args.min_edge if args.min_edge is not None else cfg.get("min_edge", MIN_EDGE)

    print(f"=== 2. Bundesliga Scan | bankroll={args.bankroll:.0f} | min_edge={min_edge:.1%} ===")
    params = _load_dc_params()
    elo = _load_elo()
    universe = _load_universe()

    if args.mock:
        matches = _mock_matches()
        print(f"MOCK: {len(matches)} synthetische Matches")
    else:
        matches = fetch_upcoming_matches(sport=SPORT_KEY, markets="h2h,totals,spreads,btts") or []
        print(f"API: {len(matches)} Fixtures")
        if not matches:
            print("  Keine Fixtures — 2.BL offseason oder API-Quota erschöpft. Nutze --mock zum Testen.")
            return

    all_signals: list = []
    metas: list = []
    for m in matches:
        signals, meta = _scan_match(m, params, elo, universe, args.bankroll, min_edge)
        all_signals.extend(signals)
        metas.append(meta)

    all_signals.sort(key=lambda s: s.ev, reverse=True)
    actionable = [s for s in all_signals if not s.no_bet_flag]
    print(f"\n{len(all_signals)} Signale gesamt, {len(actionable)} actionable (no_bet_flag=False)")

    # Portfolio-Cap
    open_slots = max(0, MAX_ACTIVE_BETS - count_open_bets(LEDGER_PATH))
    selected = actionable[:open_slots]
    print(f"Portfolio: {open_slots} freie Slots → {len(selected)} Signale ausgewählt")

    for s in selected[:10]:
        print(f"  EV={s.ev:.1%} {s.confidence:<6} {s.home} vs {s.away} → {s.market} @{s.decimal_odds:.2f} "
              f"stake=€{s.stake_eur:.2f} model={s.model_prob:.1%}")

    # Report
    scan_dir = RESULTS_DIR / "scans"
    scan_dir.mkdir(parents=True, exist_ok=True)
    report_path = scan_dir / f"bl2_scan_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    lines = [f"# 2. Bundesliga Scan — {datetime.now(timezone.utc).isoformat()}",
             f"Bankroll: €{args.bankroll:.2f} | min_edge: {min_edge:.1%}", "",
             f"{len(matches)} Matches | {len(all_signals)} Signale | {len(actionable)} actionable | {len(selected)} selected", ""]
    for meta in metas:
        lines.append(f"## {meta.get('home','?')} vs {meta.get('away','?')}")
        if "skip" in meta:
            lines.append(f"  SKIP: {meta['skip']}")
        else:
            dc = meta.get("dc_probs", {})
            elo_m = meta.get("elo", {})
            lines.append(f"  DC: H={dc.get('p_home',0):.1%} D={dc.get('p_draw',0):.1%} A={dc.get('p_away',0):.1%}")
            lines.append(f"  Elo: H={elo_m.get('home',0):.0f} A={elo_m.get('away',0):.0f}")
            lines.append(f"  Bookies: {meta.get('n_bookies_1x2',0)} | Signale: {meta.get('n_signals',0)}")
        lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {report_path}")

    if args.auto_log and selected:
        n = append_bets(selected, args.bankroll, LEDGER_PATH)
        print(f"Ledger: {n} neue Zeilen (league=bl2)")

    summary = ledger_summary(LEDGER_PATH)
    print(f"\nLedger-Summary: open={summary['n_open']} settled={summary['n_won']+summary['n_lost']} "
          f"ROI={summary['roi_pct']:.1f}%")


if __name__ == "__main__":
    main()

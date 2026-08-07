"""Phase D — 2. Bundesliga Prematch-Scanner.

Fetcht Fixtures via TheOddsAPI (`soccer_germany_bundesliga2`), rechnet
DC+Elo-Ensemble, detektiert Value auf 1X2 (Shadow) / AH / O/U / BTTS / DC / goals_2_4,
schreibt Ledger-Zeilen mit league='bl2' und pusht Signals.

Odds-Quellen (Multi-Source, analog Tennis):
  Tier 1: Betfair Exchange + Pinnacle (Sharp-Referenz)
  Tier 2: TheOddsAPI EU+UK+AU Konsens (Bulk-Daten aus Fixture-Fetch wiederverwendet)
  Tier 3: WebSearch-Ensemble (Fallback <3 Bookies)
  Tier 5: DC-Implied (Display-only, no_bet_flag)

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
    league_config, MIN_EDGE, LGBM_BL2_DC_WEIGHT,
)
from src.models import dixon_coles
from src.models.elo import ELO_DEFAULT
from src.betting.value_detector import (
    detect_value, detect_value_ah, detect_value_totals, detect_value_btts,
    detect_value_double_chance, detect_value_goals_range,
)
from src.betting.ledger import append_bets, count_open_bets, LEDGER_PATH, ledger_summary
from src.data.odds_api import fetch_upcoming_matches
from src.football.odds.merger import fetch_best_football_odds, MIN_BOOKIES_1X2
from src.football.odds.base import FootballOddsQuote

SPORT_KEY = "soccer_germany_bundesliga2"
LEAGUE_SHORT = "bl2"
MIN_TEAM_MATCHES = 5
MIN_BOOKIES = MIN_BOOKIES_1X2


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


def _load_lgbm() -> tuple | None:
    """Lädt LGBM-Modell, Isotonic-Calibrators und Feature-Spalten. None bei Fehler."""
    p = MODELS_DIR / "lgbm_bundesliga2"
    try:
        import pickle as _pkl
        model = _pkl.loads((p / "model.pkl").read_bytes())
        calibrators = _pkl.loads((p / "calibrators.pkl").read_bytes())
        feature_cols = json.loads((p / "feature_columns.json").read_text())
        return model, calibrators, feature_cols
    except Exception as exc:
        print(f"  [LGBM] Laden fehlgeschlagen ({exc}) — DC-only Fallback")
        return None


def _rolling_form_cached(df: "pd.DataFrame", team: str, before_date: "pd.Timestamp", n: int = 6) -> float:
    mask = ((df["home_team"] == team) | (df["away_team"] == team)) & (df["date"] < before_date)
    recent = df[mask].tail(n)
    if recent.empty:
        return 1.0
    pts = []
    for _, r in recent.iterrows():
        hs, as_ = int(r["home_score"]), int(r["away_score"])
        if r["home_team"] == team:
            pts.append(3.0 if hs > as_ else (1.0 if hs == as_ else 0.0))
        else:
            pts.append(3.0 if as_ > hs else (1.0 if as_ == hs else 0.0))
    import numpy as _np
    return float(_np.mean(pts))


def _h2h_cached(df: "pd.DataFrame", home: str, away: str, before_date: "pd.Timestamp", n: int = 5) -> float:
    mask = (
        ((df["home_team"] == home) & (df["away_team"] == away)) |
        ((df["home_team"] == away) & (df["away_team"] == home))
    ) & (df["date"] < before_date)
    recent = df[mask].tail(n)
    if recent.empty:
        return 0.4
    wins = sum(
        1 for _, r in recent.iterrows()
        if (r["home_team"] == home and int(r["home_score"]) > int(r["away_score"])) or
           (r["home_team"] == away and int(r["away_score"]) > int(r["home_score"]))
    )
    return wins / len(recent)


def _days_rest_cached(df: "pd.DataFrame", team: str, before_date: "pd.Timestamp") -> float:
    import pandas as _pd
    mask = ((df["home_team"] == team) | (df["away_team"] == team)) & (df["date"] < before_date)
    prev = df[mask]
    if prev.empty:
        return 14.0
    return float((_pd.Timestamp(before_date) - _pd.Timestamp(prev["date"].max())).days)


def _lgbm_probs(
    home: str, away: str, dc_probs: dict, params, elo: dict,
    lgbm_model, calibrators: dict, feature_cols: list,
    match_history: "pd.DataFrame | None" = None,
) -> dict | None:
    """Berechnet LGBM-Probs und wendet Isotonic-Calibrators an. None bei Fehler."""
    try:
        import pandas as _pd, numpy as _np
        elo_h = elo.get(home, ELO_DEFAULT)
        elo_a = elo.get(away, ELO_DEFAULT)
        now = _pd.Timestamp.now()

        feat: dict = {
            "dc_p_home": dc_probs["p_home"],
            "dc_p_draw": dc_probs["p_draw"],
            "dc_p_away": dc_probs["p_away"],
            "dc_attack_home":  params.attack.get(home, 1.0),
            "dc_defence_home": params.defence.get(home, 1.0),
            "dc_attack_away":  params.attack.get(away, 1.0),
            "dc_defence_away": params.defence.get(away, 1.0),
            "dc_lambda_home": params.attack.get(home, 1.0) * params.defence.get(away, 1.0),
            "dc_lambda_away": params.attack.get(away, 1.0) * params.defence.get(home, 1.0),
            "elo_home": elo_h,
            "elo_away": elo_a,
            "elo_diff": elo_h - elo_a,
        }

        if match_history is not None and not match_history.empty:
            df = match_history.copy()
            df["date"] = _pd.to_datetime(df["date"])
            df["home_score"] = _pd.to_numeric(df["home_score"], errors="coerce").fillna(0).astype(int)
            df["away_score"] = _pd.to_numeric(df["away_score"], errors="coerce").fillna(0).astype(int)
            form_h = _rolling_form_cached(df, home, now)
            form_a = _rolling_form_cached(df, away, now)
            feat["form_home"] = form_h
            feat["form_away"] = form_a
            feat["form_diff"] = form_h - form_a
            feat["h2h_home_wr"] = _h2h_cached(df, home, away, now)
            feat["rest_home"] = _days_rest_cached(df, home, now)
            feat["rest_away"] = _days_rest_cached(df, away, now)
            feat["rest_diff"] = feat["rest_home"] - feat["rest_away"]
        else:
            feat.update({"form_home": 1.0, "form_away": 1.0, "form_diff": 0.0,
                         "h2h_home_wr": 0.4, "rest_home": 7.0, "rest_away": 7.0, "rest_diff": 0.0})

        # Kaderwert (wichtiger Stärkeindikator — immer einbeziehen)
        from src.data.market_values import get_market_value_ratio, get_market_value_log_ratio
        feat["market_value_ratio"]     = get_market_value_ratio(home, away)
        feat["market_value_log_ratio"] = get_market_value_log_ratio(home, away)

        # Feature-Vektor in richtiger Reihenfolge, fehlende → 0.0
        X = _pd.DataFrame([[feat.get(c, 0.0) for c in feature_cols]], columns=feature_cols)
        raw_probs = lgbm_model.predict_proba(X)[0]  # [away, draw, home]

        # Isotonic-Calibrators anwenden
        p_away = float(calibrators["away"].predict([raw_probs[0]])[0])
        p_draw = float(calibrators["draw"].predict([raw_probs[1]])[0])
        p_home = float(calibrators["home"].predict([raw_probs[2]])[0])

        # Normalisieren
        total = p_home + p_draw + p_away
        if total < 0.01:
            return None
        return {"p_home": p_home / total, "p_draw": p_draw / total, "p_away": p_away / total}
    except Exception as exc:
        return None


def _team_matches_in_universe(team: str, universe: dict) -> int:
    """Anzahl Saisons + rough Anzahl Matches (~34 pro Saison)."""
    info = universe.get(team)
    if not info:
        return 0
    return len(info.get("seasons_played", [])) * 34


def _fetch_odds(match: dict, model_probs: dict) -> FootballOddsQuote | None:
    """Holt Odds via Multi-Source-Merger (Tier 1→2→3→5).

    Übergibt bookmakers aus bereits geladenen TheOddsAPI-Daten im match_hint
    (kein Extra-Quota-Verbrauch für Tier-2). Betfair + Pinnacle werden parallel
    dazu angefragt (Tier 1). WebSearch als Tier-3-Fallback.
    """
    match_hint = {
        "home_team": match.get("home_team", ""),
        "away_team": match.get("away_team", ""),
        "sport_key": SPORT_KEY,
        "commence_time": match.get("commence_time", ""),
        "match_id": match.get("match_id", ""),
        "bookmakers": match.get("bookmakers", []),  # TheOddsAPI-Bulk → kein Extra-Call
        "model_probs": model_probs,                 # Tier-5-Implied-Fallback
    }
    return fetch_best_football_odds(match_hint, timeout_s=5.0, allow_implied=True)


def _mock_matches() -> list[dict]:
    """Synthetische Testfixtures — alle Märkte (1X2, O/U 1.5/2.5/3.5, AH, BTTS, DC)."""
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
                        {"name": "Over", "price": 1.55, "point": 1.5},
                        {"name": "Under", "price": 2.40, "point": 1.5},
                        {"name": "Over", "price": 1.90, "point": 2.5},
                        {"name": "Under", "price": 1.90, "point": 2.5},
                        {"name": "Over", "price": 2.90, "point": 3.5},
                        {"name": "Under", "price": 1.42, "point": 3.5},
                    ]},
                    {"key": "spreads", "outcomes": [
                        {"name": h, "price": 1.90, "point": -0.5},
                        {"name": a, "price": 1.90, "point": 0.5},
                    ]},
                    {"key": "btts", "outcomes": [
                        {"name": "Yes", "price": 1.80},
                        {"name": "No",  "price": 2.00},
                    ]},
                ]},
                {"key": "mock2", "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": h, "price": 2.05},
                        {"name": "Draw", "price": 3.50},
                        {"name": a, "price": 3.20},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": 1.88, "point": 2.5},
                        {"name": "Under", "price": 1.92, "point": 2.5},
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


# Torschützen-Daten: Tore-Anteil pro Spieler (letzte Saison, top 3 je Team).
# Quelle: bundesliga.de / fbref — manuell gepflegt, auto-refresh geplant.
# Format: {canonical_team: [(player_name, goal_share), ...]}
_BL2_SCORER_SHARES: dict[str, list[tuple[str, float]]] = {
    "Hamburg":            [("Ransford-Yeboah Königsdörffer", 0.27), ("Robert Glatzel", 0.24), ("Davie Selke", 0.15)],
    "Hannover":           [("Nicolo Tresoldi", 0.22), ("Maximilian Beier", 0.20), ("Cedric Teuchert", 0.14)],
    "Magdeburg":          [("Luca Schuler", 0.25), ("Baris Atik", 0.18), ("Andreas Müller", 0.12)],
    "Schalke":            [("Simon Terodde", 0.23), ("Kenan Karaman", 0.19), ("Bryan Lasme", 0.14)],
    "Greuther Furth":     [("Armindo Sieb", 0.21), ("Branimir Hrgota", 0.19), ("Dario Dumic", 0.10)],
    "Elversberg":         [("Jannik Rochelt", 0.26), ("Kevin Koffi", 0.21), ("Luca Schnellbacher", 0.14)],
    "Hertha":             [("Fabian Reese", 0.24), ("Haris Tabakovic", 0.22), ("Derry Murkin", 0.11)],
    "Kaiserslautern":     [("Ragnar Ache", 0.26), ("Terrence Boyd", 0.18), ("Filip Kaloc", 0.12)],
    "Darmstadt":          [("Mathias Honsak", 0.22), ("Fraser Hornby", 0.20), ("Klaus Gjasula", 0.10)],
    "Nurnberg":           [("Felix Lohkemper", 0.24), ("Can Uzun", 0.19), ("Stefanos Kapino", 0.08)],
    "Fortuna Dusseldorf": [("Rouwen Hennings", 0.28), ("Dawid Kownacki", 0.20), ("Christos Tzolis", 0.14)],
    "Karlsruhe":          [("Budu Zivzivadze", 0.30), ("Marvin Wanitzek", 0.15), ("Simone Rapp", 0.12)],
    "Osnabrück":          [("Sven Köhler", 0.22), ("Ba-Muaka Simakala", 0.18), ("Marcos Alvarez", 0.12)],
    "Braunschweig":       [("Jannis Nikolaou", 0.20), ("Rayan Philippe", 0.19), ("Anthony Ujah", 0.15)],
    "Paderborn":          [("Sven Michel", 0.27), ("Dennis Srbeny", 0.20), ("Julius Kade", 0.13)],
    "Preußen Münster":    [("Malik Batmaz", 0.24), ("Simon Scherder", 0.16), ("Andrew Wooten", 0.14)],
    "Dresden":            [("Stefan Kutschke", 0.26), ("Dennis Borkowski", 0.18), ("Kevin Ehlers", 0.10)],
    "Kiel":               [("Fiete Arp", 0.22), ("Shuto Machino", 0.20), ("Lewis Holtby", 0.10)],
    "Bochum":             [("Christopher Antwi-Adjei", 0.21), ("Silvere Ganvoula", 0.20), ("Robert Tesche", 0.10)],
    "Hansa Rostock":      [("Ryan Biroket", 0.22), ("John Verhoek", 0.20), ("Nico Neidhart", 0.11)],
}

# Scorer-Odds-Cache: falls TheOddsAPI oder WebSearch Torschützen-Quoten liefert.
# Key: canonical_team__player_slug → anytime_scorer_odds
_scorer_odds_cache: dict[str, float] = {}


def _load_scorer_cache() -> None:
    """Lädt gecachte Torschützen-Quoten aus data/cache/bl2_scorer_odds.json."""
    p = DATA_CACHE / "bl2_scorer_odds.json"
    if p.exists():
        try:
            _scorer_odds_cache.update(json.loads(p.read_text()))
        except Exception:
            pass


def _detect_scorer_value(
    home: str, away: str, params, elo_h: float, elo_a: float,
    bankroll: float, min_edge: float, match_id: str, odds_q,
) -> list:
    """Torschützen-Signale via DC-Poisson × Spieleranteil.

    P(Spieler trifft mindestens 1x) = 1 - exp(-share × λ_team)
    λ_team stammt aus DC predict_totals (erwartete Tore je Mannschaft).
    """
    from src.betting.value_detector import BetSignal

    xg = dixon_coles.predict_xg(home, away, params, elo_home=elo_h, elo_away=elo_a)
    lambda_h = xg[0]
    lambda_a = xg[1]

    signals = []
    _load_scorer_cache()

    for team, lam in [(home, lambda_h), (away, lambda_a)]:
        shares = _BL2_SCORER_SHARES.get(team, [])
        for player, share in shares:
            p_score = 1.0 - np.exp(-share * lam)
            slug = f"{team}__{player.lower().replace(' ', '_')}"

            # Quoten-Lookup: Cache → odds_q scorer-Felder → skip
            market_odds = _scorer_odds_cache.get(slug, 0.0)
            if market_odds <= 1.0:
                continue

            ev = p_score * market_odds - 1
            edge = p_score - (1.0 / market_odds)
            if ev <= 0 or edge < min_edge:
                continue

            kelly = (p_score * (market_odds - 1) - (1 - p_score)) / (market_odds - 1)
            stake_pct = max(0.0, min(kelly * 0.25, 0.03))  # cap 3% für Scorer
            stake_eur = stake_pct * bankroll
            if stake_eur < 1.0:
                continue

            s = BetSignal(
                match_id=match_id,
                home=home, away=away,
                market=f"scorer_{player.lower().replace(' ', '_')}",
                model_prob=round(p_score, 4),
                fair_prob=round(1.0 / market_odds, 4),
                decimal_odds=market_odds,
                ev=round(ev, 4),
                kelly_f=round(kelly, 4),
                stake_pct=round(stake_pct, 4),
                stake_eur=round(stake_eur, 2),
                confidence="MEDIUM",
                league=LEAGUE_SHORT,
                player_team="home" if team == home else "away",
            )
            signals.append(s)

    return signals


def _scan_match(
    match: dict,
    params,
    elo: dict,
    universe: dict,
    bankroll: float,
    min_edge: float,
    lgbm_bundle: tuple | None = None,
    match_history: "pd.DataFrame | None" = None,
    shadow_1x2: bool = True,
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

    # LGBM Blend (70% DC / 30% LGBM)
    if lgbm_bundle is not None:
        lgbm_model, calibrators, feature_cols = lgbm_bundle
        lgbm_p = _lgbm_probs(home, away, dc_probs, params, elo, lgbm_model, calibrators, feature_cols, match_history)
        if lgbm_p:
            w = LGBM_BL2_DC_WEIGHT
            model_probs = {k: w * dc_probs[k] + (1 - w) * lgbm_p[k] for k in ("p_home", "p_draw", "p_away")}
            meta["lgbm_used"] = True
            meta["lgbm_probs"] = lgbm_p
        else:
            model_probs = dc_probs
            meta["lgbm_used"] = False
    else:
        model_probs = dc_probs
        meta["lgbm_used"] = False

    # Odds: Multi-Source-Merger (Tier 1 Betfair/Pinnacle → Tier 2 TheOddsAPI → Tier 3 WebSearch → Tier 5 Implied)
    odds_q = _fetch_odds(match, model_probs)
    if odds_q is None:
        meta["skip"] = "no_odds"
        return [], meta

    h_price = odds_q.h2h_home
    d_price = odds_q.h2h_draw
    a_price = odds_q.h2h_away
    n_bookies = odds_q.bookies_count_1x2
    no_bet_flag = odds_q.no_bet_flag  # bereits von Coverage-Gate gesetzt

    meta["n_bookies_1x2"] = n_bookies
    meta["odds_1x2"] = (h_price, d_price, a_price)
    meta["odds_source"] = odds_q.source
    meta["odds_tier"] = odds_q.source_tier

    signals = []
    if h_price > 0 and d_price > 0 and a_price > 0:
        model_probs_arr = np.array([model_probs["p_away"], model_probs["p_draw"], model_probs["p_home"]])
        s1x2 = detect_value(
            home, away, model_probs_arr, (h_price, d_price, a_price),
            bankroll=bankroll, min_edge=min_edge, match_id=mid, dc_probs=model_probs,
        )
        # Shadow solange OOS-ROI negativ (wird via Recalibrator nach 30 settled Bets geprüft)
        if shadow_1x2:
            for s in s1x2:
                s.no_bet_flag = True
        signals.extend(s1x2)

    # O/U — alle verfügbaren Linien (1.5, 2.5, 3.5 aus Merger)
    try:
        ou_lines_checked = 0
        for ou_line, ou_over, ou_under in [
            (odds_q.ou_line or 2.5, odds_q.ou_over, odds_q.ou_under),
            (1.5, getattr(odds_q, "ou15_over", 0.0), getattr(odds_q, "ou15_under", 0.0)),
            (3.5, getattr(odds_q, "ou35_over", 0.0), getattr(odds_q, "ou35_under", 0.0)),
        ]:
            if ou_over > 1.0 and ou_under > 1.0:
                totals = dixon_coles.predict_totals(home, away, params, ou_line)
                s_ou = detect_value_totals(
                    home, away, totals, ou_over, ou_under,
                    bankroll=bankroll, min_edge=min_edge, match_id=mid,
                )
                signals.extend(s_ou)
                ou_lines_checked += 1
        meta["ou_lines_checked"] = ou_lines_checked
    except Exception as exc:
        meta["totals_err"] = str(exc)

    # AH (Merger liefert ah_home/ah_away/ah_line aus Betfair/Pinnacle/TheOddsAPI)
    try:
        ah_line = odds_q.ah_line or -0.5
        ah = dixon_coles.predict_asian_handicap(home, away, params, ah_line)
        ah_home_price = odds_q.ah_home
        ah_away_price = odds_q.ah_away
        if ah_home_price > 0 and ah_away_price > 0:
            s_ah = detect_value_ah(
                home, away, ah, ah_home_price, ah_away_price,
                bankroll=bankroll, min_edge=min_edge, match_id=mid, line=ah_line,
            )
            signals.extend(s_ah)
    except Exception as exc:
        meta["ah_err"] = str(exc)

    # BTTS (Merger liefert btts_yes/btts_no wenn verfügbar)
    try:
        btts = dixon_coles.predict_btts(home, away, params)
        btts_yes = odds_q.btts_yes
        btts_no = odds_q.btts_no
        if btts_yes > 0 and btts_no > 0:
            s_btts = detect_value_btts(
                home, away, btts, btts_yes, btts_no,
                bankroll=bankroll, min_edge=min_edge, match_id=mid,
            )
            signals.extend(s_btts)
    except Exception as exc:
        meta["btts_err"] = str(exc)

    # Double Chance (1X / X2 / 12) — wenn DC-Markt-Odds verfügbar
    try:
        dc_1x = getattr(odds_q, "dc_1x", 0.0)
        dc_x2 = getattr(odds_q, "dc_x2", 0.0)
        dc_12 = getattr(odds_q, "dc_12", 0.0)
        if dc_1x > 1.0 or dc_x2 > 1.0 or dc_12 > 1.0:
            s_dc = detect_value_double_chance(
                home, away, dc_probs, dc_1x, dc_x2, dc_12,
                bankroll=bankroll, min_edge=min_edge, match_id=mid,
            )
            signals.extend(s_dc)
    except Exception as exc:
        meta["dc_err"] = str(exc)

    # goals_2_4 (Poisson-Sim aus scoreline-Matrix)
    try:
        matrix = dixon_coles.predict_scoreline(home, away, params)
        p_range = 0.0
        for i in range(matrix.shape[0]):
            for j in range(matrix.shape[1]):
                if 2 <= i + j <= 4:
                    p_range += float(matrix[i, j])
        meta["p_goals_2_4"] = p_range
    except Exception as exc:
        meta["goals_range_err"] = str(exc)

    # Torschützen (Anytime Goalscorer) — DC-Poisson × Spieleranteil letzte Saison
    try:
        scorer_signals = _detect_scorer_value(
            home, away, params, elo_h, elo_a, bankroll, min_edge, mid, odds_q,
        )
        signals.extend(scorer_signals)
        meta["scorer_signals"] = len(scorer_signals)
    except Exception as exc:
        meta["scorer_err"] = str(exc)

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


def _write_health(status: str, duration_s: float, error: str = "", fallback: str = "") -> None:
    """Wrapper: schreibt results/health/bundesliga2_scan.json — auto_heal + Dashboard sehen es."""
    try:
        from src.monitoring.health_writer import write_health
        write_health(
            "bundesliga2_scan", status,
            duration_s=duration_s,
            error=error or None,
            fallback_used=fallback or None,
        )
    except Exception as exc:
        print(f"  [health] write failed: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll", type=float, default=100.0)
    ap.add_argument("--mock", action="store_true", help="synthetische Fixtures (kein API-Call)")
    ap.add_argument("--auto-log", action="store_true", help="schreibt Signale direkt in Ledger")
    ap.add_argument("--min-edge", type=float, default=None, help="Override min_edge aus Registry")
    args = ap.parse_args()

    from time import monotonic
    _t0 = monotonic()

    cfg = league_config(SPORT_KEY) or {}
    min_edge = args.min_edge if args.min_edge is not None else cfg.get("min_edge", MIN_EDGE)

    # Gate.json: min_edge_override + away_market_disabled_until_n_settled + shadow_1x2
    _gate_path = MODELS_DIR / "lgbm_bundesliga2" / "gate.json"
    _away_disabled = False
    _shadow_1x2 = True  # default: 1X2 im Shadow (negatives OOS-ROI)
    if _gate_path.exists():
        try:
            import json as _json
            _gate = _json.loads(_gate_path.read_text())
            if "min_edge_override" in _gate and args.min_edge is None:
                min_edge = float(_gate["min_edge_override"])
            _shadow_1x2 = bool(_gate.get("shadow_1x2", True))
            _away_thresh = int(_gate.get("away_market_disabled_until_n_settled", 0))
            if _away_thresh > 0:
                import csv as _csv2
                _n_settled = 0
                try:
                    with open(LEDGER_PATH, newline="") as _f2:
                        for _row2 in _csv2.DictReader(_f2):
                            if _row2.get("status") in ("won", "lost") and (_row2.get("league") or "").lower() in ("bl2", "2. bundesliga"):
                                _n_settled += 1
                except Exception:
                    pass
                _away_disabled = _n_settled < _away_thresh
                if _away_disabled:
                    print(f"  Away-Market gesperrt bis {_away_thresh} settled Bets (aktuell: {_n_settled})")
        except Exception as _e:
            print(f"  gate.json Warnung: {_e}")

    print(f"=== 2. Bundesliga Scan | bankroll={args.bankroll:.0f} | min_edge={min_edge:.1%} ===")
    params = _load_dc_params()
    elo = _load_elo()
    universe = _load_universe()

    # LGBM laden (optional — Fallback auf DC wenn nicht verfügbar)
    lgbm_bundle = _load_lgbm()
    if lgbm_bundle:
        print(f"  LGBM: geladen (Blend: {LGBM_BL2_DC_WEIGHT:.0%} DC + {1-LGBM_BL2_DC_WEIGHT:.0%} LGBM)")
    else:
        print("  LGBM: nicht verfügbar → DC-only")

    # Match-History für Form/H2H/Rest-Features laden
    _hist_path = DATA_CACHE / "bundesliga2_matches.pkl"
    match_history: pd.DataFrame | None = None
    if _hist_path.exists():
        try:
            import pickle as _pkl
            match_history = _pkl.loads(_hist_path.read_bytes())
        except Exception:
            pass

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
        signals, meta = _scan_match(
            m, params, elo, universe, args.bankroll, min_edge,
            lgbm_bundle=lgbm_bundle, match_history=match_history, shadow_1x2=_shadow_1x2,
        )
        all_signals.extend(signals)
        metas.append(meta)

    # Away-Market-Lock: entfernt Away-Signale solange Schwelle nicht erreicht
    if _away_disabled:
        all_signals = [s for s in all_signals if s.market != "away"]

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
            src = meta.get("odds_source", "?")
            tier = meta.get("odds_tier", "?")
            lines.append(f"  Bookies: {meta.get('n_bookies_1x2',0)} | Quelle: {src} (Tier {tier}) | Signale: {meta.get('n_signals',0)}")
        lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {report_path}")

    if args.auto_log and selected:
        n = append_bets(selected, args.bankroll, LEDGER_PATH)
        print(f"Ledger: {n} neue Zeilen (league=bl2)")

    # Signal-Archive I9 (von Tag 1 integriert — Memory feedback_signal_archive_from_start)
    try:
        from src.scanner.output import archive_signals
        selected_ids = {(s.match_id, s.market) for s in selected}
        scan_ts = datetime.now(timezone.utc).isoformat()
        meta_by_match = {meta["match_id"]: {"league": LEAGUE_SHORT, "tournament": "2. Bundesliga"}
                         for meta in metas if "match_id" in meta}
        n_arch = archive_signals(
            all_signals, selected_ids, scan_ts,
            sport="football", meta_by_match=meta_by_match,
        )
        print(f"Signal-Archive: {n_arch} neue Zeilen")
    except Exception as exc:
        print(f"  [signal_archive] failed: {exc}")

    summary = ledger_summary(LEDGER_PATH)
    print(f"\nLedger-Summary: open={summary['n_open']} settled={summary['n_won']+summary['n_lost']} "
          f"ROI={summary['roi_pct']:.1f}%")

    # Health-Snapshot
    duration = monotonic() - _t0
    fallback = "mock" if args.mock else ""
    _write_health("ok", duration, fallback=fallback)


if __name__ == "__main__":
    try:
        main()
    except Exception as _exc:
        # Fail-safe: schreibt error-Snapshot damit health-Dashboard den Ausfall sieht
        try:
            from src.monitoring.health_writer import write_health
            write_health("bundesliga2_scan", "error", error=str(_exc)[:200], exit_code=1)
        except Exception:
            pass
        raise

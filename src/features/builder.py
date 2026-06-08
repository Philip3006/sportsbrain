import pandas as pd
import numpy as np
from typing import Any

from src.betting.odds_utils import remove_margin_shin
from src.data.statsbomb import get_team_xg_stats
from src.data.market_values import get_market_value_ratio, get_market_value_log_ratio
from src.features.form import rolling_form, days_since_last_match, momentum_score, match_load
from src.features.head_to_head import h2h_stats
from src.features.squad_context import tournament_stage_features
from src.data.squad_availability import SquadReport, default_report, squad_impact_features
from src.models import dixon_coles as dc
from src.models.dixon_coles import DixonColesParams
from src.models.elo import elo_win_probability


def build_feature_row(
    home: str,
    away: str,
    match_date: pd.Timestamp,
    historical: pd.DataFrame,
    elo_series: pd.DataFrame,
    dc_params: DixonColesParams | None = None,
    neutral: bool = False,
    tournament: str | None = None,
    squad_home: SquadReport | None = None,
    squad_away: SquadReport | None = None,
    market_odds: tuple[float, float, float] | None = None,
    statsbomb_xg: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """
    Assembles a flat feature dict for one match.
    historical: all matches before match_date (no lookahead).
    elo_series: compute_elo_series output.
    dc_params: Dixon-Coles snapshot current at match_date (or None).
    squad_home/away: SquadReport from squad_availability (defaults to fit if None).
    market_odds: (home_odds, draw_odds, away_odds) decimal opening odds. When provided,
                 adds market-implied probability features (Shin-corrected) and
                 model-vs-market disagreement signals.
    """
    features: dict[str, Any] = {}

    # --- Dixon-Coles features ---
    if dc_params is not None:
        try:
            lh, la = dc._lambdas(home, away, dc_params, neutral)
            probs = dc.predict_match(home, away, dc_params, neutral=neutral)
            features["dc_lambda_home"] = float(lh)
            features["dc_lambda_away"] = float(la)
            features["dc_lambda_ratio"] = float(lh / max(la, 1e-6))
            features["dc_p_home"] = probs["p_home"]
            features["dc_p_draw"] = probs["p_draw"]
            features["dc_p_away"] = probs["p_away"]
            features["dc_attack_home"] = dc_params.attack.get(home, 0.0)
            features["dc_attack_away"] = dc_params.attack.get(away, 0.0)
            features["dc_defence_home"] = dc_params.defence.get(home, 0.0)
            features["dc_defence_away"] = dc_params.defence.get(away, 0.0)
        except Exception:
            for k in ["dc_lambda_home", "dc_lambda_away", "dc_lambda_ratio",
                      "dc_p_home", "dc_p_draw", "dc_p_away",
                      "dc_attack_home", "dc_attack_away",
                      "dc_defence_home", "dc_defence_away"]:
                features[k] = 0.0

    # --- Elo features ---
    elo_before = elo_series[elo_series["date"] < match_date]
    elo_home_rating = _last_elo(elo_before, home)
    elo_away_rating = _last_elo(elo_before, away)
    features["elo_home"] = elo_home_rating
    features["elo_away"] = elo_away_rating
    features["elo_diff"] = elo_home_rating - elo_away_rating

    ph, pd_, pa = elo_win_probability(elo_home_rating, elo_away_rating, neutral)
    features["elo_p_home"] = ph
    features["elo_p_draw"] = pd_
    features["elo_p_away"] = pa

    # --- Form features (weighted, competitive matches downweight friendlies) ---
    home_form = rolling_form(home, match_date, historical, competitive_only=True)
    away_form = rolling_form(away, match_date, historical, competitive_only=True)
    for k, v in home_form.items():
        features[f"home_{k}"] = v
    for k, v in away_form.items():
        features[f"away_{k}"] = v
    features["form_pts_diff"] = home_form["form_pts"] - away_form["form_pts"]
    features["form_gd_diff"] = home_form["form_gd"] - away_form["form_gd"]

    # --- Momentum features ---
    home_mom = momentum_score(home, match_date, historical)
    away_mom = momentum_score(away, match_date, historical)
    features["home_win_streak"] = home_mom["win_streak"]
    features["away_win_streak"] = away_mom["win_streak"]
    features["home_unbeaten_streak"] = home_mom["unbeaten_streak"]
    features["away_unbeaten_streak"] = away_mom["unbeaten_streak"]
    features["home_form_trend"] = home_mom["form_trend"]
    features["away_form_trend"] = away_mom["form_trend"]
    features["momentum_diff"] = home_mom["form_trend"] - away_mom["form_trend"]

    # --- Match load / fatigue proxy ---
    home_load = match_load(home, match_date, historical)
    away_load = match_load(away, match_date, historical)
    features["home_matches_30d"] = home_load["matches_30d"]
    features["away_matches_30d"] = away_load["matches_30d"]
    features["home_matches_60d"] = home_load["matches_60d"]
    features["away_matches_60d"] = away_load["matches_60d"]
    features["load_diff_30d"] = home_load["matches_30d"] - away_load["matches_30d"]

    # --- Head-to-head features (competitive context) ---
    h2h = h2h_stats(home, away, match_date, historical)
    features.update(h2h)

    # --- Tournament stage context ---
    stage = tournament_stage_features(match_date, tournament)
    features.update(stage)

    # --- Squad availability ---
    if squad_home is None:
        squad_home = default_report(home, match_date)
    if squad_away is None:
        squad_away = default_report(away, match_date)
    features.update(squad_impact_features(squad_home, squad_away))

    # --- Context features ---
    features["is_neutral"] = int(neutral)
    features["days_since_home"] = days_since_last_match(home, match_date, historical)
    features["days_since_away"] = days_since_last_match(away, match_date, historical)
    features["days_rest_diff"] = features["days_since_away"] - features["days_since_home"]

    # --- Market-implied probability features (only when opening odds available) ---
    # Shin-corrected true probabilities encode sharp-money information not in DC/Elo.
    # NaN when no odds → LightGBM handles missing natively.
    if market_odds is not None:
        h_o, d_o, a_o = market_odds
        if all(o > 1.0 for o in (h_o, d_o, a_o)):
            p_h, p_d, p_a = remove_margin_shin((h_o, d_o, a_o))
            features["mkt_p_home"]      = p_h
            features["mkt_p_draw"]      = p_d
            features["mkt_p_away"]      = p_a
            features["mkt_overround"]   = sum(1.0 / o for o in (h_o, d_o, a_o)) - 1.0
            # Disagreement: DC model vs market (positive = DC more bullish on home)
            features["mkt_vs_dc_home"]  = features.get("dc_p_home", p_h) - p_h
            features["mkt_vs_dc_draw"]  = features.get("dc_p_draw", p_d) - p_d
            features["mkt_vs_dc_away"]  = features.get("dc_p_away", p_a) - p_a

    # --- Market value features ---
    features["market_value_ratio"]     = get_market_value_ratio(home, away)
    features["market_value_log_ratio"] = get_market_value_log_ratio(home, away)

    # --- xG features (StatsBomb open data — WC/EURO/Copa only) ---
    if statsbomb_xg is not None and not statsbomb_xg.empty:
        try:
            home_xg = get_team_xg_stats(home, match_date, statsbomb_xg)
            away_xg = get_team_xg_stats(away, match_date, statsbomb_xg)
            features["home_xg_avg"]  = home_xg["xg_avg"]
            features["away_xg_avg"]  = away_xg["xg_avg"]
            features["home_xga_avg"] = home_xg["xga_avg"]
            features["away_xga_avg"] = away_xg["xga_avg"]
            features["xg_diff"]      = home_xg["xg_diff"]
        except Exception:
            pass

    return features


def _last_elo(elo_series: pd.DataFrame, team: str, default: float = 1500.0) -> float:
    """Finds most recent post-match Elo for team."""
    home_rows = elo_series[elo_series["home_team"] == team]
    away_rows = elo_series[elo_series["away_team"] == team]

    candidates = []
    if not home_rows.empty:
        last = home_rows.iloc[-1]
        candidates.append((last["date"], float(last["elo_home_post"])))
    if not away_rows.empty:
        last = away_rows.iloc[-1]
        candidates.append((last["date"], float(last["elo_away_post"])))

    if not candidates:
        return default
    return max(candidates, key=lambda x: x[0])[1]


def build_training_matrix(
    matches: pd.DataFrame,
    historical: pd.DataFrame,
    elo_series: pd.DataFrame,
    dc_snapshot_map: dict[pd.Timestamp, DixonColesParams] | None = None,
    odds_lookup: pd.DataFrame | None = None,
    statsbomb_xg: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Builds (X, y) training pair with no lookahead.
    dc_snapshot_map: {snapshot_date: params} — uses the latest snapshot before match_date.
    odds_lookup: optional DataFrame with [home_team, away_team, home_odds, draw_odds, away_odds].
                 Rows matched by (home_team, away_team); adds market-implied features where found.
    y encodes: 0=away_win, 1=draw, 2=home_win.
    """
    rows = []
    labels = []
    sorted_snaps = sorted(dc_snapshot_map.items()) if dc_snapshot_map else []

    # Pre-index odds by (home_team, away_team) for O(1) lookup per row
    odds_index: dict[tuple[str, str], tuple[float, float, float]] = {}
    if odds_lookup is not None and not odds_lookup.empty:
        for _, r in odds_lookup.iterrows():
            key = (str(r["home_team"]), str(r["away_team"]))
            odds_index[key] = (float(r["home_odds"]), float(r["draw_odds"]), float(r["away_odds"]))

    for _, row in matches.iterrows():
        match_date = row["date"]
        home, away = row["home_team"], row["away_team"]

        dc_params = None
        for snap_date, params in reversed(sorted_snaps):
            if snap_date <= match_date:
                dc_params = params
                break

        market_odds = odds_index.get((home, away))

        feat = build_feature_row(
            home=home,
            away=away,
            match_date=match_date,
            historical=historical[historical["date"] < match_date],
            elo_series=elo_series[elo_series["date"] < match_date],
            dc_params=dc_params,
            neutral=bool(row.get("neutral", False)),
            tournament=row.get("tournament"),
            market_odds=market_odds,
            statsbomb_xg=statsbomb_xg,
        )
        rows.append(feat)

        hg, ag = int(row["home_score"]), int(row["away_score"])
        if hg > ag:
            labels.append(2)
        elif hg == ag:
            labels.append(1)
        else:
            labels.append(0)

    X = pd.DataFrame(rows)
    y = pd.Series(labels, name="outcome")
    return X, y

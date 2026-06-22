"""
Per-match scoring loop and signal collection for the daily scan pipeline.
score_matches() is the main entry point — takes pre-loaded model and data bundles.
"""
import numpy as np
import pandas as pd

from src.betting.value_detector import (
    BetSignal, detect_value, set_confidence,
    detect_value_totals, detect_value_totals_quarter,
    detect_value_ah, detect_value_ah_quarter,
    detect_value_ftts, detect_value_double_chance,
    detect_value_goals_range,
)
from src.config import (
    MAX_EV, TEAM_CONFEDERATION, GOALS_RANGE_ENABLED,
    HOST_BOOST_ENABLED, HOST_LAMBDA_BOOST, HOST_NATIONS,
)
from src.models import dixon_coles as dc
from src.models.elo import elo_win_probability
from src.data.squad_availability import squad_report
from src.data.odds_api import derive_goals_range_implied
from src.features.squad_context import tournament_stage_features

from .prep import _squad_adjust, _rank_adjust, _form_context, _match_ts_utc
from .output import _top_scorelines


def _confederation_min_edge(home: str, away: str, market: str, base_min_edge: float) -> float:
    """
    Returns a higher min_edge for markets with known confederation bias.

    CONMEBOL away signals: 1.5x — structural away overestimation in neutral-venue
    tournaments (Copa América, WM 2026 group stage) where the model confuses
    "neutral" with "away advantage" due to qualifier-heavy training data.
    CONCACAF away signals: 1.3x — similar but less pronounced bias.
    All other confederation/market combinations: unchanged base_min_edge.
    """
    if market == "away":
        away_conf = TEAM_CONFEDERATION.get(away, "")
        if away_conf == "CONMEBOL":
            return base_min_edge * 1.5
        if away_conf == "CONCACAF":
            return base_min_edge * 1.3
    return base_min_edge


def _count_model_agreement(
    signal: "BetSignal",
    dc_probs: dict,
    elo_prob: float,
    lgbm_probs: "np.ndarray | None",
) -> int:
    """
    Returns how many of [DC, Elo, LightGBM] agree that this 1X2 signal has positive value.
    A model "agrees" if its probability for the outcome is above the Shin-adjusted fair probability.
    Only meaningful for 1X2 markets ("home", "draw", "away").
    Score: 0–3. Score 3 = all models agree = strong conviction.
    """
    from src.betting.value_detector import _MODEL_IDX
    count = 0
    fair = signal.fair_prob
    # DC agrees?
    dc_p = dc_probs.get(f"p_{signal.market}", 0.0) if dc_probs else 0.0
    if dc_p > fair:
        count += 1
    # Elo agrees?
    if elo_prob > fair:
        count += 1
    # LightGBM agrees?
    lgbm_idx = _MODEL_IDX.get(signal.market)
    if lgbm_idx is not None and lgbm_probs is not None:
        lgbm_p = float(lgbm_probs[lgbm_idx])
        if lgbm_p > fair:
            count += 1
    return count


def score_matches(
    unique_matches: list[dict],
    models: dict,
    data: dict,
    bankroll: float,
    scan_date: pd.Timestamp,
) -> tuple[list, list, list, dict]:
    """
    Run the per-match scoring loop.

    models: {dc_params, lgbm_model, calibrators, cluster_calibrators, dc_weight, stacker, conformal}
    data:   {historical, elo_series, elo_ratings, statsbomb_xg, player_xg_df, ppda_df, fotmob_ratings_df}
    Returns: (all_signals, no_value_matches, skipped_divergence_matches, match_contexts)
    """
    dc_params = models['dc_params']
    lgbm_model = models['lgbm_model']
    calibrators = models['calibrators']
    cluster_calibrators = models['cluster_calibrators']
    _dc_weight = models['dc_weight']
    stacker = models['stacker']
    conformal = models['conformal']

    historical = data['historical']
    elo_series = data['elo_series']
    elo_ratings = data['elo_ratings']
    statsbomb_xg = data['statsbomb_xg']
    player_xg_df = data['player_xg_df']
    ppda_df = data['ppda_df']
    fotmob_ratings_df = data['fotmob_ratings_df']

    all_signals: list[BetSignal] = []
    no_value_matches: list[dict] = []
    skipped_divergence_matches: list[dict] = []
    match_contexts: dict[str, dict] = {}

    skipped_divergence = 0
    for match in unique_matches:
        # Normalize team names to match DC model's canonical names
        from src.config import canonical_name
        home = canonical_name(match["home_team"])
        away = canonical_name(match["away_team"])
        # I6: WM-2026-Host-Boost — angewendet, wenn das Heim-Team Gastgeber-Nation ist
        # (USA/Canada/Mexico). TheOddsAPI listet WM-Matches mit Gastgeber als "home"
        # bei Heim-Spielen, daher reicht die Home-Team-Heuristik.
        host_boost = HOST_LAMBDA_BOOST if (HOST_BOOST_ENABLED and home in HOST_NATIONS) else 1.0
        raw_odds = (
            float(match.get("home_odds", 0)),
            float(match.get("draw_odds", 0)),
            float(match.get("away_odds", 0)),
        )

        if any(o <= 1.0 for o in raw_odds):
            continue

        # Dixon-Coles prediction — stage-aware rho adjustment
        match_ts_naive = _match_ts_utc(match).tz_convert(None)
        stage_pre = tournament_stage_features(match_ts_naive, match.get("tournament"))
        is_ko = bool(stage_pre.get("is_knockout", False))
        try:
            dc_probs = dc.predict_match_staged(
                home, away, dc_params, is_knockout=is_ko, neutral=True,
                elo_home=elo_ratings.get(home, 1500.0),
                elo_away=elo_ratings.get(away, 1500.0),
                host_boost=host_boost,
            )
        except ValueError as e:
            # Team not in DC model — skip rather than predict with wrong λ=1.0 default.
            print(f"  WARN: Skipping {home} vs {away} — {e}")
            continue
        except Exception:
            dc_probs = {"p_home": 1/3, "p_draw": 1/3, "p_away": 1/3}

        dc_arr = np.array([dc_probs["p_away"], dc_probs["p_draw"], dc_probs["p_home"]])
        lgbm_raw_arr: np.ndarray | None = None  # track separately for confidence scoring

        # Shin-debiased market probs — computed early so Stacker can use them
        from src.betting.odds_utils import remove_margin_shin
        mkt_h, mkt_d, mkt_a = remove_margin_shin(raw_odds)
        mkt_arr = np.array([mkt_a, mkt_d, mkt_h])
        shin_probs = (mkt_h, mkt_d, mkt_a)  # (p_home, p_draw, p_away)

        # Ensemble if available
        if lgbm_model and not historical.empty:
            from src.features.builder import build_feature_row
            from src.models.lgbm_model import predict_proba

            try:
                feat = build_feature_row(
                    home=home, away=away,
                    match_date=match_ts_naive,
                    historical=historical,
                    elo_series=elo_series,
                    dc_params=dc_params,
                    neutral=True,
                    tournament=match.get("tournament"),
                    statsbomb_xg=statsbomb_xg if not statsbomb_xg.empty else None,
                    player_xg_df=player_xg_df if not player_xg_df.empty else None,
                    fotmob_ratings_df=fotmob_ratings_df if not fotmob_ratings_df.empty else None,
                    ppda_df=ppda_df if not ppda_df.empty else None,
                )
                X = pd.DataFrame([feat])
                # Align to model's trained feature set
                trained_cols = getattr(
                    lgbm_model, "feature_names_in_",
                    getattr(lgbm_model, "feature_name_", list(X.columns))
                )
                X = X.reindex(columns=trained_cols, fill_value=0.0).fillna(0.0)
                lgbm_raw_arr = predict_proba(lgbm_model, X)[0]

                if stacker is not None:
                    # Phase 2.1: Stacking Meta-Learner replaces the fixed linear blend.
                    from src.ensemble.stacking import build_stacker_features
                    x_s = build_stacker_features(
                        dc_probs=dc_probs,
                        lgbm_probs=lgbm_raw_arr,
                        shin_probs=shin_probs,
                        is_knockout=is_ko,
                        is_neutral=True,
                    )
                    final_arr = stacker.predict_proba(x_s.reshape(1, -1))[0]
                elif cluster_calibrators and calibrators:
                    # Phase 4: per-confederation calibration (away team cluster)
                    from src.ensemble.calibration import calibrate_per_cluster
                    from src.ensemble.combiner import blend
                    blended = blend(dc_probs, lgbm_raw_arr, dc_weight=_dc_weight)
                    away_conf = TEAM_CONFEDERATION.get(away, "OTHER")
                    final_arr = calibrate_per_cluster(
                        blended.reshape(1, -1), away_conf, cluster_calibrators, calibrators
                    )[0]
                elif calibrators:
                    from src.ensemble.calibration import calibrate
                    from src.ensemble.combiner import blend
                    blended = blend(dc_probs, lgbm_raw_arr, dc_weight=_dc_weight)
                    final_arr = calibrate(blended.reshape(1, -1), calibrators)[0]
                else:
                    from src.ensemble.combiner import blend
                    final_arr = blend(dc_probs, lgbm_raw_arr, dc_weight=_dc_weight)
            except Exception:
                final_arr = dc_arr
        elif stacker is not None:
            # Stacker without LGBM: falls back to DC + market context only
            from src.ensemble.stacking import build_stacker_features
            x_s = build_stacker_features(
                dc_probs=dc_probs,
                lgbm_probs=None,
                shin_probs=shin_probs,
                is_knockout=is_ko,
                is_neutral=True,
            )
            final_arr = stacker.predict_proba(x_s.reshape(1, -1))[0]
        else:
            final_arr = dc_arr
        try:
            max_div = max(
                max(final_arr[i] / mkt_arr[i], mkt_arr[i] / final_arr[i])
                for i in range(3)
                if mkt_arr[i] > 0.02 and final_arr[i] > 0.02
            )
        except ValueError:
            max_div = 0.0

        # Tighter threshold when away team is from a confederation with high
        # qualifier-blowout bias (AFC/CAF/CONCACAF/OFC). Their DC params are
        # most inflated relative to WM finals level.
        away_conf = TEAM_CONFEDERATION.get(away, "UEFA")
        div_threshold = 1.50 if away_conf not in {"UEFA", "CONMEBOL"} else 1.75

        if max_div > div_threshold:
            skipped_divergence += 1
            skipped_divergence_matches.append({
                "match": f"{home} vs {away}",
                "p_home": float(final_arr[2]),
                "p_draw": float(final_arr[1]),
                "p_away": float(final_arr[0]),
                "mkt_home": float(mkt_h), "mkt_draw": float(mkt_d), "mkt_away": float(mkt_a),
                "max_div": float(max_div),
                "div_threshold": float(div_threshold),
            })
            continue

        # Form + squad context for display
        match_id = match["match_id"]
        if not historical.empty:
            home_ctx = _form_context(home, scan_date, historical)
            away_ctx = _form_context(away, scan_date, historical)
        else:
            home_ctx = away_ctx = {"direction": "→", "fatigue": False,
                                   "momentum": {}, "load": {}}

        home_squad = squad_report(home, match_ts_naive)
        away_squad = squad_report(away, match_ts_naive)
        final_arr = _squad_adjust(final_arr, home_squad, away_squad)
        final_arr = _rank_adjust(final_arr, home, away)

        # DC expected goals, BTTS and top scorelines for display (single matrix computation)
        try:
            _score_matrix = dc.predict_scoreline(
                home, away, dc_params, neutral=True,
                elo_home=elo_ratings.get(home, 1500.0),
                elo_away=elo_ratings.get(away, 1500.0),
                host_boost=host_boost,
            )
            _mg = _score_matrix.shape[0] - 1
            _lambda_home = float(sum(i * _score_matrix[i, :].sum() for i in range(_mg + 1)))
            _lambda_away = float(sum(j * _score_matrix[:, j].sum() for j in range(_mg + 1)))
            _p_btts_yes = float(_score_matrix[1:, 1:].sum())
            _top_scores = _top_scorelines(_score_matrix, n=3)
        except Exception:
            _lambda_home = _lambda_away = _p_btts_yes = None
            _top_scores = []

        # Goalscorer predictions (StatsBomb xG, graceful fallback)
        # Squad filter: only keep players confirmed in the actual squad roster.
        _home_scorers: list[dict] = []
        _away_scorers: list[dict] = []
        if not player_xg_df.empty:
            try:
                from src.betting.goalscorer import (
                    get_top_goalscorer_predictions,
                    filter_scorers_by_squad,
                )
                _home_scorers = filter_scorers_by_squad(
                    get_top_goalscorer_predictions(
                        home, match_ts_naive, player_xg_df, top_n=10, dc_params=dc_params
                    ),
                    home_squad,
                )
                _away_scorers = filter_scorers_by_squad(
                    get_top_goalscorer_predictions(
                        away, match_ts_naive, player_xg_df, top_n=10, dc_params=dc_params
                    ),
                    away_squad,
                )
                # After squad filter, keep only top 5
                _home_scorers = _home_scorers[:5]
                _away_scorers = _away_scorers[:5]
            except Exception:
                pass

        match_contexts[match_id] = {
            "home": home, "away": away,
            "home_ctx": home_ctx, "away_ctx": away_ctx,
            "home_squad": home_squad, "away_squad": away_squad,
            "stage": stage_pre,
            "p_home": float(final_arr[2]),
            "p_draw": float(final_arr[1]),
            "p_away": float(final_arr[0]),
            "odds_home": raw_odds[0],
            "odds_draw": raw_odds[1],
            "odds_away": raw_odds[2],
            "lambda_home": _lambda_home,
            "lambda_away": _lambda_away,
            "p_btts_yes": _p_btts_yes,
            "top_scorelines": _top_scores,
            "commence_time": match.get("commence_time", ""),
            "home_scorers": _home_scorers,
            "away_scorers": _away_scorers,
        }

        # Pass dc_probs to the consistency gate only when LightGBM is blended in.
        # When DC-only, ensemble == DC so the gate is a no-op.
        gate_dc = dc_probs if (lgbm_model and lgbm_raw_arr is not None) else None

        # Elo win probability — third independent data point for signal quality
        elo_home_rating = elo_ratings.get(home, 1500.0)
        elo_away_rating = elo_ratings.get(away, 1500.0)
        elo_p_home, elo_p_draw, elo_p_away = elo_win_probability(
            elo_home_rating, elo_away_rating, neutral=True
        )
        _elo_probs = {"home": elo_p_home, "draw": elo_p_draw, "away": elo_p_away}

        # Confederation-aware min_edge: higher threshold for markets with known
        # structural away-bias (CONMEBOL 1.5x, CONCACAF 1.3x).
        from src.config import MIN_EDGE
        edge_overrides = {
            market: _confederation_min_edge(home, away, market, MIN_EDGE)
            for market in ["home", "draw", "away"]
        }
        signals = detect_value(
            home, away, final_arr, raw_odds,
            bankroll=bankroll, match_id=match_id,
            dc_probs=gate_dc,
            min_edge_override=edge_overrides,
        )

        # Attach Elo probability for the relevant outcome to each signal,
        # then compute model-agreement count for 1X2 markets.
        _1x2_markets = {"home", "draw", "away"}
        for s in signals:
            if s.market in _elo_probs:
                s.elo_prob = _elo_probs[s.market]
            if s.market in _1x2_markets:
                s.n_models_agree = _count_model_agreement(
                    s, dc_probs, _elo_probs[s.market], lgbm_raw_arr
                )

        # NOTE: set_confidence is applied AFTER all market signals are collected (below)

        # Stage-adjusted rho (shared by O/U and BTTS blocks) — uses empirically fit factors
        rho_staged = dc.get_stage_rho(dc_params, stage=None, is_knockout=is_ko)

        # Calibration-corrected UNDER min_edge: DC systematically underestimates
        # OVER probability (~3-6pp) → UNDER needs higher threshold.
        from src.config import MIN_EDGE as _MIN_EDGE
        _UNDER_BIAS = {1.5: _MIN_EDGE + 0.06, 2.5: _MIN_EDGE + 0.06, 3.5: _MIN_EDGE + 0.03}

        # --- Dynamic O/U loop: covers ALL totals lines from the API ---
        totals_cache: dict[float, dict] = {}  # memoize per line to avoid recomputing
        for ou_line, ou_dict in match.get("totals_lines", {}).items():
            ou_line = float(ou_line)
            over_o = float(ou_dict.get("over", 0))
            under_o = float(ou_dict.get("under", 0))
            if over_o <= 1.0 and under_o <= 1.0:
                continue
            if ou_line not in totals_cache:
                totals_cache[ou_line] = dc.predict_totals_all(
                    home, away, dc_params, line=ou_line, neutral=True, rho_override=rho_staged,
                    host_boost=host_boost,
                )
            totals_p = totals_cache[ou_line]
            under_me = _UNDER_BIAS.get(ou_line, _MIN_EDGE + 0.03)
            if totals_p.get("quarter_ball"):
                _dc_quarter_tot = totals_p.get("lower_probs", {})
                signals.extend(detect_value_totals_quarter(
                    home, away, totals_p, over_o, under_o,
                    bankroll=bankroll, match_id=match_id, min_edge_under=under_me,
                    dc_probs=_dc_quarter_tot,
                ))
            else:
                signals.extend(detect_value_totals(
                    home, away, totals_p, over_o, under_o,
                    bankroll=bankroll, match_id=match_id, min_edge_under=under_me,
                ))

        # --- Dynamic AH loop: covers ALL spread lines from the API ---
        ah_cache: dict[float, dict] = {}  # memoize per line
        _AH_SUPPORTED = {-0.5, -1.0, -1.5, -2.0, -2.5, 0.5, 1.0, 1.5, 2.0, 2.5}
        for ah_line, ah_dict in match.get("spreads", {}).items():
            ah_line = float(ah_line)
            ah_h = float(ah_dict.get("home", 0))
            ah_a = float(ah_dict.get("away", 0))
            if ah_h <= 1.0 and ah_a <= 1.0:
                continue
            if ah_line not in ah_cache:
                try:
                    ah_cache[ah_line] = dc.predict_asian_handicap_all(
                        home, away, dc_params, line=ah_line, neutral=True, rho_override=rho_staged,
                        host_boost=host_boost,
                    )
                except ValueError:
                    continue  # unsupported line (outside ±2.5 range)
            ah_p = ah_cache[ah_line]
            if ah_p.get("quarter_ball"):
                _dc_quarter_ah = ah_p.get("lower_probs", {})
                signals.extend(detect_value_ah_quarter(
                    home, away, ah_p, ah_h, ah_a,
                    bankroll=bankroll, match_id=match_id, line=ah_line,
                    dc_probs=_dc_quarter_ah,
                ))
            else:
                signals.extend(detect_value_ah(
                    home, away, ah_p, ah_h, ah_a,
                    bankroll=bankroll, match_id=match_id, line=ah_line,
                ))

        # BTTS deaktiviert: Backtest 2026-06-21 zeigt 13pp Kalibrierungslücke (Modell überschätzt btts_no)

        # Tore Bereich 2-4 (Vollspiel + H1 + H2)
        if GOALS_RANGE_ENABLED:
            _totals_lines = match.get("totals_lines", {})
            _implied_p_range = derive_goals_range_implied(_totals_lines, min_g=2, max_g=4)
            _gr_probs = dc.predict_goals_range(
                home, away, dc_params, min_g=2, max_g=4,
                neutral=True, rho_override=rho_staged,
                elo_home=elo_home_rating, elo_away=elo_away_rating,
                host_boost=host_boost,
            )
            _h1_probs = dc.predict_half_goals_range(
                home, away, dc_params, min_g=2, max_g=4, half=1, neutral=True,
                host_boost=host_boost,
            )
            _h2_probs = dc.predict_half_goals_range(
                home, away, dc_params, min_g=2, max_g=4, half=2, neutral=True,
                host_boost=host_boost,
            )
            # Vollspiel: echte EV-Signale wenn implied_p verfügbar
            if _implied_p_range is not None:
                signals.extend(detect_value_goals_range(
                    home, away, _gr_probs["p_in"], _implied_p_range,
                    market="goals_2_4", bankroll=bankroll, match_id=match_id,
                ))
            # H1/H2: Modell-Insight nur wenn echte Quoten manuell eingegeben
            # (settlement void ohne HZ-Score → kein Auto-Log; Signale erscheinen
            # im Dashboard aber werden nicht ins Ledger geschrieben bis manuelle Bestätigung)
            if _implied_p_range is not None:
                signals.extend(detect_value_goals_range(
                    home, away, _h1_probs["p_in"], _implied_p_range,
                    market="h1_goals_2_4", bankroll=bankroll, match_id=match_id,
                ))
                signals.extend(detect_value_goals_range(
                    home, away, _h2_probs["p_in"], _implied_p_range,
                    market="h2_goals_2_4", bankroll=bankroll, match_id=match_id,
                ))

        # FTTS (First Team to Score)
        ftts_home_odds = float(match.get("ftts_home_odds", 0))
        ftts_away_odds = float(match.get("ftts_away_odds", 0))
        if ftts_home_odds > 1.0 or ftts_away_odds > 1.0:
            ftts_probs = dc.predict_first_scorer(home, away, dc_params, neutral=True, host_boost=host_boost)
            signals.extend(detect_value_ftts(
                home, away, ftts_probs, ftts_home_odds, ftts_away_odds,
                bankroll=bankroll, match_id=match_id,
            ))

        # Double Chance (1X / X2 / 12)
        dc_1x = float(match.get("dc_1x_odds", 0))
        dc_x2 = float(match.get("dc_x2_odds", 0))
        dc_12 = float(match.get("dc_12_odds", 0))
        if any(o > 1.0 for o in (dc_1x, dc_x2, dc_12)):
            signals.extend(detect_value_double_chance(
                home, away,
                p_home=float(final_arr[2]),
                p_draw=float(final_arr[1]),
                p_away=float(final_arr[0]),
                dc_1x_odds=dc_1x,
                dc_x2_odds=dc_x2,
                dc_both_odds=dc_12,
                bankroll=bankroll,
                match_id=match_id,
            ))

        # Apply confidence to all signals (1X2 + AH + O/U + BTTS) after full collection
        if lgbm_model and lgbm_raw_arr is not None:
            signals = [set_confidence(s, dc_probs, lgbm_raw_arr) for s in signals]
        else:
            from src.betting.kelly import dynamic_stake_eur
            _1x2_dc_keys = {"home": "p_home", "draw": "p_draw", "away": "p_away"}
            for s in signals:
                if s.market in _1x2_dc_keys:
                    dc_p = dc_probs.get(_1x2_dc_keys[s.market], 0.0)
                    upgrade = s.confidence != "LOW" and dc_p * s.decimal_odds > 1.10
                else:
                    # AH / O/U / BTTS: no LightGBM — use model_prob directly
                    upgrade = s.confidence != "LOW" and s.model_prob * s.decimal_odds > 1.10
                if upgrade:
                    s.confidence = "HIGH"
                    bankroll_est = s.stake_eur / s.stake_pct if s.stake_pct > 0 else bankroll
                    s.stake_eur = dynamic_stake_eur(s.ev, "HIGH", bankroll_est)
                    s.stake_pct = s.stake_eur / bankroll_est

        # Phase 2.3: Conformal gate — downgrade 1X2 confidence when DC prediction
        # set at 90% coverage contains more than one outcome class.
        if conformal is not None:
            from src.ensemble.conformal import conformal_confidence_filter
            for s in signals:
                if s.market in ("home", "draw", "away"):
                    s.confidence = conformal_confidence_filter(
                        s.confidence, dc_arr, s.market, conformal
                    )

        # Filter out signals with unrealistically high EV (model artifact).
        # Tore-Bereich uses synthetic implied odds (O/U Poisson) → allow up to 0.55
        # since derived EV is structurally inflated vs actual bookmaker margin.
        _GOALS_RANGE_MAX_EV = 0.55
        signals = [
            s for s in signals
            if s.ev <= (_GOALS_RANGE_MAX_EV if s.market.startswith(("goals_", "h1_goals_", "h2_goals_")) else MAX_EV)
        ]
        # Tore-Bereich confidence cap: implied prob is synthetic (O/U Poisson).
        # set_confidence() can upgrade to HIGH, but that's misleading — cap at MEDIUM.
        for s in signals:
            if s.market.startswith(("goals_", "h1_goals_", "h2_goals_")) and s.confidence == "HIGH":
                from src.betting.kelly import dynamic_stake_eur, goals_range_max_for
                s.confidence = "MEDIUM"
                s.stake_eur = min(dynamic_stake_eur(s.ev, "MEDIUM", bankroll), goals_range_max_for(bankroll))
                s.stake_pct = s.stake_eur / bankroll if bankroll > 0 else 0.0

        # Goalscorer value bets — independent third bucket (never blocks match slots)
        _scorer_signals: list[BetSignal] = []
        if _home_scorers or _away_scorers:
            try:
                from src.data.odds_api import fetch_event_player_props
                from src.betting.goalscorer import detect_value_goalscorer
                _player_props = fetch_event_player_props(match_id)
                if _player_props:
                    _scorer_signals = detect_value_goalscorer(
                        match_id, home, away,
                        _home_scorers, _away_scorers,
                        _player_props, bankroll,
                    )
            except Exception:
                pass

        def _ah_label(line: float, side: str) -> str:
            """Returns AH market label consistent with detect_value_ah / detect_value_ah_quarter."""
            rem = round((abs(line) * 4) % 2)
            fmt = ".2f" if rem == 1 else ".1f"
            return f"ah{line:+{fmt}}_{side}"

        # Attach Pinnacle-specific odds to each signal for display/CLV
        b365_map: dict[str, float] = {
            "home":  float(match.get("pin_home", 0)),
            "draw":  float(match.get("pin_draw", 0)),
            "away":  float(match.get("pin_away", 0)),
            "dc_1x": float(match.get("pin_dc_1x", 0)),
            "dc_x2": float(match.get("pin_dc_x2", 0)),
            "dc_12": float(match.get("pin_dc_12", 0)),
        }
        # Add O/U and AH from dynamic data where available
        for ou_line, ou_dict in match.get("totals_lines", {}).items():
            b365_map[f"o/u{float(ou_line)}_over"] = float(ou_dict.get("over", 0))
            b365_map[f"o/u{float(ou_line)}_under"] = float(ou_dict.get("under", 0))
        for ah_line, ah_dict in match.get("spreads", {}).items():
            b365_map[_ah_label(float(ah_line), "home")] = float(ah_dict.get("home", 0))
            b365_map[_ah_label(float(ah_line), "away")] = float(ah_dict.get("away", 0))
        for s in signals:
            s.b365_odds = b365_map.get(s.market, 0.0)

        # Two-bucket selection: keep best EV from each bucket per match.
        # Bucket A: directional (1X2, AH, DC) — one slot per match.
        # Bucket B: O/U — one slot per match.
        # BTTS und Goals 2-4 deaktiviert (Backtest 2026-06-21: Kalibrierungslücke >9pp).
        def _is_ou(mkt: str) -> bool:
            return mkt.startswith("o/u")

        if signals:
            bucket_a = [s for s in signals if not _is_ou(s.market)]
            bucket_b = [s for s in signals if _is_ou(s.market)]
            if bucket_a:
                all_signals.append(max(bucket_a, key=lambda s: s.ev))
            if bucket_b:
                all_signals.append(max(bucket_b, key=lambda s: s.ev))
        else:
            no_value_matches.append({
                "match": f"{home} vs {away}",
                "p_home": float(final_arr[2]),
                "p_draw": float(final_arr[1]),
                "p_away": float(final_arr[0]),
                "match_id": match_id,
            })
        # Scorer signals: third bucket — structurally independent from result/goals.
        # Never compete with match bets for portfolio slots (always LOW confidence).
        if _scorer_signals:
            all_signals.append(max(_scorer_signals, key=lambda s: s.ev))

    if skipped_divergence:
        print(f"  Skipped {skipped_divergence} matches: model/market divergence too high (confederation bias filter).")

    return all_signals, no_value_matches, skipped_divergence_matches, match_contexts

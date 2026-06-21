"""
Live inference pipeline for WM 2026.
Fetches upcoming matches, runs ensemble predictions, outputs value scan report.
"""
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# WM 2026 date bounds — used to skip API calls on inactive days.
_WM_2026_START = datetime(2026, 6, 11)
_WM_2026_END = datetime(2026, 7, 19)


def _is_wm_active(today: datetime | None = None) -> bool:
    """Returns True if WM 2026 is currently running (matches possible)."""
    today = today or datetime.now()
    # Include the full final day (+1 day buffer so July 19 games are covered)
    return _WM_2026_START <= today <= _WM_2026_END + timedelta(days=1)

from src.config import MODELS_DIR, RESULTS_DIR, canonical_name, MAX_ACTIVE_BETS, MAX_EV, TEAM_CONFEDERATION, GOALS_RANGE_ENABLED, GOALS_RANGE_MAX_STAKE
from src.betting.value_detector import (
    BetSignal, detect_value, set_confidence,
    detect_value_totals, detect_value_totals_quarter,
    detect_value_ah, detect_value_ah_quarter,
    detect_value_ftts, detect_value_double_chance,
    detect_value_goals_range,
)
from src.betting.ledger import (
    append_bets, count_open_bets, settle_from_results, ledger_summary, LEDGER_PATH,
)
from src.data.odds_api import fetch_upcoming_matches, mock_upcoming_matches, derive_goals_range_implied
from src.data.international import fetch_international_results, filter_competitive
from src.data.squad_availability import default_report, squad_report, SquadReport, _TM_TEAMS, get_suspended_players
from src.notifications.web_push import send_scan_alert
from src.features.form import momentum_score, match_load, form_direction_label
from src.features.squad_context import tournament_stage_features
from src.models import dixon_coles as dc
from src.models.elo import compute_elo_series, elo_win_probability, current_ratings


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


def _load_latest_dc_params() -> dc.DixonColesParams | None:
    snap_dir = MODELS_DIR / "dixon_coles"
    if not snap_dir.exists():
        return None
    files = sorted(snap_dir.glob("params_*.pkl"))
    if not files:
        return None
    return dc.load(files[-1])


def _load_lgbm_gate() -> dict:
    """Reads models/lgbm/gate.json (written by train_lgbm.py).
    Returns {'passed': bool, 'dc_weight': float, ...}. Missing file → not passed.
    """
    import json as _json
    path = MODELS_DIR / "lgbm" / "gate.json"
    if not path.exists():
        return {"passed": False, "dc_weight": 0.5, "reason": "no gate.json"}
    try:
        return _json.loads(path.read_text())
    except Exception as e:
        return {"passed": False, "dc_weight": 0.5, "reason": f"unreadable gate.json: {e}"}


def _load_latest_lgbm():
    """Loads LGBM model only if the ensemble gate passed (gate.json)."""
    gate = _load_lgbm_gate()
    if not gate.get("passed"):
        return None
    try:
        from src.models import lgbm_model
        model_path = MODELS_DIR / "lgbm" / "model.pkl"
        if model_path.exists():
            return lgbm_model.load_model(model_path)
    except ImportError:
        pass
    return None


def _load_calibrators():
    try:
        from src.ensemble.calibration import load_calibrators
        path = MODELS_DIR / "lgbm" / "calibrators.pkl"
        if path.exists():
            return load_calibrators(path)
    except Exception:
        pass
    return None


def _load_cluster_calibrators():
    try:
        from src.config import PER_CLUSTER_CALIBRATION_ENABLED
        if not PER_CLUSTER_CALIBRATION_ENABLED:
            return None
        from src.ensemble.calibration import load_cluster_calibrators
        path = MODELS_DIR / "lgbm" / "cluster_calibrators.pkl"
        if path.exists():
            return load_cluster_calibrators(path)
    except Exception:
        pass
    return None


def _load_stacker():
    try:
        from src.config import STACKER_ENABLED
        if not STACKER_ENABLED:
            return None
        from src.ensemble.stacking import Stacker
        path = MODELS_DIR / "lgbm" / "stacker.pkl"
        if path.exists():
            return Stacker.load(path)
    except Exception:
        pass
    return None


def _load_conformal():
    try:
        from src.config import CONFORMAL_ENABLED
        if not CONFORMAL_ENABLED:
            return None
        from src.ensemble.conformal import ConformalPredictor
        path = MODELS_DIR / "lgbm" / "conformal.pkl"
        if path.exists():
            return ConformalPredictor.load(path)
    except Exception:
        pass
    return None


def _squad_adjust(
    final_arr: np.ndarray,
    home_squad: SquadReport,
    away_squad: SquadReport,
    weight: float = 0.30,
) -> np.ndarray:
    """Shifts home/away win probs by squad availability difference.
    No-op only when BOTH sources are default (no real data at all).
    Covers.com, Transfermarkt, Wikipedia all count as real data.
    """
    both_default = (
        home_squad.data_source == "default"
        and away_squad.data_source == "default"
    )
    if both_default:
        return final_arr
    avail_diff = home_squad.availability_score - away_squad.availability_score
    shift = avail_diff * weight
    adjusted = final_arr.copy()
    adjusted[2] = max(0.01, adjusted[2] + shift)
    adjusted[0] = max(0.01, adjusted[0] - shift)
    adjusted[1] = max(0.01, adjusted[1])
    return adjusted / adjusted.sum()


def _rank_adjust(
    final_arr: np.ndarray,
    home: str,
    away: str,
    weight: float = 0.03,
) -> np.ndarray:
    """Small shift based on FIFA ranking difference. Complements Elo.
    Effect is capped at ±weight (3%). Applied after squad adjustment.
    Positive rank_diff = home is better ranked (lower rank number = stronger).
    """
    from src.data.fifa_rankings import get_fifa_rank_diff
    rank_diff = get_fifa_rank_diff(home, away)  # positive = home better ranked
    # Normalize: every 50 rank positions = full weight unit
    shift = float(np.clip(rank_diff / 50.0 * weight, -weight, weight))
    adjusted = final_arr.copy()
    adjusted[2] = max(0.01, adjusted[2] + shift)
    adjusted[0] = max(0.01, adjusted[0] - shift)
    adjusted[1] = max(0.01, adjusted[1])
    return adjusted / adjusted.sum()


def _form_context(team: str, scan_date: pd.Timestamp, historical: pd.DataFrame) -> dict:
    """Computes display-only form context for scan report."""
    mom = momentum_score(team, scan_date, historical)
    load = match_load(team, scan_date, historical)
    direction = form_direction_label(mom["form_trend"])
    fatigue = load["matches_30d"] >= 4
    return {
        "momentum": mom,
        "load": load,
        "direction": direction,
        "fatigue": fatigue,
    }


def _match_ts_utc(match: dict) -> pd.Timestamp:
    """Parses commence_time to UTC Timestamp, falls back to far future."""
    try:
        ts = pd.Timestamp(match.get("commence_time", ""))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts
    except Exception:
        return pd.Timestamp("2099-01-01", tz="UTC")


def run_daily_scan(
    bankroll: float = 1000.0,
    api_key: str | None = None,
    mock: bool = False,
    output_path: Path | None = None,
    auto_log: bool = False,
    horizon_hours: int | None = None,
    scan_date_filter: str | None = None,
    force: bool = False,
) -> tuple[pd.DataFrame, list, list, dict, dict]:
    """
    Main scan orchestrator.
    Returns (signals_df, all_signals, selected_signals, match_date_lookup, match_contexts).
    all_signals: every value bet found (pre portfolio cap) — for dashboard display.
    selected_signals: post portfolio cap — for ledger logging.

    Args:
        force: Skip the WM date guard (useful for testing before/after tournament).
        mock:  Use synthetic match data — also bypasses the date guard (no quota used).
    """
    # --- Date guard: skip expensive API call when WM 2026 is not active ---
    # Mock mode bypasses this (no real API quota used).
    # Pass force=True to override (e.g. pre-tournament testing).
    if not mock and not force and not _is_wm_active():
        today = datetime.now()
        if today < _WM_2026_START:
            days_until = (_WM_2026_START.date() - today.date()).days
            print(
                f"[{today.strftime('%Y-%m-%d')}] WM 2026 starts in {days_until} day(s) "
                f"({_WM_2026_START.strftime('%Y-%m-%d')}). Scan skipped — no quota used.\n"
                f"Next scan should be run on or after {_WM_2026_START.strftime('%Y-%m-%d')}."
            )
        else:
            print(
                f"[{today.strftime('%Y-%m-%d')}] WM 2026 ended on "
                f"{_WM_2026_END.strftime('%Y-%m-%d')}. Scan skipped — no quota used."
            )
        return pd.DataFrame(), [], [], {}, {}

    print("Loading DC model...")
    dc_params = _load_latest_dc_params()
    if dc_params is None:
        raise RuntimeError(
            "No Dixon-Coles model found. Run: python scripts/train_dixon_coles.py"
        )

    lgbm_model = _load_latest_lgbm()
    calibrators = _load_calibrators() if lgbm_model else None
    cluster_calibrators = _load_cluster_calibrators() if lgbm_model else None
    _gate = _load_lgbm_gate()
    _dc_weight = float(_gate.get("dc_weight", 0.5))
    stacker = _load_stacker()
    conformal = _load_conformal()

    if lgbm_model:
        print(f"LightGBM model loaded (gate ✅ dc_weight={_dc_weight:.2f}) — ensemble active.")
    else:
        reason = _gate.get("reason", "no model")
        print(f"LightGBM disabled ({reason}) — Dixon-Coles only.")
    if stacker is not None:
        print(f"Stacker meta-learner loaded (n={stacker.n_training_samples}) — replaces linear blend.")
    if conformal is not None:
        print(f"Conformal predictor loaded (n={conformal.fit_calibration_size}, α=0.10) — confidence gate active.")

    print("Fetching upcoming matches...")
    if mock:
        raw_matches = mock_upcoming_matches()
        print("  [MOCK MODE] Using synthetic match data.")
    else:
        raw_matches = fetch_upcoming_matches(api_key=api_key)

    seen = {}
    for m in raw_matches:
        mid = m["match_id"]
        if mid not in seen:
            seen[mid] = m
    unique_matches = list(seen.values())
    print(f"  {len(unique_matches)} matches from API.")

    # Filter to actual WM 2026 tournament matches only.
    # Skip warm-up friendlies (before June 11) and unknown/non-qualified teams.
    wm_start = pd.Timestamp("2026-06-11", tz="UTC")
    known_teams = set(_TM_TEAMS.keys())
    filtered = []
    skipped_team, skipped_date = 0, 0
    for m in unique_matches:
        home_raw, away_raw = m["home_team"], m["away_team"]
        home_can = canonical_name(home_raw)
        away_can = canonical_name(away_raw)
        if home_can not in known_teams or away_can not in known_teams:
            skipped_team += 1
            continue
        home, away = home_raw, away_raw
        commence = m.get("commence_time", "")
        try:
            match_ts = pd.Timestamp(commence)
            if match_ts.tzinfo is None:
                match_ts = match_ts.tz_localize("UTC")
            if match_ts < wm_start:
                skipped_date += 1
                continue
        except Exception:
            pass
        filtered.append(m)

    if horizon_hours is not None:
        horizon_cutoff = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=horizon_hours)
        before_horizon = len(filtered)
        filtered = [m for m in filtered if _match_ts_utc(m) <= horizon_cutoff]
        n_beyond = before_horizon - len(filtered)
        if n_beyond:
            print(f"  Horizon filter: dropped {n_beyond} matches beyond {horizon_hours}h window.")

    if scan_date_filter is not None:
        day_start = pd.Timestamp(scan_date_filter, tz="UTC")
        day_end = day_start + pd.Timedelta(days=1)
        before = len(filtered)
        filtered = [m for m in filtered if day_start <= _match_ts_utc(m) < day_end]
        print(f"  Date filter '{scan_date_filter}': {len(filtered)} matches (dropped {before - len(filtered)}).")

    if skipped_team:
        print(f"  Skipped {skipped_team} matches with non-WM-2026 teams (pre-tournament friendlies).")
    if skipped_date:
        print(f"  Skipped {skipped_date} matches before tournament start (June 11).")
    unique_matches = filtered
    print(f"  {len(unique_matches)} WM 2026 tournament matches to scan.")

    print("Loading historical data for context...")
    try:
        historical = filter_competitive(fetch_international_results())
        elo_series = compute_elo_series(historical)
        elo_ratings = current_ratings(elo_series)
    except Exception:
        historical = pd.DataFrame()
        elo_series = pd.DataFrame()
        elo_ratings = {}

    print("Loading live xG data (StatsBomb + Sofascore WC2026)...")
    try:
        from src.features.xg_live import fetch_live_xg
        statsbomb_xg = fetch_live_xg()
        print(f"  {len(statsbomb_xg)} matches with xG data.")
    except Exception:
        statsbomb_xg = pd.DataFrame()

    player_xg_df = pd.DataFrame()
    try:
        from src.config import PLAYER_XG_ENABLED
        if PLAYER_XG_ENABLED:
            from src.data.statsbomb import fetch_statsbomb_player_xg
            player_xg_df = fetch_statsbomb_player_xg()
            print(f"  {len(player_xg_df)} player-match xG records loaded.")
    except Exception as _e:
        print(f"  [player_xg] skipped: {_e}")

    ppda_df = pd.DataFrame()
    try:
        from src.config import PPDA_LIVE_ENABLED
        if PPDA_LIVE_ENABLED:
            from src.data.statsbomb_ppda import fetch_statsbomb_ppda
            ppda_df = fetch_statsbomb_ppda()
            print(f"  {len(ppda_df)} PPDA match-rows loaded.")
    except Exception as _e:
        print(f"  [ppda] skipped: {_e}")

    fotmob_ratings_df = pd.DataFrame()
    try:
        import pickle
        from src.config import DATA_CACHE
        _fm_path = DATA_CACHE / "fotmob_ratings.pkl"
        if _fm_path.exists():
            with open(_fm_path, "rb") as _fh:
                fotmob_ratings_df = pickle.load(_fh)
            print(f"  {len(fotmob_ratings_df)} Fotmob rating records loaded.")
        else:
            print("  [fotmob] no cache — run scripts/prefetch_fotmob.py")
    except Exception as _e:
        print(f"  [fotmob] skipped: {_e}")

    # BTTS deaktiviert (Backtest 2026-06-21: 13pp Kalibrierungslücke)

    all_signals: list[BetSignal] = []
    no_value_matches: list[dict] = []
    skipped_divergence_matches: list[dict] = []
    match_contexts: dict[str, dict] = {}
    scan_date = pd.Timestamp.now()

    skipped_divergence = 0
    for match in unique_matches:
        # Normalize team names to match DC model's canonical names
        home = canonical_name(match["home_team"])
        away = canonical_name(match["away_team"])
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
                    home, away, dc_params, line=ou_line, neutral=True, rho_override=rho_staged
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
                        home, away, dc_params, line=ah_line, neutral=True, rho_override=rho_staged
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
            )
            _h1_probs = dc.predict_half_goals_range(
                home, away, dc_params, min_g=2, max_g=4, half=1, neutral=True,
            )
            _h2_probs = dc.predict_half_goals_range(
                home, away, dc_params, min_g=2, max_g=4, half=2, neutral=True,
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
            ftts_probs = dc.predict_first_scorer(home, away, dc_params, neutral=True)
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

    # D/E — Settle existing open bets, apply portfolio cap, log new bets
    settle_from_results(LEDGER_PATH)
    open_count = count_open_bets(LEDGER_PATH)
    remaining_slots = max(0, MAX_ACTIVE_BETS - open_count)
    all_signals.sort(key=lambda s: s.ev, reverse=True)
    # LOW-confidence signals must NOT consume portfolio slots — exclude from selection.
    actionable_for_slots = [s for s in all_signals if s.confidence != "LOW"]
    low_only_signals = [s for s in all_signals if s.confidence == "LOW"]
    selected_signals = actionable_for_slots[:remaining_slots]
    if len(actionable_for_slots) > remaining_slots:
        print(f"  Portfolio cap: {len(actionable_for_slots)} actionable signals → {remaining_slots} slot(s) free "
              f"({open_count}/{MAX_ACTIVE_BETS} active bets)")
    if low_only_signals:
        print(f"  {len(low_only_signals)} LOW-confidence signal(s) excluded from portfolio (DC/LGBM divergent).")
    # Build match_date_lookup from match_contexts for ledger population
    match_date_lookup: dict[str, str] = {}
    for mid, ctx in match_contexts.items():
        commence = ctx.get("commence_time", "")
        if commence:
            try:
                match_date_lookup[mid] = pd.Timestamp(commence).strftime("%Y-%m-%d")
            except Exception:
                pass

    # Logging happens externally after user confirmation (auto_log=True bypasses this)
    n_logged = 0
    if auto_log and selected_signals:
        for s in selected_signals:
            md = match_date_lookup.get(s.match_id, "")
            n_logged += append_bets([s], bankroll, LEDGER_PATH, match_date=md)
        if n_logged:
            print(f"  {n_logged} new bet(s) written to ledger.")

    # Write report
    if output_path is None:
        scan_dir = RESULTS_DIR / "scans"
        scan_dir.mkdir(parents=True, exist_ok=True)
        output_path = scan_dir / f"scan_{scan_date.strftime('%Y-%m-%d')}.md"

    report = _format_report(all_signals, no_value_matches, match_contexts, scan_date, bankroll,
                            skipped_divergence_matches, selected_signals=selected_signals)
    output_path.write_text(report)
    print(f"\nReport written to: {output_path}")
    print(f"Value bets found: {len(all_signals)}")

    # Pass selected (actionable) + low signals so Push can show LOW as warnings
    push_signals = selected_signals + low_only_signals
    sent = send_scan_alert(
        push_signals,
        {**ledger_summary(LEDGER_PATH), "bankroll": bankroll},
        scan_date.strftime("%Y-%m-%d"),
        bankroll=bankroll,
        match_contexts=match_contexts,
    )
    if sent:
        print("  Push alert sent.")
    elif push_signals:
        print("  Push: keine Subscribers oder VAPID nicht konfiguriert.")

    signals_df = pd.DataFrame([
        {
            "match_id": s.match_id, "market": s.market,
            "model_prob": s.model_prob, "decimal_odds": s.decimal_odds,
            "ev": s.ev, "kelly_f": s.kelly_f, "stake_pct": s.stake_pct,
            "stake_amount": s.stake_pct * bankroll,
            "confidence": s.confidence,
        }
        for s in all_signals
    ])
    return signals_df, all_signals, selected_signals, match_date_lookup, match_contexts


def _top_scorelines(score_matrix, n: int = 3) -> list[tuple[int, int, float]]:
    """Returns top-n most probable (home_goals, away_goals, probability) from DC matrix."""
    import numpy as np
    flat = [(i, j, float(score_matrix[i, j]))
            for i in range(score_matrix.shape[0])
            for j in range(score_matrix.shape[1])]
    return sorted(flat, key=lambda x: x[2], reverse=True)[:n]


def _get_wm_group_context(home: str, away: str) -> str:
    """Returns a group-context line if both teams are WM 2026 participants."""
    import logging
    from src.config import WM2026_GROUPS
    h_group = WM2026_GROUPS.get(home, "")
    a_group = WM2026_GROUPS.get(away, "")
    if not h_group:
        logging.warning("WM2026_GROUPS: team not found — '%s' (skipping group context)", home)
    if not a_group:
        logging.warning("WM2026_GROUPS: team not found — '%s' (skipping group context)", away)
    if h_group and a_group:
        if h_group == a_group:
            group_teams = sorted(t for t, g in WM2026_GROUPS.items() if g == h_group)
            return f"  - 🏆 WM Gruppe {h_group}: Direktduell! ({', '.join(group_teams)})"
        return f"  - 🏆 WM 2026: {home} (Gr.{h_group}) — {away} (Gr.{a_group})"
    return ""


def _format_match_context(ctx: dict) -> list[str]:
    """Renders mandatory per-match output fields (spec §6)."""
    home, away = ctx["home"], ctx["away"]
    hc, ac = ctx["home_ctx"], ctx["away_ctx"]
    hs, as_ = ctx["home_squad"], ctx["away_squad"]
    stage = ctx.get("stage", {})

    lines = []

    # Form trend + fatigue
    h_dir = hc.get("direction", "→")
    a_dir = ac.get("direction", "→")
    h_fat = " ⚠️ FATIGUE" if hc.get("fatigue") else ""
    a_fat = " ⚠️ FATIGUE" if ac.get("fatigue") else ""
    h_streak = int(hc.get("momentum", {}).get("win_streak", 0))
    a_streak = int(ac.get("momentum", {}).get("win_streak", 0))

    # Suspension overlay: append ⛔N to squad emoji if any suspensions exist
    h_susp = get_suspended_players(home)
    a_susp = get_suspended_players(away)
    h_susp_suffix = f" ⛔{len(h_susp)}" if h_susp else ""
    a_susp_suffix = f" ⛔{len(a_susp)}" if a_susp else ""

    h_pts = hc.get("momentum", {}).get("pts_last3", 0.0)
    a_pts = ac.get("momentum", {}).get("pts_last3", 0.0)
    lines.append(f"  - **{home}** form {h_dir} ({h_pts:.1f}pts) | win streak: {h_streak}{h_fat} | "
                 f"squad: {hs.ampel_status}{h_susp_suffix}")
    lines.append(f"  - **{away}** form {a_dir} ({a_pts:.1f}pts) | win streak: {a_streak}{a_fat} | "
                 f"squad: {as_.ampel_status}{a_susp_suffix}")

    if hs.risk_players:
        risks = ", ".join(f"{p.name} ({p.status})" for p in hs.risk_players[:3])
        lines.append(f"  - ⚠️ {home} risk players: {risks}")
    if as_.risk_players:
        risks = ", ".join(f"{p.name} ({p.status})" for p in as_.risk_players[:3])
        lines.append(f"  - ⚠️ {away} risk players: {risks}")

    if h_susp:
        lines.append(f"  - ⛔ {home} gesperrt: {', '.join(h_susp)}")
    if a_susp:
        lines.append(f"  - ⛔ {away} gesperrt: {', '.join(a_susp)}")

    if stage.get("is_group_stage"):
        lines.append("  - 🏟️ Group stage — draw has elevated tactical value")
    elif stage.get("is_knockout"):
        lines.append("  - ⚔️ Knockout — no draw optionality")

    # WM 2026 group context
    group_line = _get_wm_group_context(home, away)
    if group_line:
        lines.append(group_line)

    # DC expected goals + top scorelines
    lh = ctx.get("lambda_home")
    la = ctx.get("lambda_away")
    if lh is not None and la is not None:
        lines.append(f"  - 📊 DC xG: {lh:.2f} — {la:.2f} ({lh + la:.2f} total)")
    top_scores = ctx.get("top_scorelines", [])
    if top_scores:
        score_str = ", ".join(f"{i}-{j} ({p*100:.0f}%)" for i, j, p in top_scores)
        lines.append(f"  - 🎯 Wahrscheinlichste Ergebnisse: {score_str}")

    if hs.data_source == "default" or as_.data_source == "default":
        lines.append("  - ℹ️ Squad data: default (all fit) — run refresh_squad_cache.py to update")

    # Goalscorer predictions
    home_scorers = ctx.get("home_scorers", [])
    away_scorers = ctx.get("away_scorers", [])
    if home_scorers or away_scorers:
        from src.betting.goalscorer import format_goalscorer_section
        lines.extend(format_goalscorer_section(home, away, home_scorers, away_scorers))

    return lines


def _format_report(
    signals: list[BetSignal],
    no_value: list[dict],
    match_contexts: dict[str, dict],
    scan_date: pd.Timestamp,
    bankroll: float,
    skipped_divergence: list[dict] | None = None,
    selected_signals: list[BetSignal] | None = None,
) -> str:
    summary = ledger_summary(LEDGER_PATH)
    open_count = summary["n_open"]
    lines = [
        f"# WM 2026 Value Scan — {scan_date.strftime('%Y-%m-%d')}",
        "",
        f"Bankroll: €{bankroll:,.0f} | Min edge: 3% | Kelly fraction: 25% | Model/market divergence cap: 1.50x–1.75x",
        "",
        f"**Portfolio:** {open_count}/{MAX_ACTIVE_BETS} active bets | "
        f"ROI: {summary['roi_pct']:+.1f}% on {summary['n_won']+summary['n_lost']} settled "
        f"(W{summary['n_won']}/L{summary['n_lost']}) | P&L: €{summary['total_pnl']:+.2f}",
        "",
    ]

    selected_ids = {id(s) for s in selected_signals} if selected_signals is not None else None

    # Separate HIGH/MEDIUM signals (actionable) from LOW (divergent, warning only)
    actionable_signals = [s for s in signals if s.confidence != "LOW"]
    low_signals = [s for s in signals if s.confidence == "LOW"]

    def _agree_stars(n: int) -> str:
        stars = {3: "★★★", 2: "★★☆", 1: "★☆☆", 0: "☆☆☆"}
        return stars.get(n, "")

    if actionable_signals:
        lines += [
            f"## Active Bets — {len(actionable_signals)} signal(s) with EV > 3%",
            "",
            "| Kickoff (CET) | Match | Market | Model% | Odds | EV | Kelly | Stake | Confidence | Agree |",
            "|---------------|-------|--------|--------|------|----|-------|-------|------------|-------|",
        ]
        high_ev_note_shown = False
        for s in sorted(actionable_signals, key=lambda x: x.ev, reverse=True):
            match_label = f"{s.home} vs {s.away}"
            capped = selected_ids is not None and id(s) not in selected_ids
            ev_flag = " ⚠️" if s.ev > 0.30 else (" 🚫" if capped else "")
            elo_suffix = f" (Elo:{s.elo_prob*100:.1f}%)" if s.elo_prob > 0.0 else ""
            agree_str = _agree_stars(s.n_models_agree) if s.n_models_agree > 0 else ""
            _stake_eur = s.stake_eur if s.stake_eur > 0 else s.stake_pct * bankroll
            _profit = _stake_eur * (s.decimal_odds - 1)
            # Kickoff time from match context (CET) — append (KO) for knockout rounds
            kickoff_str = "—"
            ctx = match_contexts.get(s.match_id, {})
            commence = ctx.get("commence_time", "")
            if commence:
                try:
                    ko = pd.Timestamp(commence)
                    if ko.tzinfo is None:
                        ko = ko.tz_localize("UTC")
                    kickoff_str = ko.tz_convert("Europe/Berlin").strftime("%d.%m %H:%M")
                except Exception:
                    pass
            if ctx.get("stage", {}).get("is_knockout"):
                kickoff_str += "(KO)"
            lines.append(
                f"| {kickoff_str} "
                f"| {match_label} | {s.market.upper()} "
                f"| {s.model_prob*100:.1f}%{elo_suffix} "
                f"| {s.decimal_odds:.2f} "
                f"| +{s.ev*100:.1f}%{ev_flag} "
                f"| {s.kelly_f*100:.2f}% "
                f"| {'🚫 capped' if capped else f'€{_stake_eur:.0f} (+€{_profit:.0f}/−€{_stake_eur:.0f})'} "
                f"| {s.confidence} "
                f"| {agree_str} |"
            )
            if s.ev > 0.30 and not high_ev_note_shown:
                high_ev_note_shown = True

        if high_ev_note_shown:
            lines.append(
                "\n> ⚠️ **Signals with EV > 30%** may reflect confederation-bias "
                "in DC model (qualifying-match inflation). Treat with caution; "
                "verify against market context before placing."
            )

        lines += ["", "### Match Context"]
        seen_matches = set()
        for s in actionable_signals:
            mid = s.match_id
            if mid in seen_matches or mid not in match_contexts:
                continue
            seen_matches.add(mid)
            ctx = match_contexts[mid]
            lines.append(f"\n**{ctx['home']} vs {ctx['away']}**  "
                         f"P(H)={ctx['p_home']*100:.1f}% "
                         f"P(D)={ctx['p_draw']*100:.1f}% "
                         f"P(A)={ctx['p_away']*100:.1f}%")
            lines.extend(_format_match_context(ctx))
    elif not low_signals:
        n_div = len(skipped_divergence) if skipped_divergence else 0
        n_total = len(actionable_signals) + len(no_value) + n_div
        lines += [
            "## No value bets found today (all EV < 3%)",
            "",
            f"> Scanned {n_total} match(es): "
            f"{len(no_value)} with EV < 3%, "
            f"{n_div} skipped (model/market divergence too high).",
            "",
        ]
    else:
        lines += ["## No actionable value bets found today (all signals LOW or EV < 3%)", ""]

    # LOW-confidence signals: shown as divergence warnings, NOT as bets
    if low_signals:
        lines += [
            "",
            "## ⚠️ Modell-Divergenz — LOW Confidence Signals",
            "",
            "> **Nicht wetten.** DC und LightGBM zeigen in entgegengesetzte Richtungen. "
            "Diese Signals sind zur Information, aber kein Bet-Signal.",
            "",
            "| Match | Market | Ensemble% | DC divergiert | Odds | EV | Grund |",
            "|-------|--------|-----------|---------------|------|----|-------|",
        ]
        for s in sorted(low_signals, key=lambda x: x.ev, reverse=True):
            match_label = f"{s.home} vs {s.away}"
            lines.append(
                f"| ⚠️ {match_label} | {s.market.upper()} "
                f"| {s.model_prob*100:.1f}% "
                f"| ja "
                f"| {s.decimal_odds:.2f} "
                f"| +{s.ev*100:.1f}% "
                f"| LOW (DC/LGBM divergent) |"
            )

        lines += ["", "### LOW Signal Match Context"]
        seen_low = set()
        for s in low_signals:
            mid = s.match_id
            if mid in seen_low or mid not in match_contexts:
                continue
            seen_low.add(mid)
            ctx = match_contexts[mid]
            lines.append(f"\n**⚠️ {ctx['home']} vs {ctx['away']}** (LOW — DC/LGBM divergent)  "
                         f"P(H)={ctx['p_home']*100:.1f}% "
                         f"P(D)={ctx['p_draw']*100:.1f}% "
                         f"P(A)={ctx['p_away']*100:.1f}%")
            lines.extend(_format_match_context(ctx))

    if no_value:
        lines += [
            "",
            "## Tracked — No Value",
            "",
            "| Match | P(H) | P(D) | P(A) |",
            "|-------|------|------|------|",
        ]
        for m in no_value:
            lines.append(
                f"| {m['match']} "
                f"| {m['p_home']*100:.1f}% "
                f"| {m['p_draw']*100:.1f}% "
                f"| {m['p_away']*100:.1f}% |"
            )

        # Context for no-value matches
        no_val_with_ctx = [m for m in no_value if m.get("match_id") in match_contexts]
        if no_val_with_ctx:
            lines.append("\n### No-Value Match Context")
            for m in no_val_with_ctx:
                ctx = match_contexts[m["match_id"]]
                lines.append(f"\n**{ctx['home']} vs {ctx['away']}**")
                lines.extend(_format_match_context(ctx))

    if skipped_divergence:
        lines += [
            "",
            "## 🚫 Divergence-Filtered Matches",
            "",
            "> Model/market divergence exceeded threshold — excluded from signal evaluation.",
            "> High divergence typically indicates confederation bias in DC params (qualifier blowouts).",
            "",
            "| Match | Model P(H)/P(D)/P(A) | Market P(H)/P(D)/P(A) | Max Divergence | Threshold |",
            "|-------|----------------------|----------------------|----------------|-----------|",
        ]
        for m in skipped_divergence:
            lines.append(
                f"| {m['match']} "
                f"| {m['p_home']*100:.1f}%/{m['p_draw']*100:.1f}%/{m['p_away']*100:.1f}% "
                f"| {m['mkt_home']*100:.1f}%/{m['mkt_draw']*100:.1f}%/{m['mkt_away']*100:.1f}% "
                f"| {m['max_div']:.2f}x "
                f"| {m['div_threshold']:.2f}x |"
            )

    lines += [
        "",
        "---",
        "*SportsBrain — model output only. No estimates. EV > 0 required.*",
    ]
    return "\n".join(lines)

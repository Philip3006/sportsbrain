"""
Live inference pipeline for WM 2026.
Fetches upcoming matches, runs ensemble predictions, outputs value scan report.
"""
from pathlib import Path
from datetime import datetime

import pandas as pd

from src.config import (
    RESULTS_DIR, canonical_name, MAX_ACTIVE_BETS,
    HOST_BOOST_ENABLED, HOST_LAMBDA_BOOST, HOST_NATIONS,
)
from src.betting.ledger import (
    append_bets, count_open_bets, settle_from_results, ledger_summary, LEDGER_PATH,
)
from src.data.odds_api import fetch_upcoming_matches, mock_upcoming_matches
from src.data.international import fetch_international_results, filter_competitive
from src.data.squad_availability import _TM_TEAMS
from src.notifications.web_push import send_scan_alert
from src.models.elo import compute_elo_series, current_ratings

from .prep import (
    _is_wm_active, _load_latest_dc_params, _load_lgbm_gate,
    _load_latest_lgbm, _load_calibrators, _load_cluster_calibrators,
    _load_stacker, _load_conformal, _match_ts_utc,
)
from .scoring import score_matches
from .output import _format_report

_WM_2026_START = datetime(2026, 6, 11)
_WM_2026_END = datetime(2026, 7, 19)


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

    scan_date = pd.Timestamp.now()

    models = {
        'dc_params': dc_params,
        'lgbm_model': lgbm_model,
        'calibrators': calibrators,
        'cluster_calibrators': cluster_calibrators,
        'dc_weight': _dc_weight,
        'stacker': stacker,
        'conformal': conformal,
    }
    data = {
        'historical': historical,
        'elo_series': elo_series,
        'elo_ratings': elo_ratings,
        'statsbomb_xg': statsbomb_xg,
        'player_xg_df': player_xg_df,
        'ppda_df': ppda_df,
        'fotmob_ratings_df': fotmob_ratings_df,
    }

    all_signals, no_value_matches, skipped_divergence_matches, match_contexts = score_matches(
        unique_matches, models, data, bankroll, scan_date
    )

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

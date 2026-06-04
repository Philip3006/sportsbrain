"""
Live inference pipeline for WM 2026.
Fetches upcoming matches, runs ensemble predictions, outputs value scan report.
"""
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

from src.config import MODELS_DIR, RESULTS_DIR, canonical_name, MAX_ACTIVE_BETS, MAX_EV, TEAM_CONFEDERATION
from src.betting.value_detector import (
    BetSignal, detect_value, set_confidence, detect_value_totals, detect_value_ah,
)
from src.betting.ledger import (
    append_bets, count_open_bets, settle_from_results, ledger_summary, LEDGER_PATH,
)
from src.data.odds_api import fetch_upcoming_matches, mock_upcoming_matches
from src.data.international import fetch_international_results, filter_competitive
from src.data.squad_availability import default_report, squad_report, SquadReport, _TM_TEAMS
from src.notifications.telegram import send_scan_alert
from src.features.form import momentum_score, match_load, form_direction_label
from src.features.squad_context import tournament_stage_features
from src.models import dixon_coles as dc
from src.models.elo import compute_elo_series


def _load_latest_dc_params() -> dc.DixonColesParams | None:
    snap_dir = MODELS_DIR / "dixon_coles"
    if not snap_dir.exists():
        return None
    files = sorted(snap_dir.glob("params_*.pkl"))
    if not files:
        return None
    return dc.load(files[-1])


def _load_latest_lgbm():
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


def _squad_adjust(
    final_arr: np.ndarray,
    home_squad: SquadReport,
    away_squad: SquadReport,
    weight: float = 0.30,
) -> np.ndarray:
    """Shifts home/away win probs by squad availability difference. No-op if both default."""
    if home_squad.data_source == "default" and away_squad.data_source == "default":
        return final_arr
    avail_diff = home_squad.availability_score - away_squad.availability_score
    shift = avail_diff * weight
    adjusted = final_arr.copy()
    adjusted[2] = max(0.01, adjusted[2] + shift)
    adjusted[0] = max(0.01, adjusted[0] - shift)
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
) -> tuple[pd.DataFrame, list]:
    """
    Main scan orchestrator.
    1. Fetch upcoming WM 2026 matches + odds.
    2. Load DC params + optional LightGBM + calibrators.
    3. Per match: predict → detect value → compute form/squad context.
    4. Write markdown report with mandatory output fields.
    Returns DataFrame of BetSignals.
    """
    print("Loading DC model...")
    dc_params = _load_latest_dc_params()
    if dc_params is None:
        raise RuntimeError(
            "No Dixon-Coles model found. Run: python scripts/train_dixon_coles.py"
        )

    lgbm_model = _load_latest_lgbm()
    calibrators = _load_calibrators() if lgbm_model else None

    if lgbm_model:
        print("LightGBM model loaded — using ensemble predictions.")
    else:
        print("No LightGBM model found — using Dixon-Coles only.")

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
        home, away = m["home_team"], m["away_team"]
        if home not in known_teams or away not in known_teams:
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
    except Exception:
        historical = pd.DataFrame()
        elo_series = pd.DataFrame()

    print("Loading StatsBomb xG data...")
    try:
        from src.data.statsbomb import fetch_statsbomb_xg
        statsbomb_xg = fetch_statsbomb_xg()
        print(f"  {len(statsbomb_xg)} matches with xG data.")
    except Exception:
        statsbomb_xg = pd.DataFrame()
        print("  StatsBomb xG unavailable — xG features disabled.")

    all_signals: list[BetSignal] = []
    no_value_matches: list[dict] = []
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
        stage_pre = tournament_stage_features(scan_date, match.get("tournament"))
        is_ko = bool(stage_pre.get("is_knockout", False))
        try:
            dc_probs = dc.predict_match_staged(home, away, dc_params, is_knockout=is_ko, neutral=True)
        except Exception:
            dc_probs = {"p_home": 1/3, "p_draw": 1/3, "p_away": 1/3}

        dc_arr = np.array([dc_probs["p_away"], dc_probs["p_draw"], dc_probs["p_home"]])
        lgbm_raw_arr: np.ndarray | None = None  # track separately for confidence scoring

        # Ensemble if available
        if lgbm_model and not historical.empty:
            from src.features.builder import build_feature_row
            from src.models.lgbm_model import predict_proba

            try:
                feat = build_feature_row(
                    home=home, away=away,
                    match_date=scan_date,
                    historical=historical,
                    elo_series=elo_series,
                    dc_params=dc_params,
                    neutral=True,
                    tournament=match.get("tournament"),
                    statsbomb_xg=statsbomb_xg if not statsbomb_xg.empty else None,
                )
                X = pd.DataFrame([feat])
                # Align to model's trained feature set
                trained_cols = getattr(
                    lgbm_model, "feature_names_in_",
                    getattr(lgbm_model, "feature_name_", list(X.columns))
                )
                X = X.reindex(columns=trained_cols, fill_value=0.0)
                lgbm_raw_arr = predict_proba(lgbm_model, X)[0]

                if calibrators:
                    from src.ensemble.calibration import calibrate
                    from src.ensemble.combiner import blend
                    blended = blend(dc_probs, lgbm_raw_arr)
                    final_arr = calibrate(blended.reshape(1, -1), calibrators)[0]
                else:
                    from src.ensemble.combiner import blend
                    final_arr = blend(dc_probs, lgbm_raw_arr)
            except Exception:
                final_arr = dc_arr
        else:
            final_arr = dc_arr

        # Skip if model diverges significantly from Shin-corrected market in EITHER direction.
        # Catches both: model overestimates weak team (e.g. DR Congo) AND
        # underestimates strong favorite (e.g. Portugal), which is the same bias.
        from src.betting.odds_utils import remove_margin_shin
        mkt_h, mkt_d, mkt_a = remove_margin_shin(raw_odds)
        mkt_arr = np.array([mkt_a, mkt_d, mkt_h])
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
            continue

        # Form + squad context for display
        match_id = match["match_id"]
        if not historical.empty:
            home_ctx = _form_context(home, scan_date, historical)
            away_ctx = _form_context(away, scan_date, historical)
        else:
            home_ctx = away_ctx = {"direction": "→", "fatigue": False,
                                   "momentum": {}, "load": {}}

        home_squad = squad_report(home, scan_date)
        away_squad = squad_report(away, scan_date)
        final_arr = _squad_adjust(final_arr, home_squad, away_squad)

        match_contexts[match_id] = {
            "home": home, "away": away,
            "home_ctx": home_ctx, "away_ctx": away_ctx,
            "home_squad": home_squad, "away_squad": away_squad,
            "stage": stage_pre,
            "p_home": float(final_arr[2]),
            "p_draw": float(final_arr[1]),
            "p_away": float(final_arr[0]),
        }

        signals = detect_value(
            home, away, final_arr, raw_odds,
            bankroll=bankroll, match_id=match_id,
        )

        if lgbm_model and lgbm_raw_arr is not None:
            signals = [set_confidence(s, dc_probs, lgbm_raw_arr) for s in signals]

        # O/U 2.5 market
        over_odds = float(match.get("over_odds", 0))
        under_odds = float(match.get("under_odds", 0))
        if over_odds > 1.0 or under_odds > 1.0:
            totals = dc.predict_totals(home, away, dc_params, neutral=True)
            ou_signals = detect_value_totals(
                home, away, totals, over_odds, under_odds,
                bankroll=bankroll, match_id=match_id,
            )
            signals.extend(ou_signals)

        # Asian Handicap -0.5/+0.5
        ah_home_odds = float(match.get("ah_home_odds", 0))
        ah_away_odds = float(match.get("ah_away_odds", 0))
        if ah_home_odds > 1.0 or ah_away_odds > 1.0:
            ah_probs = dc.predict_asian_handicap(home, away, dc_params, neutral=True)
            ah_signals = detect_value_ah(
                home, away, ah_probs, ah_home_odds, ah_away_odds,
                bankroll=bankroll, match_id=match_id,
            )
            signals.extend(ah_signals)

        # Filter out signals with unrealistically high EV (model artifact)
        signals = [s for s in signals if s.ev <= MAX_EV]

        # 1-bet-per-match: keep only the highest-EV signal per match.
        # AH -0.5 and 1X2 home share >80% outcome correlation — stacking them
        # effectively doubles exposure on the same result with no diversification.
        if signals:
            best = max(signals, key=lambda s: s.ev)
            all_signals.append(best)
        else:
            no_value_matches.append({
                "match": f"{home} vs {away}",
                "p_home": float(final_arr[2]),
                "p_draw": float(final_arr[1]),
                "p_away": float(final_arr[0]),
                "match_id": match_id,
            })

    if skipped_divergence:
        print(f"  Skipped {skipped_divergence} matches: model/market divergence too high (confederation bias filter).")

    # D/E — Settle existing open bets, apply portfolio cap, log new bets
    settle_from_results(LEDGER_PATH)
    open_count = count_open_bets(LEDGER_PATH)
    remaining_slots = max(0, MAX_ACTIVE_BETS - open_count)
    all_signals.sort(key=lambda s: s.ev, reverse=True)
    selected_signals = all_signals[:remaining_slots]
    if len(all_signals) > remaining_slots:
        print(f"  Portfolio cap: {len(all_signals)} signals → {remaining_slots} slot(s) free "
              f"({open_count}/{MAX_ACTIVE_BETS} active bets)")
    # Logging happens externally after user confirmation (auto_log=True bypasses this)
    n_logged = 0
    if auto_log and selected_signals:
        n_logged = append_bets(selected_signals, bankroll, LEDGER_PATH)
        if n_logged:
            print(f"  {n_logged} new bet(s) written to ledger.")

    # Write report
    if output_path is None:
        scan_dir = RESULTS_DIR / "scans"
        scan_dir.mkdir(parents=True, exist_ok=True)
        output_path = scan_dir / f"scan_{scan_date.strftime('%Y-%m-%d')}.md"

    report = _format_report(all_signals, no_value_matches, match_contexts, scan_date, bankroll)
    output_path.write_text(report)
    print(f"\nReport written to: {output_path}")
    print(f"Value bets found: {len(all_signals)}")

    sent = send_scan_alert(
        selected_signals,
        {**ledger_summary(LEDGER_PATH), "bankroll": bankroll},
        scan_date.strftime("%Y-%m-%d"),
        bankroll=bankroll,
    )
    if sent:
        print("  Telegram alert sent.")
    elif selected_signals:
        print("  Telegram: no token configured (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env).")

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
    return signals_df, selected_signals


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

    lines.append(f"  - **{home}** form {h_dir} | win streak: {h_streak}{h_fat} | "
                 f"squad: {hs.ampel_status}")
    lines.append(f"  - **{away}** form {a_dir} | win streak: {a_streak}{a_fat} | "
                 f"squad: {as_.ampel_status}")

    if hs.risk_players:
        risks = ", ".join(f"{p.name} ({p.status})" for p in hs.risk_players[:3])
        lines.append(f"  - ⚠️ {home} risk players: {risks}")
    if as_.risk_players:
        risks = ", ".join(f"{p.name} ({p.status})" for p in as_.risk_players[:3])
        lines.append(f"  - ⚠️ {away} risk players: {risks}")

    if stage.get("is_group_stage"):
        lines.append("  - 🏟️ Group stage — draw has elevated tactical value")
    elif stage.get("is_knockout"):
        lines.append("  - ⚔️ Knockout — no draw optionality")

    if hs.data_source == "default" or as_.data_source == "default":
        lines.append("  - ℹ️ Squad data: default (all fit) — Transfermarkt not yet connected")

    return lines


def _format_report(
    signals: list[BetSignal],
    no_value: list[dict],
    match_contexts: dict[str, dict],
    scan_date: pd.Timestamp,
    bankroll: float,
) -> str:
    summary = ledger_summary(LEDGER_PATH)
    open_count = summary["n_open"]
    lines = [
        f"# WM 2026 Value Scan — {scan_date.strftime('%Y-%m-%d')}",
        "",
        f"Bankroll: €{bankroll:,.0f} | Min edge: 3% | Kelly fraction: 25% | Model/market divergence cap: 2.0x",
        "",
        f"**Portfolio:** {open_count}/{MAX_ACTIVE_BETS} active bets | "
        f"ROI: {summary['roi_pct']:+.1f}% on {summary['n_won']+summary['n_lost']} settled "
        f"(W{summary['n_won']}/L{summary['n_lost']}) | P&L: €{summary['total_pnl']:+.2f}",
        "",
    ]

    if signals:
        lines += [
            f"## Active Bets — {len(signals)} signal(s) with EV > 3%",
            "",
            "| Match | Market | Model% | Odds | EV | Kelly | Stake | Confidence |",
            "|-------|--------|--------|------|----|-------|-------|------------|",
        ]
        high_ev_note_shown = False
        for s in sorted(signals, key=lambda x: x.ev, reverse=True):
            match_label = f"{s.home} vs {s.away}"
            ev_flag = " ⚠️" if s.ev > 0.30 else ""
            lines.append(
                f"| {match_label} | {s.market.upper()} "
                f"| {s.model_prob*100:.1f}% "
                f"| {s.decimal_odds:.2f} "
                f"| +{s.ev*100:.1f}%{ev_flag} "
                f"| {s.kelly_f*100:.2f}% "
                f"| €{s.stake_pct * bankroll:.0f} "
                f"| {s.confidence} |"
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
        for s in signals:
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
    else:
        lines += ["## No value bets found today (all EV < 3%)", ""]

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

    lines += [
        "",
        "---",
        "*SportsBrain — model output only. No estimates. EV > 0 required.*",
    ]
    return "\n".join(lines)

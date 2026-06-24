"""
Report formatting functions for the daily scan pipeline.
All functions here are pure display/text generators — no model inference or I/O.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
SIGNAL_HISTORY = _ROOT / "data" / "cache" / "signal_history.jsonl"

from src.betting.ledger import ledger_summary, LEDGER_PATH
from src.betting.value_detector import BetSignal
from src.config import MAX_ACTIVE_BETS
from src.data.squad_availability import get_suspended_players


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
            from src.config import WM2026_GROUPS as _G
            group_teams = sorted(t for t, g in _G.items() if g == h_group)
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
                lines.append("> ⚠️ EV >30% — model artifact likely. Verify odds manually.")

        lines.append("")

        for s in sorted(actionable_signals, key=lambda x: x.ev, reverse=True):
            mid = s.match_id
            if mid not in match_contexts:
                continue
            ctx = match_contexts[mid]
            capped = selected_ids is not None and id(s) not in selected_ids
            capped_str = " 🚫 CAPPED (portfolio full)" if capped else ""
            lines.append(
                f"\n**{ctx['home']} vs {ctx['away']}**{capped_str}  "
                f"P(H)={ctx['p_home']*100:.1f}% "
                f"P(D)={ctx['p_draw']*100:.1f}% "
                f"P(A)={ctx['p_away']*100:.1f}%"
            )
            lines.extend(_format_match_context(ctx))

    else:
        lines += [
            "## No Value Bets Found",
            "",
            "> No signals passed the min-edge and EV filters today.",
            "",
        ]

    if low_signals:
        lines += [
            "",
            "## ⚠️ LOW Confidence Signals (DC/LGBM Divergent)",
            "",
            "| Kickoff (CET) | Match | Market | Model% | Odds | EV | Kelly | Stake |",
            "|---------------|-------|--------|--------|------|----|-------|-------|",
        ]
        for s in sorted(low_signals, key=lambda x: x.ev, reverse=True):
            match_label = f"{s.home} vs {s.away}"
            _stake_eur = s.stake_eur if s.stake_eur > 0 else s.stake_pct * bankroll
            _profit = _stake_eur * (s.decimal_odds - 1)
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
                f"| {s.model_prob*100:.1f}% "
                f"| {s.decimal_odds:.2f} "
                f"| +{s.ev*100:.1f}% "
                f"| {s.kelly_f*100:.2f}% "
                f"| €{_stake_eur:.0f} (+€{_profit:.0f}/−€{_stake_eur:.0f}) |"
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


def archive_signals(
    all_signals: list["BetSignal"],
    selected_ids: set[tuple[str, str]],
    scan_ts: str,
    sport: str = "football",
) -> int:
    """Append all signals to signal_history.jsonl. Returns count of new entries written.

    Deduplicates by (match_id, market, scan_date) so re-runs don't double-write.
    selected_ids: set of (match_id, market) tuples that were actually placed.
    """
    SIGNAL_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    scan_date = scan_ts[:10]

    existing: set[tuple[str, str, str]] = set()
    if SIGNAL_HISTORY.exists():
        for line in SIGNAL_HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                existing.add((row["match_id"], row["market"], row["scan_date"]))
            except Exception:
                pass

    written = 0
    with SIGNAL_HISTORY.open("a", encoding="utf-8") as f:
        for s in all_signals:
            key = (s.match_id, s.market, scan_date)
            if key in existing:
                continue
            entry = {
                "scan_ts": scan_ts,
                "scan_date": scan_date,
                "sport": sport,
                "match_id": s.match_id,
                "home": s.home,
                "away": s.away,
                "market": s.market,
                "model_prob": round(s.model_prob, 4),
                "fair_prob": round(s.fair_prob, 4),
                "decimal_odds": round(s.decimal_odds, 4),
                "ev_pct": round(s.ev * 100, 2),
                "confidence": s.confidence,
                "n_models_agree": s.n_models_agree,
                "placed": (s.match_id, s.market) in selected_ids,
                "outcome": None,
                "outcome_ts": None,
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            existing.add(key)
            written += 1
    return written

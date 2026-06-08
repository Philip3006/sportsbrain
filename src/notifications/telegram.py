"""
Telegram alert for SportsBrain scan results.
Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
No token = silent no-op (never raises).
"""
from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from src.betting.value_detector import BetSignal
from src.config import MAX_ACTIVE_BETS

_ENV_PATH = Path(__file__).parent.parent.parent / ".env"


def _market_label(market: str, home: str, away: str) -> str:
    m = market.lower()
    if m == "home":
        return f"{home} gewinnt"
    if m == "away":
        return f"{away} gewinnt"
    if m == "draw":
        return "Unentschieden"
    if m == "o/u2.5_over":
        return "Over 2.5 Tore"
    if m == "o/u2.5_under":
        return "Under 2.5 Tore"
    if m == "ah-0.5_home":
        return f"AH -0.5: {home} gewinnt (kein Draw)"
    if m == "ah+0.5_away":
        return f"AH +0.5: {away} gewinnt oder Draw"
    if m == "ah-1.0_home":
        return f"AH -1.0: {home} gewinnt mit 2+ Toren (Push: genau 1)"
    if m == "ah+1.0_away":
        return f"AH +1.0: {away} gewinnt, Unentschieden oder Heimsieg genau 1 Tor (Push)"
    if m == "ah-1.5_home":
        return f"AH -1.5: {home} gewinnt mit 2+ Toren"
    if m == "ah+1.5_away":
        return f"AH +1.5: {away} gewinnt, Unentschieden oder verliert mit 1 Tor"
    if m == "btts_yes":
        return "BTTS: Beide Teams treffen"
    if m == "btts_no":
        return "BTTS: Mindestens ein Team trifft nicht"
    # Tennis set handicap markets (home = player_a, away = player_b)
    if m == "ah-1.5_a":
        return f"🎾 {home} gewinnt 3:0 oder 3:1"
    if m == "ah+1.5_b":
        return f"🎾 {away} gewinnt oder verliert max. 1 Satz"
    return market.upper()


def _post(token: str, chat_id: str, text: str, reply_markup: dict | None = None) -> bool:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=10,
        )
        return resp.ok
    except Exception:
        return False


def send_scan_alert(
    signals: list[BetSignal],
    summary: dict,
    scan_date: str,
    bankroll: float = 1000.0,
    match_contexts: dict | None = None,
) -> bool:
    """
    Sends scan alert with inline buttons for each match (Analyse + team Ratings).
    Returns True if message was sent.
    """
    load_dotenv(dotenv_path=_ENV_PATH)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id or not signals:
        return False

    SEP = "─────────────────────"

    # Separate actionable (HIGH/MEDIUM) from divergent (LOW) signals
    actionable = [s for s in signals if s.confidence != "LOW"]
    low_signals = [s for s in signals if s.confidence == "LOW"]

    top = sorted(actionable, key=lambda s: s.ev, reverse=True)[:5]

    lines = [f"<b>WM 2026 — {scan_date}</b>", SEP]

    if top:
        for s in top:
            stake_eur = s.stake_pct * bankroll
            profit = stake_eur * (s.decimal_odds - 1)

            if s.b365_odds > 1.0:
                odds_str = f"Bet365: {s.b365_odds:.2f}"
                if s.b365_odds < s.decimal_odds - 0.02:
                    odds_str += f"  (Markt: {s.decimal_odds:.2f})"
            else:
                odds_str = f"Kurs: {s.decimal_odds:.2f}  (Bet365 n.v.)"

            conf = "  Beide Modelle einig" if s.confidence == "HIGH" else ""
            elo_suffix = f" (Elo {s.elo_prob*100:.1f}%)" if s.elo_prob > 0.0 else ""
            agree_stars = {3: "★★★", 2: "★★☆", 1: "★☆☆", 0: "☆☆☆"}.get(s.n_models_agree, "")
            agree_label = f"  {agree_stars} ({s.n_models_agree}/3 Modelle)" if s.n_models_agree > 0 else ""

            # Match context: xG, BTTS, group, kickoff
            ctx_lines = []
            if match_contexts and s.match_id in match_contexts:
                ctx = match_contexts[s.match_id]
                lh = ctx.get("lambda_home")
                la = ctx.get("lambda_away")
                if lh is not None and la is not None:
                    ctx_lines.append(f"xG:        {lh:.2f} — {la:.2f} ({lh + la:.2f} total)")
                p_btts = ctx.get("p_btts_yes")
                if p_btts is not None:
                    ctx_lines.append(f"BTTS:      {p_btts*100:.0f}%")
                from src.config import WM2026_GROUPS
                h_grp = WM2026_GROUPS.get(s.home, "")
                a_grp = WM2026_GROUPS.get(s.away, "")
                if h_grp and a_grp and h_grp == a_grp:
                    ctx_lines.append(f"Gruppe {h_grp}: Direktduell!")
                top_scores = ctx.get("top_scorelines", [])
                if top_scores:
                    sc_str = "  ".join(f"{i}-{j}({p*100:.0f}%)" for i, j, p in top_scores)
                    ctx_lines.append(f"Scores:    {sc_str}")
                commence = ctx.get("commence_time", "")
                if commence:
                    try:
                        import pandas as pd
                        ko = pd.Timestamp(commence)
                        if ko.tzinfo is None:
                            ko = ko.tz_localize("UTC")
                        ko_cet = ko.tz_convert("Europe/Berlin")
                        ctx_lines.append(f"Anpfiff:   {ko_cet.strftime('%d.%m. %H:%M')} CET")
                    except Exception:
                        pass

            lines += [
                f"<b>{s.home} vs {s.away}</b>",
                f"Tipp:      {_market_label(s.market, s.home, s.away)}",
                f"Quote:     {odds_str}",
                f"Modell:    {s.model_prob*100:.1f}%{elo_suffix}   EV: +{s.ev*100:.1f}%{conf}{agree_label}",
                f"Einsatz:   {stake_eur:.2f} EUR",
                f"Gewinn:    +{profit:.2f} EUR   Verlust: -{stake_eur:.2f} EUR",
            ] + ctx_lines + [SEP]
    else:
        lines += ["<i>Keine actionable Signals heute.</i>", SEP]

    n_open = summary.get("n_open", 0)
    pnl = summary.get("total_pnl", 0.0)
    roi = summary.get("roi_pct", 0.0)
    n_won = summary.get("n_won", 0)
    n_lost = summary.get("n_lost", 0)
    lines.append(
        f"<b>Portfolio:</b> {n_open}/{MAX_ACTIVE_BETS} aktiv   "
        f"G/V: {pnl:+.2f} EUR   ROI: {roi:+.1f}%   W{n_won}/L{n_lost}"
    )
    clv = summary.get("mean_clv", None)
    if clv is not None and (n_won + n_lost) > 0:
        lines.append(f"CLV: {clv*100:+.1f}%")

    # Append LOW signals as a warning block (never in actionable top-5)
    if low_signals:
        lines += [
            "",
            SEP,
            "<b>⚠️ Modell-Divergenz (NICHT wetten)</b>",
            "<i>DC und LightGBM zeigen in entgegengesetzte Richtungen:</i>",
        ]
        for s in sorted(low_signals, key=lambda s: s.ev, reverse=True):
            lines.append(
                f"⚠️ <b>{s.home} vs {s.away}</b> — "
                f"{_market_label(s.market, s.home, s.away)}  "
                f"EV: +{s.ev*100:.1f}%  LOW (DC/LGBM divergent)"
            )
        lines.append(SEP)

    keyboard = []
    for s in top:
        keyboard.append([{
            "text": f"Analyse: {s.home} vs {s.away}",
            "callback_data": f"/analyse {s.home} vs {s.away}",
        }])
        keyboard.append([
            {"text": f"Rating {s.home}", "callback_data": f"/rating {s.home}"},
            {"text": f"Rating {s.away}", "callback_data": f"/rating {s.away}"},
        ])

    full_text = "\n".join(lines)
    # Telegram hard limit is 4096 chars per message. If exceeded, split: send first
    # 3800 chars as main message (with keyboard), then continue in a follow-up.
    if len(full_text) <= 3800:
        return _post(token, chat_id, full_text, {"inline_keyboard": keyboard})

    # Split at 3800 chars on a newline boundary to avoid mid-word cuts
    cut = full_text.rfind("\n", 0, 3800)
    if cut == -1:
        cut = 3800
    part1, part2 = full_text[:cut], full_text[cut:].lstrip("\n")
    ok = _post(token, chat_id, part1, {"inline_keyboard": keyboard})
    if part2:
        _post(token, chat_id, part2)
    return ok


def send_settlement_alert(record: dict, summary: dict) -> bool:
    """
    Sends a won/lost notification after a bet settles.
    record: dict with keys home, away, market, decimal_odds, stake_amount, status, pnl, clv.
    """
    load_dotenv(dotenv_path=_ENV_PATH)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    status = str(record.get("status", ""))
    if status not in ("won", "lost", "void"):
        return False

    icon = {"won": "✅", "lost": "❌", "void": "↩️"}.get(status, "")
    home = str(record.get("home", ""))
    away = str(record.get("away", ""))
    market = str(record.get("market", ""))
    odds = float(record.get("decimal_odds", 0))
    stake = float(record.get("stake_amount", 0))
    pnl = float(record.get("pnl", 0))

    clv_val = record.get("clv", "")
    try:
        clv_f = float(clv_val)
        clv_str = f"CLV: {clv_f*100:+.1f}%" if abs(clv_f) > 0.001 else ""
    except (TypeError, ValueError):
        clv_str = ""

    lines = [
        f"{icon} <b>{home} vs {away}</b>",
        f"Tipp:    {_market_label(market, home, away)}  @ {odds:.2f}",
        f"Einsatz: {stake:.2f} EUR   P&L: <b>{pnl:+.2f} EUR</b>",
    ]
    if clv_str:
        lines.append(clv_str)

    n_open = summary.get("n_open", 0)
    total_pnl = summary.get("total_pnl", 0.0)
    roi = summary.get("roi_pct", 0.0)
    lines.append(f"Portfolio: {n_open}/{MAX_ACTIVE_BETS} aktiv   Gesamt: {total_pnl:+.2f} EUR   ROI: {roi:+.1f}%")

    return _post(token, chat_id, "\n".join(lines))


def send_quota_alert(remaining: int) -> bool:
    """Sends a Telegram alert when API quota is critically low (< 20 requests left)."""
    load_dotenv(dotenv_path=_ENV_PATH)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    text = (
        f"<b>⚠️ SportsBrain API-Quota kritisch niedrig</b>\n\n"
        f"Noch <b>{remaining}</b> TheOddsAPI-Requests übrig diesen Monat.\n"
        f"Scans werden pausiert wenn Quota erschöpft ist.\n"
        f"Quota: <a href='https://the-odds-api.com'>the-odds-api.com</a>"
    )
    return _post(token, chat_id, text)

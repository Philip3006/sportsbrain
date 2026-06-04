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
        return f"Asian Handicap: {home} gewinnt (kein Unentschieden)"
    if m == "ah+0.5_away":
        return f"Asian Handicap: {away} gewinnt oder Unentschieden"
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
    top = sorted(signals, key=lambda s: s.ev, reverse=True)[:5]

    lines = [f"<b>WM 2026 — {scan_date}</b>", SEP]
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
        lines += [
            f"<b>{s.home} vs {s.away}</b>",
            f"Tipp:      {_market_label(s.market, s.home, s.away)}",
            f"Quote:     {odds_str}",
            f"Modell:    {s.model_prob*100:.1f}%   EV: +{s.ev*100:.1f}%{conf}",
            f"Einsatz:   {stake_eur:.2f} EUR",
            f"Gewinn:    +{profit:.2f} EUR   Verlust: -{stake_eur:.2f} EUR",
            SEP,
        ]

    n_open = summary.get("n_open", 0)
    pnl = summary.get("total_pnl", 0.0)
    roi = summary.get("roi_pct", 0.0)
    n_won = summary.get("n_won", 0)
    n_lost = summary.get("n_lost", 0)
    lines.append(
        f"<b>Portfolio:</b> {n_open}/3 aktiv   "
        f"G/V: {pnl:+.2f} EUR   ROI: {roi:+.1f}%   W{n_won}/L{n_lost}"
    )
    clv = summary.get("mean_clv", None)
    if clv is not None and (n_won + n_lost) > 0:
        lines.append(f"CLV: {clv*100:+.1f}%")

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

    return _post(token, chat_id, "\n".join(lines), {"inline_keyboard": keyboard})

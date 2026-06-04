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


def send_scan_alert(
    signals: list[BetSignal],
    summary: dict,
    scan_date: str,
    bankroll: float = 1000.0,
) -> bool:
    """
    Sends a Telegram message with up to 5 top signals.
    Shows Bet365 odds when available, best-market odds as fallback.
    Returns True if message was sent, False if skipped (no token, no signals, or error).
    """
    load_dotenv(dotenv_path=_ENV_PATH)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id or not signals:
        return False

    top = sorted(signals, key=lambda s: s.ev, reverse=True)[:5]

    lines = [f"<b>WM 2026 Wert-Wetten — {scan_date}</b>", ""]
    for s in top:
        stake_eur = s.stake_pct * bankroll
        total_return = stake_eur * s.decimal_odds
        profit = stake_eur * (s.decimal_odds - 1)

        # Show Bet365 odds if available, otherwise best-market odds
        if s.b365_odds > 1.0:
            odds_line = f"Bet365: {s.b365_odds:.2f}"
            if s.b365_odds < s.decimal_odds - 0.02:
                odds_line += f"  (bester Kurs: {s.decimal_odds:.2f})"
        else:
            odds_line = f"Bester Kurs: {s.decimal_odds:.2f}  (Bet365 nicht verfugbar)"

        conf_line = "\nBeide Modelle einig" if s.confidence == "HIGH" else ""

        lines.append(
            f"<b>{s.home} vs {s.away}</b>\n"
            f"Tipp: {_market_label(s.market, s.home, s.away)}\n"
            f"{odds_line}\n"
            f"Modell-Wahrscheinlichkeit: {s.model_prob*100:.1f}%\n"
            f"Expected Value: +{s.ev*100:.1f}%\n"
            f"Einsatz: {stake_eur:.2f} EUR\n"
            f"Bei Gewinn: {total_return:.2f} EUR ({profit:+.2f} EUR)\n"
            f"Bei Verlust: -{stake_eur:.2f} EUR{conf_line}"
        )

    n_open = summary.get("n_open", 0)
    pnl = summary.get("total_pnl", 0.0)
    roi = summary.get("roi_pct", 0.0)
    n_won = summary.get("n_won", 0)
    n_lost = summary.get("n_lost", 0)
    clv = summary.get("mean_clv", None)

    portfolio_line = (
        f"<b>Portfolio:</b> {n_open}/3 aktive Wetten | "
        f"G/V: {pnl:+.2f} EUR | Rendite: {roi:+.1f}% | "
        f"W{n_won}/L{n_lost}"
    )
    if clv is not None and (n_won + n_lost) > 0:
        portfolio_line += f" | CLV: {clv*100:+.1f}%"

    lines += ["", portfolio_line]
    text = "\n".join(lines)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.ok
    except Exception:
        return False

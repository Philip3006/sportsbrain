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

_MARKET_LABELS = {
    "home":          "🏠 Heimsieg",
    "draw":          "🤝 Unentschieden",
    "away":          "✈️ Auswärtssieg",
    "o/u2.5_over":   "📈 Over 2.5 Tore",
    "o/u2.5_under":  "📉 Under 2.5 Tore",
    "ah-0.5_home":   "⚖️ Asian Handicap -0.5 (Heimsieg)",
    "ah+0.5_away":   "⚖️ Asian Handicap +0.5 (Auswärts/Unentschieden)",
}


def _market_label(market: str, home: str = "", away: str = "") -> str:
    m = market.lower()
    if m == "ah-0.5_home":
        return f"⚖️ Asian Handicap: {home} gewinnt (kein Unentschieden)"
    if m == "ah+0.5_away":
        return f"⚖️ Asian Handicap: {away} gewinnt oder Unentschieden"
    return _MARKET_LABELS.get(m, market.upper())


def send_scan_alert(
    signals: list[BetSignal],
    summary: dict,
    scan_date: str,
    bankroll: float = 1000.0,
) -> bool:
    """
    Sends a Telegram message with up to 5 top signals.
    Returns True if message was sent, False if skipped (no token, no signals, or error).
    """
    load_dotenv(dotenv_path=_ENV_PATH)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id or not signals:
        return False

    top = sorted(signals, key=lambda s: s.ev, reverse=True)[:5]

    lines = [f"<b>🎯 WM 2026 Wert-Wetten — {scan_date}</b>", ""]
    for s in top:
        conf = "\n  ⭐ Beide Modelle einig" if s.confidence == "HIGH" else ""
        stake_eur = s.stake_pct * bankroll
        total_return = stake_eur * s.decimal_odds
        profit = stake_eur * (s.decimal_odds - 1)
        lines.append(
            f"<b>{s.home} vs {s.away}</b>\n"
            f"  Markt: {_market_label(s.market, s.home, s.away)}\n"
            f"  Modell-Wahrscheinlichkeit: {s.model_prob*100:.1f}%\n"
            f"  Quote: {s.decimal_odds:.2f}\n"
            f"  Expected Value: +{s.ev*100:.1f}%\n"
            f"  Einsatz: €{stake_eur:.2f}\n"
            f"  Bei Gewinn: €{total_return:.2f} zurück (€{profit:.2f} Gewinn)\n"
            f"  Bei Verlust: -€{stake_eur:.2f}{conf}"
        )

    n_open = summary.get("n_open", 0)
    pnl = summary.get("total_pnl", 0.0)
    roi = summary.get("roi_pct", 0.0)
    n_won = summary.get("n_won", 0)
    n_lost = summary.get("n_lost", 0)
    clv = summary.get("mean_clv", None)

    portfolio_line = (
        f"📊 <b>Portfolio:</b> {n_open}/3 aktive Wetten | "
        f"Gewinn/Verlust: €{pnl:+.2f} | Rendite: {roi:+.1f}% | "
        f"Gewonnen: {n_won} / Verloren: {n_lost}"
    )
    if clv is not None and (n_won + n_lost) > 0:
        portfolio_line += f" | Closing Line Value: {clv*100:+.1f}%"

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

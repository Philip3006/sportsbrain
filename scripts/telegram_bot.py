"""
Telegram command responder for SportsBrain.
Reads unprocessed messages AND button taps (callback queries) and answers them.

Commands (text or button):
  /analyse <home> vs <away>   Full match analysis
  /rating <team>              Team profile (Elo, market value, form)
  /scan                       Quick scan for today
  /hilfe                      List commands

Usage:
  python scripts/telegram_bot.py     # process pending messages once
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_OFFSET_FILE = Path(__file__).parent.parent / "data" / "cache" / "tg_offset.json"


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def _send(text: str, reply_markup: dict | None = None) -> None:
    if not TOKEN or not CHAT_ID:
        return
    payload: dict = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
    except Exception:
        pass


def _answer_callback(callback_id: str) -> None:
    """Acknowledge button tap so Telegram removes the loading indicator."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id},
            timeout=5,
        )
    except Exception:
        pass


def _get_updates(offset: int) -> list[dict]:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10,
        )
        return resp.json().get("result", [])
    except Exception:
        return []


def _load_offset() -> int:
    try:
        return json.loads(_OFFSET_FILE.read_text()).get("offset", 0)
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    _OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OFFSET_FILE.write_text(json.dumps({"offset": offset}))


# ---------------------------------------------------------------------------
# Follow-up keyboards
# ---------------------------------------------------------------------------

def _analyse_keyboard(home: str, away: str) -> dict:
    """Buttons shown after an analysis: ratings for both teams + new scan."""
    return {"inline_keyboard": [
        [
            {"text": f"Rating: {home}", "callback_data": f"/rating {home}"},
            {"text": f"Rating: {away}", "callback_data": f"/rating {away}"},
        ],
        [{"text": "Scan heute", "callback_data": "/scan"}],
    ]}


def _rating_keyboard(team: str) -> dict:
    """Buttons shown after a rating: back to hilfe or scan."""
    return {"inline_keyboard": [
        [{"text": "Scan heute", "callback_data": "/scan"}],
        [{"text": "Alle Befehle", "callback_data": "/hilfe"}],
    ]}


def _scan_keyboard(signals: list) -> dict:
    """Buttons shown after a scan: analyse each match."""
    rows = []
    for s in signals[:3]:
        rows.append([{
            "text": f"Analyse: {s.home} vs {s.away}",
            "callback_data": f"/analyse {s.home} vs {s.away}",
        }])
    return {"inline_keyboard": rows} if rows else {}


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_hilfe() -> tuple[str, dict]:
    text = (
        "<b>Verfuegbare Befehle</b>\n\n"
        "/analyse Heim vs Auswaerts  —  Vollstaendige Match-Analyse\n"
        "/rating Teamname  —  Team-Profil (Elo, Marktwert, Form)\n"
        "/scan  —  Sofort-Scan heutiger Spiele\n"
        "/hilfe  —  Diese Uebersicht"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "Scan heute", "callback_data": "/scan"}],
    ]}
    return text, keyboard


def _cmd_rating(team: str) -> tuple[str, dict]:
    from src.config import canonical_name
    from src.data.international import fetch_international_results, filter_competitive
    from src.data.market_values import SQUAD_VALUES_M
    from src.models.elo import compute_elo_series
    from src.features.form import rolling_form, momentum_score

    team = canonical_name(team)
    try:
        historical = filter_competitive(fetch_international_results())
        elo_series = compute_elo_series(historical)
        now = pd.Timestamp.now()

        home_rows = elo_series[elo_series["home_team"] == team]
        away_rows = elo_series[elo_series["away_team"] == team]
        candidates = []
        if not home_rows.empty:
            candidates.append(float(home_rows.iloc[-1]["elo_home_post"]))
        if not away_rows.empty:
            candidates.append(float(away_rows.iloc[-1]["elo_away_post"]))
        elo = max(candidates) if candidates else 1500.0

        form = rolling_form(team, now, historical, competitive_only=True)
        mom = momentum_score(team, now, historical)
        mv = SQUAD_VALUES_M.get(team, 0)

        text = (
            f"<b>{team}</b>\n\n"
            f"Elo-Rating: {elo:.0f}\n"
            f"Kaderwert: {mv:.0f} Mio EUR\n"
            f"Form (letzte 5): {form['form_pts']:.1f} Pkt/Spiel\n"
            f"Tore/Spiel: {form['form_gf']:.2f}\n"
            f"Gegentore/Spiel: {form['form_ga']:.2f}\n"
            f"Siegesserie: {int(mom['win_streak'])}\n"
            f"Formtrend: {mom['form_trend']:+.2f}"
        )
    except Exception as e:
        text = f"Fehler bei {team}: {e}"

    return text, _rating_keyboard(team)


def _cmd_analyse(home: str, away: str) -> tuple[str, dict]:
    from src.config import canonical_name
    from src.data.international import fetch_international_results, filter_competitive
    from src.data.market_values import SQUAD_VALUES_M, get_market_value_ratio
    from src.models.elo import compute_elo_series, elo_win_probability
    from src.models import dixon_coles as dc
    from src.features.form import rolling_form
    from src.features.head_to_head import h2h_stats

    home = canonical_name(home)
    away = canonical_name(away)
    now = pd.Timestamp.now()

    try:
        historical = filter_competitive(fetch_international_results())
        elo_series = compute_elo_series(historical)

        snap_dir = Path(__file__).parent.parent / "models" / "dixon_coles"
        files = sorted(snap_dir.glob("params_*.pkl"))
        if not files:
            return "Kein Modell gefunden.", {}
        params = dc.load(files[-1])

        dc_probs = dc.predict_match_staged(home, away, params, is_knockout=False, neutral=True)

        def last_elo(team: str) -> float:
            hr = elo_series[elo_series["home_team"] == team]
            ar = elo_series[elo_series["away_team"] == team]
            cands = []
            if not hr.empty:
                cands.append(float(hr.iloc[-1]["elo_home_post"]))
            if not ar.empty:
                cands.append(float(ar.iloc[-1]["elo_away_post"]))
            return max(cands) if cands else 1500.0

        elo_h, elo_a = last_elo(home), last_elo(away)
        eh, _, ea = elo_win_probability(elo_h, elo_a, neutral=True)

        fh = rolling_form(home, now, historical, competitive_only=True)
        fa = rolling_form(away, now, historical, competitive_only=True)

        h2h = h2h_stats(home, away, now, historical)
        h2h_played = int(h2h.get("h2h_matches", 0))
        h2h_hw = int(h2h.get("h2h_home_wins", 0))
        h2h_draw = int(h2h.get("h2h_draws", 0))
        h2h_aw = h2h_played - h2h_hw - h2h_draw

        mv_h = SQUAD_VALUES_M.get(home, 0)
        mv_a = SQUAD_VALUES_M.get(away, 0)
        mv_ratio = get_market_value_ratio(home, away)
        mv_note = f"{home} staerker" if mv_ratio >= 1 else f"{away} staerker"

        text = (
            f"<b>Analyse: {home} vs {away}</b>\n\n"
            f"<b>Modell-Prognose</b>\n"
            f"{home} gewinnt: {dc_probs['p_home']*100:.1f}%\n"
            f"Unentschieden: {dc_probs['p_draw']*100:.1f}%\n"
            f"{away} gewinnt: {dc_probs['p_away']*100:.1f}%\n\n"
            f"<b>Elo-Ratings</b>\n"
            f"{home}: {elo_h:.0f}  (Siegchance: {eh*100:.1f}%)\n"
            f"{away}: {elo_a:.0f}  (Siegchance: {ea*100:.1f}%)\n\n"
            f"<b>Kaderwert</b>\n"
            f"{home}: {mv_h:.0f} Mio EUR\n"
            f"{away}: {mv_a:.0f} Mio EUR\n"
            f"Verhaeltnis: {mv_ratio:.2f}x  ({mv_note})\n\n"
            f"<b>Letzte 5 Pflichtspiele</b>\n"
            f"{home}: {fh['form_pts']:.1f} Pkt | {fh['form_gf']:.1f} Tore | {fh['form_ga']:.1f} Gegentore\n"
            f"{away}: {fa['form_pts']:.1f} Pkt | {fa['form_gf']:.1f} Tore | {fa['form_ga']:.1f} Gegentore\n\n"
            f"<b>Direktvergleich ({h2h_played} Spiele)</b>\n"
            f"{home}: {h2h_hw}S  |  Unentschieden: {h2h_draw}U  |  {away}: {h2h_aw}S"
        )
    except Exception as e:
        text = f"Fehler: {e}"
        return text, {}

    return text, _analyse_keyboard(home, away)


def _cmd_scan() -> tuple[str, dict]:
    from src.scanner.daily_scan import run_daily_scan
    from src.notifications.telegram import _market_label
    try:
        _, signals = run_daily_scan(bankroll=100.0, auto_log=False)
        if not signals:
            text = "Kein Value-Signal heute gefunden."
            return text, {"inline_keyboard": [[{"text": "Alle Befehle", "callback_data": "/hilfe"}]]}
        lines = ["<b>Tages-Scan</b>\n"]
        for s in signals[:3]:
            lines.append(
                f"<b>{s.home} vs {s.away}</b>\n"
                f"{_market_label(s.market, s.home, s.away)}  |  "
                f"Quote: {s.decimal_odds:.2f}  |  EV: +{s.ev*100:.1f}%"
            )
        return "\n\n".join(lines), _scan_keyboard(signals)
    except Exception as e:
        return f"Scan-Fehler: {e}", {}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _dispatch(text: str) -> tuple[str, dict]:
    """Routes a command string to the right handler. Returns (reply_text, keyboard)."""
    parts = text.strip().split()
    if not parts:
        return _cmd_hilfe()

    cmd = parts[0].lower().split("@")[0]

    if cmd in ("/hilfe", "/start", "/help"):
        return _cmd_hilfe()

    if cmd == "/rating" and len(parts) >= 2:
        return _cmd_rating(" ".join(parts[1:]))

    if cmd == "/analyse" and len(parts) >= 3:
        rest = " ".join(parts[1:])
        if " vs " in rest.lower():
            idx = rest.lower().index(" vs ")
            home, away = rest[:idx].strip(), rest[idx + 4:].strip()
        else:
            mid = max(1, len(parts[1:]) // 2)
            home = " ".join(parts[1:1 + mid])
            away = " ".join(parts[1 + mid:])
        return _cmd_analyse(home, away)

    if cmd == "/scan":
        return _cmd_scan()

    return (f"Unbekannter Befehl: {cmd}", {"inline_keyboard": [
        [{"text": "Alle Befehle", "callback_data": "/hilfe"}]
    ]})


# ---------------------------------------------------------------------------
# Main — process one batch of updates per run
# ---------------------------------------------------------------------------

def main() -> None:
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
        return

    offset = _load_offset()
    updates = _get_updates(offset)
    processed = 0

    for upd in updates:
        offset = upd["update_id"] + 1

        # Text message
        if "message" in upd:
            text = upd["message"].get("text", "").strip()
            if text.startswith("/"):
                reply, keyboard = _dispatch(text)
                _send(reply, keyboard or None)
                processed += 1

        # Button tap (inline keyboard callback)
        elif "callback_query" in upd:
            cq = upd["callback_query"]
            _answer_callback(cq["id"])
            data = cq.get("data", "").strip()
            if data.startswith("/"):
                reply, keyboard = _dispatch(data)
                _send(reply, keyboard or None)
                processed += 1

    _save_offset(offset)
    if processed:
        print(f"  Telegram bot: {processed} Nachricht(en) verarbeitet.")


if __name__ == "__main__":
    main()

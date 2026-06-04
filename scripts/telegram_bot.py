"""
Telegram command responder for SportsBrain.
Reads unprocessed messages from the bot chat and answers commands.

Commands:
  /analyse <home> vs <away>   Full match analysis (model reasoning, odds, value)
  /analyse <home> <away>      Same without "vs"
  /rating <team>              Team profile (Elo, market value, recent form)
  /scan                       Quick scan for today's matches
  /hilfe                      List all commands

Usage:
  python scripts/telegram_bot.py          # process pending messages once
  # Run automatically after daily_scan.py in GitHub Actions
"""
from __future__ import annotations

import glob
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


def _send(text: str) -> None:
    if not TOKEN or not CHAT_ID:
        return
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )


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
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_hilfe() -> str:
    return (
        "<b>SportsBrain Befehle</b>\n\n"
        "/analyse &lt;Heim&gt; vs &lt;Auswaerts&gt;  —  Vollstandige Match-Analyse\n"
        "/rating &lt;Team&gt;  —  Team-Profil (Elo, Marktwert, Form)\n"
        "/scan  —  Sofort-Scan heutiger Spiele\n"
        "/hilfe  —  Diese Ubersicht"
    )


def _cmd_rating(team: str) -> str:
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

        # Latest Elo
        home_rows = elo_series[elo_series["home_team"] == team]
        away_rows = elo_series[elo_series["away_team"] == team]
        candidates = []
        if not home_rows.empty:
            r = home_rows.iloc[-1]
            candidates.append((r["date"], float(r["elo_home_post"])))
        if not away_rows.empty:
            r = away_rows.iloc[-1]
            candidates.append((r["date"], float(r["elo_away_post"])))
        elo = max(candidates, key=lambda x: x[0])[1] if candidates else 1500.0

        form = rolling_form(team, now, historical, competitive_only=True)
        mom = momentum_score(team, now, historical)
        mv = SQUAD_VALUES_M.get(team, 0)

        return (
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
        return f"Fehler bei {team}: {e}"


def _cmd_analyse(home: str, away: str) -> str:
    from src.config import canonical_name
    from src.data.international import fetch_international_results, filter_competitive
    from src.data.market_values import SQUAD_VALUES_M, get_market_value_ratio
    from src.models.elo import compute_elo_series, elo_win_probability
    from src.models import dixon_coles as dc
    from src.features.form import rolling_form
    from src.features.head_to_head import h2h_stats
    from src.betting.odds_utils import remove_margin_shin

    home = canonical_name(home)
    away = canonical_name(away)
    now = pd.Timestamp.now()

    try:
        historical = filter_competitive(fetch_international_results())
        elo_series = compute_elo_series(historical)

        # DC params
        snap_dir = Path(__file__).parent.parent / "models" / "dixon_coles"
        files = sorted(snap_dir.glob("params_*.pkl"))
        if not files:
            return "Kein DC-Modell gefunden."
        params = dc.load(files[-1])

        dc_probs = dc.predict_match_staged(home, away, params, is_knockout=False, neutral=True)

        # Elo
        def last_elo(team):
            hr = elo_series[elo_series["home_team"] == team]
            ar = elo_series[elo_series["away_team"] == team]
            cands = []
            if not hr.empty:
                cands.append(float(hr.iloc[-1]["elo_home_post"]))
            if not ar.empty:
                cands.append(float(ar.iloc[-1]["elo_away_post"]))
            return max(cands) if cands else 1500.0

        elo_h = last_elo(home)
        elo_a = last_elo(away)
        eh, _, ea = elo_win_probability(elo_h, elo_a, neutral=True)

        # Form
        fh = rolling_form(home, now, historical, competitive_only=True)
        fa = rolling_form(away, now, historical, competitive_only=True)

        # H2H
        h2h = h2h_stats(home, away, now, historical)
        h2h_played = int(h2h.get("h2h_matches", 0))
        h2h_hw = int(h2h.get("h2h_home_wins", 0))
        h2h_draw = int(h2h.get("h2h_draws", 0))

        # Market value
        mv_h = SQUAD_VALUES_M.get(home, 0)
        mv_a = SQUAD_VALUES_M.get(away, 0)
        mv_ratio = get_market_value_ratio(home, away)

        return (
            f"<b>Analyse: {home} vs {away}</b>\n\n"
            f"<b>Modell-Prognose</b>\n"
            f"{home} gewinnt: {dc_probs['p_home']*100:.1f}%\n"
            f"Unentschieden: {dc_probs['p_draw']*100:.1f}%\n"
            f"{away} gewinnt: {dc_probs['p_away']*100:.1f}%\n\n"
            f"<b>Elo-Ratings</b>\n"
            f"{home}: {elo_h:.0f}  (Elo-Siegchance: {eh*100:.1f}%)\n"
            f"{away}: {elo_a:.0f}  (Elo-Siegchance: {ea*100:.1f}%)\n\n"
            f"<b>Kaderwert</b>\n"
            f"{home}: {mv_h:.0f} Mio EUR\n"
            f"{away}: {mv_a:.0f} Mio EUR\n"
            f"Verhaltnis: {mv_ratio:.2f}x ({home} {'starker' if mv_ratio >= 1 else 'schwacher'})\n\n"
            f"<b>Letzte 5 Pflichtspiele</b>\n"
            f"{home}: {fh['form_pts']:.1f} Pkt | {fh['form_gf']:.1f} Tore/Spiel\n"
            f"{away}: {fa['form_pts']:.1f} Pkt | {fa['form_gf']:.1f} Tore/Spiel\n\n"
            f"<b>Direktvergleich (letzte {h2h_played} Spiele)</b>\n"
            f"{home}: {h2h_hw}S / Unentschieden: {h2h_draw}U / {away}: {h2h_played-h2h_hw-h2h_draw}S"
        )
    except Exception as e:
        return f"Fehler bei der Analyse: {e}"


def _cmd_scan() -> str:
    from src.scanner.daily_scan import run_daily_scan
    try:
        _, signals = run_daily_scan(bankroll=100.0, auto_log=False)
        if not signals:
            return "Kein Value-Signal heute gefunden."
        lines = ["<b>Tages-Scan</b>\n"]
        for s in signals[:3]:
            from src.notifications.telegram import _market_label
            lines.append(
                f"{s.home} vs {s.away}  |  {_market_label(s.market, s.home, s.away)}\n"
                f"Quote: {s.decimal_odds:.2f}  |  EV: +{s.ev*100:.1f}%"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"Scan-Fehler: {e}"


# ---------------------------------------------------------------------------
# Main loop (run once per invocation)
# ---------------------------------------------------------------------------

def main() -> None:
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
        return

    offset = _load_offset()
    updates = _get_updates(offset)

    for upd in updates:
        offset = upd["update_id"] + 1
        msg = upd.get("message", {})
        text = msg.get("text", "").strip()

        if not text.startswith("/"):
            continue

        parts = text.split()
        cmd = parts[0].lower().split("@")[0]  # strip @botname if present

        if cmd == "/hilfe" or cmd == "/start":
            _send(_cmd_hilfe())

        elif cmd == "/rating" and len(parts) >= 2:
            team = " ".join(parts[1:])
            _send(_cmd_rating(team))

        elif cmd == "/analyse" and len(parts) >= 3:
            rest = " ".join(parts[1:])
            # Accept "Home vs Away" or "Home Away"
            if " vs " in rest.lower():
                idx = rest.lower().index(" vs ")
                home, away = rest[:idx].strip(), rest[idx+4:].strip()
            else:
                mid = len(parts[1:]) // 2
                home = " ".join(parts[1:1+mid])
                away = " ".join(parts[1+mid:])
            _send(_cmd_analyse(home, away))

        elif cmd == "/scan":
            _send("Scan laeuft...")
            _send(_cmd_scan())

        else:
            _send(f"Unbekannter Befehl: {cmd}\n\n{_cmd_hilfe()}")

    _save_offset(offset)
    if updates:
        print(f"  Telegram bot: {len(updates)} Nachricht(en) verarbeitet.")


if __name__ == "__main__":
    main()

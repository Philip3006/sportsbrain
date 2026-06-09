"""
Telegram command responder for SportsBrain.

Two modes:
  python scripts/telegram_bot.py           -- process pending messages once (for GitHub Actions)
  python scripts/telegram_bot.py --poll    -- listen 3 minutes, respond to buttons in real time
"""
from __future__ import annotations

import json
import os
import sys
import time
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
# Telegram API
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
            json=payload, timeout=10,
        )
    except Exception:
        pass


def _answer_callback(callback_id: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id}, timeout=5,
        )
    except Exception:
        pass


def _get_updates(offset: int) -> list[dict]:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 3},
            timeout=8,
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
# Keyboards
# ---------------------------------------------------------------------------

def _analyse_keyboard(home: str, away: str) -> dict:
    return {"inline_keyboard": [
        [
            {"text": f"Rating {home}", "callback_data": f"/rating {home}"},
            {"text": f"Rating {away}", "callback_data": f"/rating {away}"},
        ],
        [{"text": "Neuer Scan", "callback_data": "/scan"}],
    ]}


def _rating_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "Neuer Scan", "callback_data": "/scan"}],
        [{"text": "Alle Befehle", "callback_data": "/hilfe"}],
    ]}


def _scan_keyboard(signals: list) -> dict:
    rows = []
    for s in signals[:3]:
        rows.append([{
            "text": f"Analyse: {s.home} vs {s.away}",
            "callback_data": f"/analyse {s.home} vs {s.away}",
        }])
    rows.append([{"text": "Alle Befehle", "callback_data": "/hilfe"}])
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------------------
# Formatierung
# ---------------------------------------------------------------------------

def _line() -> str:
    return "─────────────────────"


def _cmd_hilfe() -> tuple[str, dict]:
    text = (
        "<b>SportsBrain Befehle</b>\n"
        + _line() + "\n"
        "/scan\n"
        "  → Sofort-Scan heutiger Spiele\n\n"
        "/portfolio\n"
        "  → P&L, ROI, Win-Rate nach Markt\n\n"
        "/open\n"
        "  → Alle offenen Wetten\n\n"
        "/tennis\n"
        "  → Tennis Value Bets (Wimbledon)\n\n"
        "/analyse Heim vs Auswaerts\n"
        "  → Vollstaendige Match-Analyse\n\n"
        "/rating Teamname\n"
        "  → Elo, Marktwert, Form\n\n"
        "/hilfe\n"
        "  → Diese Uebersicht"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "Scan starten", "callback_data": "/scan"},
         {"text": "Portfolio", "callback_data": "/portfolio"}],
        [{"text": "Offene Wetten", "callback_data": "/open"}],
        [{"text": "🎾 Tennis", "callback_data": "/tennis"}],
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

        hr = elo_series[elo_series["home_team"] == team]
        ar = elo_series[elo_series["away_team"] == team]
        cands = []
        if not hr.empty:
            cands.append(float(hr.iloc[-1]["elo_home_post"]))
        if not ar.empty:
            cands.append(float(ar.iloc[-1]["elo_away_post"]))
        elo = max(cands) if cands else 1500.0

        form = rolling_form(team, now, historical, competitive_only=True)
        mom = momentum_score(team, now, historical)
        mv = SQUAD_VALUES_M.get(team, 0)

        # Form bar: filled circles for wins, empty for losses
        trend = mom.get("form_trend", 0)
        trend_arrow = "↑ Aufsteigend" if trend > 0.1 else ("↓ Absteigend" if trend < -0.1 else "→ Stabil")

        text = (
            f"<b>{team}</b>\n"
            + _line() + "\n"
            f"Elo-Rating:     {elo:.0f}\n"
            f"Kaderwert:      {mv:.0f} Mio EUR\n"
            + _line() + "\n"
            f"Form (5 Spiele)\n"
            f"Punkte/Spiel:   {form['form_pts']:.1f}\n"
            f"Tore/Spiel:     {form['form_gf']:.2f}\n"
            f"Gegentore:      {form['form_ga']:.2f}\n"
            f"Siegesserie:    {int(mom['win_streak'])}\n"
            f"Trend:          {trend_arrow}"
        )
    except Exception as e:
        text = f"Fehler bei {team}: {e}"

    return text, _rating_keyboard()


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

        p = dc.predict_match_staged(home, away, params, is_knockout=False, neutral=True)

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
        played = int(h2h.get("h2h_matches", 0))
        hw = int(h2h.get("h2h_home_wins", 0))
        dr = int(h2h.get("h2h_draws", 0))
        aw = played - hw - dr

        mv_h = SQUAD_VALUES_M.get(home, 0)
        mv_a = SQUAD_VALUES_M.get(away, 0)
        ratio = get_market_value_ratio(home, away)
        stronger = home if ratio >= 1 else away

        text = (
            f"<b>{home} vs {away}</b>\n"
            + _line() + "\n"
            f"<b>Modell-Prognose</b>\n"
            f"{home}:   {p['p_home']*100:.1f}%\n"
            f"Unentschieden:   {p['p_draw']*100:.1f}%\n"
            f"{away}:   {p['p_away']*100:.1f}%\n"
            + _line() + "\n"
            f"<b>Elo-Rating</b>\n"
            f"{home}:   {elo_h:.0f}  ({eh*100:.1f}% Siegchance)\n"
            f"{away}:   {elo_a:.0f}  ({ea*100:.1f}% Siegchance)\n"
            + _line() + "\n"
            f"<b>Kaderwert</b>\n"
            f"{home}:   {mv_h:.0f} Mio EUR\n"
            f"{away}:   {mv_a:.0f} Mio EUR\n"
            f"Favorit:   {stronger}  ({ratio:.2f}x)\n"
            + _line() + "\n"
            f"<b>Form (letzte 5 Pflichtspiele)</b>\n"
            f"{home}:   {fh['form_pts']:.1f} Pkt | {fh['form_gf']:.1f} Tore | {fh['form_ga']:.1f} Gegentore\n"
            f"{away}:   {fa['form_pts']:.1f} Pkt | {fa['form_gf']:.1f} Tore | {fa['form_ga']:.1f} Gegentore\n"
            + _line() + "\n"
            f"<b>Direktvergleich</b>  ({played} Spiele)\n"
            f"{home} {hw}S  –  {dr}U  –  {aw}S {away}"
        )
    except Exception as e:
        text = f"Fehler: {e}"
        return text, {}

    return text, _analyse_keyboard(home, away)


def _cmd_portfolio() -> tuple[str, dict]:
    from src.betting.ledger import ledger_summary, LEDGER_PATH
    s = ledger_summary(LEDGER_PATH)
    if s["n_bets"] == 0:
        return "Ledger ist leer — noch keine Wetten.", {}

    lines = [
        "<b>Portfolio-Übersicht</b>",
        _line(),
        f"Wetten gesamt:  {s['n_bets']}  (offen: {s['n_open']})",
        f"Gewonnen/Verloren: {s['n_won']}W / {s['n_lost']}L" + (f" / {s['n_void']}V" if s.get('n_void') else ""),
        f"Einsatz gesamt: {s['total_staked']:.2f} EUR",
        f"P&L:            {s['total_pnl']:+.2f} EUR",
        f"ROI:            {s['roi_pct']:+.1f}%",
        f"Win Rate:       {s['win_rate']:.1f}%",
    ]
    clv = s.get("mean_clv")
    if clv is not None:
        lines.append(f"Mean CLV:       {clv*100:+.1f}%")

    by_market = s.get("by_market", {})
    if by_market:
        lines += ["", "<b>Nach Markt:</b>"]
        for mkt, m in sorted(by_market.items(), key=lambda x: x[1]["pnl"], reverse=True):
            lines.append(
                f"  {mkt:<18} {m['pnl']:+6.2f} EUR  ROI: {m['roi_pct']:+.1f}%  ({m['won']}W/{m['lost']}L)"
            )

    keyboard = {"inline_keyboard": [
        [{"text": "Offene Wetten", "callback_data": "/open"},
         {"text": "Neuer Scan", "callback_data": "/scan"}],
    ]}
    return "\n".join(lines), keyboard


def _cmd_open() -> tuple[str, dict]:
    from src.betting.ledger import _load, LEDGER_PATH
    df = _load(LEDGER_PATH)
    if df.empty:
        return "Ledger ist leer.", {}

    open_bets = df[df["status"] == "open"]
    if open_bets.empty:
        return "Keine offenen Wetten.", {"inline_keyboard": [[{"text": "Portfolio", "callback_data": "/portfolio"}]]}

    lines = [f"<b>Offene Wetten ({len(open_bets)})</b>", _line()]
    for _, row in open_bets.iterrows():
        md = str(row.get("match_date", "")).strip() or "?"
        lines.append(
            f"<b>{row['home']} vs {row['away']}</b>  [{md}]\n"
            f"  {row['market'].upper()}  @{float(row['decimal_odds']):.2f}"
            f"  Einsatz: {float(row['stake_amount']):.2f} EUR"
        )

    keyboard = {"inline_keyboard": [
        [{"text": "Portfolio", "callback_data": "/portfolio"},
         {"text": "Neuer Scan", "callback_data": "/scan"}],
    ]}
    return "\n".join(lines), keyboard


def _cmd_tennis(tour: str = "both") -> tuple[str, dict]:
    """Zeigt den letzten Tennis-Scan aus dem results/-Ordner."""
    import glob
    import datetime as dt

    results_dir = Path(__file__).parent.parent / "results"
    # Suche nach tennis_scan_*.md Dateien
    reports = sorted(results_dir.glob("tennis_scan_*.md"), reverse=True)

    keyboard = {"inline_keyboard": [
        [{"text": "⚽ Fussball Scan", "callback_data": "/scan"},
         {"text": "📊 Portfolio", "callback_data": "/portfolio"}],
        [{"text": "🎾 Herren", "callback_data": "/tennis atp"},
         {"text": "🎾 Damen", "callback_data": "/tennis wta"}],
    ]}

    if not reports:
        return (
            "🎾 <b>Kein Tennis-Scan vorhanden.</b>\n"
            "Wimbledon beginnt am 30. Juni 2026.",
            keyboard,
        )

    latest = reports[0]
    age_h = (dt.datetime.now().timestamp() - latest.stat().st_mtime) / 3600
    stale_warning = f"\n<i>⚠️ Scan ist {age_h:.0f}h alt</i>" if age_h > 26 else ""

    scan_date = latest.stem.replace("tennis_scan_", "")
    content = latest.read_text()

    SEP = "─────────────────────"
    lines = [f"🎾 <b>Wimbledon — {scan_date}</b>{stale_warning}", SEP]

    in_elo_section = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line == "---":
            continue
        if line.startswith("# "):  # Haupttitel überspringen
            continue
        if "Top Grass Elo" in line:
            in_elo_section = True
            lines += [SEP, "<b>Top Grass Elo (Herren):</b>"]
            continue
        if line.startswith("## ") and not in_elo_section:
            lines.append(f"\n<b>{line[3:]}</b>")
            continue
        if line.startswith("Surface:") and not in_elo_section:
            continue
        if line.startswith("*No value"):
            lines.append("<i>Keine Value Bets heute.</i>")
            continue
        lines.append(line)

    text = "\n".join(lines)
    # Telegram Limit: 4096 Zeichen
    if len(text) > 3800:
        text = text[:3800].rsplit("\n", 1)[0] + "\n<i>... (gekürzt)</i>"

    return text, keyboard


def _cmd_scan() -> tuple[str, dict]:
    from src.scanner.daily_scan import run_daily_scan
    from src.notifications.telegram import send_scan_alert, _market_label
    from src.betting.ledger import ledger_summary, LEDGER_PATH
    import datetime
    try:
        _, _all_sigs, signals, _match_dates, match_contexts = run_daily_scan(bankroll=100.0, auto_log=False)
        if not signals:
            return "Kein Value-Signal heute gefunden.", {"inline_keyboard": [[{"text": "Alle Befehle", "callback_data": "/hilfe"}]]}

        # Use the same rich alert format as the auto-scan
        sent = send_scan_alert(
            signals,
            {**ledger_summary(LEDGER_PATH), "bankroll": 100.0},
            scan_date=datetime.datetime.now().strftime("%Y-%m-%d"),
            bankroll=100.0,
            match_contexts=match_contexts,
        )
        if sent:
            return "Scan-Ergebnis wurde als Alert gesendet.", {}
        else:
            # Fallback: build simple text if alert couldn't be sent
            lines = ["<b>Tages-Scan</b>\n" + _line()]
            for s in signals[:3]:
                b365 = f"Bet365: {s.b365_odds:.2f}" if s.b365_odds > 1.0 else f"Kurs: {s.decimal_odds:.2f}"
                lines.append(
                    f"<b>{s.home} vs {s.away}</b>\n"
                    f"{_market_label(s.market, s.home, s.away)}\n"
                    f"{b365}  |  EV: +{s.ev*100:.1f}%"
                )
            return "\n" + _line() + "\n".join(lines), _scan_keyboard(signals)
    except Exception as e:
        return f"Scan-Fehler: {e}", {}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _dispatch(text: str) -> tuple[str, dict]:
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
    if cmd == "/portfolio":
        return _cmd_portfolio()
    if cmd == "/open":
        return _cmd_open()
    if cmd == "/tennis":
        tour_arg = parts[1].lower() if len(parts) >= 2 else "both"
        return _cmd_tennis(tour_arg)

    return (
        f"Unbekannter Befehl: {cmd}",
        {"inline_keyboard": [[{"text": "Alle Befehle", "callback_data": "/hilfe"}]]}
    )


# ---------------------------------------------------------------------------
# Update processing
# ---------------------------------------------------------------------------

def _process_updates(updates: list[dict]) -> tuple[int, int]:
    offset, processed = 0, 0
    for upd in updates:
        offset = upd["update_id"] + 1
        if "message" in upd:
            text = upd["message"].get("text", "").strip()
            if text.startswith("/"):
                reply, keyboard = _dispatch(text)
                _send(reply, keyboard or None)
                processed += 1
        elif "callback_query" in upd:
            cq = upd["callback_query"]
            _answer_callback(cq["id"])
            data = cq.get("data", "").strip()
            if data.startswith("/"):
                reply, keyboard = _dispatch(data)
                _send(reply, keyboard or None)
                processed += 1
    return offset, processed


def main(poll: bool = False) -> None:
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nicht gesetzt.")
        return

    offset = _load_offset()

    if poll:
        print("Bot lauscht 3 Minuten auf Button-Taps... (Ctrl+C zum Beenden)")
        deadline = time.time() + 180
        total = 0
        while time.time() < deadline:
            updates = _get_updates(offset)
            if updates:
                new_offset, n = _process_updates(updates)
                if new_offset:
                    offset = new_offset
                    _save_offset(offset)
                total += n
            time.sleep(2)
        print(f"Fertig. {total} Nachricht(en) verarbeitet.")
    else:
        updates = _get_updates(offset)
        new_offset, n = _process_updates(updates)
        if new_offset:
            _save_offset(new_offset)
        if n:
            print(f"  Telegram bot: {n} Nachricht(en) verarbeitet.")


if __name__ == "__main__":
    poll_mode = "--poll" in sys.argv
    main(poll=poll_mode)

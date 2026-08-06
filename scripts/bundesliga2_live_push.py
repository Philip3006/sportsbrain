"""Phase E — 2.BL Live-Push: Anpfiff / Tor / Halbzeit / Abpfiff / Settlement.

Läuft im 90s-Cron am Spieltag (Fr 20:30 / Sa 13:00 & 20:30 / So 13:30 UTC).
Skippt sofort wenn keine offenen 2.BL-Bets — spart ESPN-Requests.

Push-Events (alle via web_push.send_notification):
  ⚽ Anpfiff:   {home} vs {away}
  🥅 Tor!      {home} {H}-{A} {away}
  ⏸ HZ:        {home} {H}-{A} {away}
  ✅ Abpfiff:   {home} {H}-{A} {away} — dein Bet: won/lost/void
  ⏰ Settle:    Match beendet, aber kein Auto-Settle nach 30 Min
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DEFAULT_USER, ledger_path_for
from src.data.football_live import fetch_scoreboard, canonical_match_key
from src.config import canonical_name

SPORT_KEY = "soccer_germany_bundesliga2"
LEAGUE_SHORT = "bl2"
CACHE_PATH = ROOT / "data" / "cache" / "bundesliga2_live_scores.json"
DOCS_PATH  = ROOT / "docs" / "data" / "bundesliga2_live_scores.json"


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cache, indent=2, ensure_ascii=False)
    CACHE_PATH.write_text(payload)
    DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOCS_PATH.write_text(payload)


def _touch_heartbeat(reason: str) -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache = _load_cache()
    cache["_meta"] = {"heartbeat_at": now_iso, "reason": reason}
    _save_cache(cache)


def _open_bl2_bets() -> list[dict]:
    """Offene Wetten mit league='bl2' aus allen User-Ledgern."""
    bets: list[dict] = []
    ledger_dir = ledger_path_for(DEFAULT_USER).parent
    for ledger in ledger_dir.glob("ledger_*.csv"):
        if "backfill" in ledger.stem or "backup" in ledger.stem:
            continue
        try:
            with open(ledger, newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("status") != "open":
                        continue
                    if (r.get("league") or "").strip() == LEAGUE_SHORT:
                        bets.append(r)
        except Exception:
            continue
    return bets


def _send(title: str, body: str, kind: str = "generic") -> bool:
    try:
        from src.notifications.web_push import _send_notification
        _send_notification(title, body)
        return True
    except Exception as exc:
        print(f"  push failed ({kind}): {exc}")
        return False


def _fetch_odds_api_scoreboard() -> dict[str, dict]:
    """TheOddsAPI /scores als primäre Quelle (~30s Lag). Gibt gleiche Struktur wie fetch_scoreboard() zurück."""
    try:
        from src.data.odds_api import fetch_scores
        raw = fetch_scores(sport=SPORT_KEY, days_from=1)
    except Exception as exc:
        print(f"  TheOddsAPI scores failed: {exc}")
        return {}
    if not raw:
        return {}

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, dict] = {}
    for m in raw:
        home_name = m.get("home_team", "")
        away_name = m.get("away_team", "")
        if not home_name or not away_name:
            continue
        completed = bool(m.get("completed", False))
        scores_raw = m.get("scores") or []
        h_score = a_score = None
        for sc in scores_raw:
            name = sc.get("name", "")
            try:
                val = int(sc["score"])
            except (KeyError, ValueError, TypeError):
                continue
            if name == home_name:
                h_score = val
            elif name == away_name:
                a_score = val
        # Infer status from data
        if completed:
            status = "post"
        elif h_score is not None:
            status = "in"
        else:
            status = "pre"
        ck = canonical_match_key(home_name, away_name)
        out[ck] = {
            "home":          home_name,
            "away":          away_name,
            "home_score":    h_score,
            "away_score":    a_score,
            "status":        status,
            "period":        0,
            "clock_display": "",
            "completed":     completed,
            "commence_time": m.get("commence_time", ""),
            "last_update":   now_iso,
        }
    return out


def main() -> int:
    bets = _open_bl2_bets()
    if not bets:
        print(f"[bl2_live_push] Keine offenen 2.BL-Wetten — Skip.")
        _touch_heartbeat("no open bets")
        return 0

    # Dedupliziere Matches
    match_pairs: dict[str, tuple[str, str]] = {}
    for b in bets:
        home, away = b.get("home", ""), b.get("away", "")
        if not home or not away:
            continue
        ck = canonical_match_key(home, away)
        if ck not in match_pairs:
            match_pairs[ck] = (home, away)

    print(f"[bl2_live_push] {len(match_pairs)} 2.BL-Matches mit offenen Wetten")

    # Primary: TheOddsAPI /scores (in-play + completed, ~30s Lag)
    scoreboard = _fetch_odds_api_scoreboard()
    if scoreboard:
        print(f"  Quelle: TheOddsAPI scores ({len(scoreboard)} Matches)")
    else:
        # Fallback: ESPN public endpoint (kostenlos, ~60s Lag)
        scoreboard = fetch_scoreboard(SPORT_KEY)
        if scoreboard:
            print(f"  Quelle: ESPN fallback ({len(scoreboard)} Matches)")
        else:
            print("  Beide Quellen leer — heartbeat only")
            _touch_heartbeat("empty scoreboard")
            return 0

    cache = _load_cache()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pushes_sent = 0

    for ck, (home, away) in match_pairs.items():
        sc = scoreboard.get(ck)
        if not sc:
            continue

        prev = cache.get(ck, {})
        status = sc.get("status", "pre")
        hg = sc.get("home_score")
        ag = sc.get("away_score")
        completed = sc.get("completed", False)
        period = sc.get("period", 0)

        # Anpfiff
        if status == "in" and prev.get("status") != "in" and not prev.get("kickoff_pushed"):
            if _send("⚽ Anpfiff", f"{home} vs {away}", "kickoff"):
                pushes_sent += 1
                prev["kickoff_pushed"] = True
                print(f"  ⚽ Anpfiff: {home} vs {away}")

        # Tor
        if hg is not None and ag is not None:
            prev_hg = prev.get("home_score", 0) or 0
            prev_ag = prev.get("away_score", 0) or 0
            if hg > prev_hg or ag > prev_ag:
                if _send("🥅 Tor!", f"{home} {hg}-{ag} {away}", "goal"):
                    pushes_sent += 1
                    print(f"  🥅 Tor: {home} {hg}-{ag} {away}")

        # Halbzeit (period=1 abgeschlossen, period wechselt oder clock="HT")
        clock = sc.get("clock_display", "").lower()
        is_ht = "ht" in clock or (period == 1 and status == "in" and "0:00" in clock)
        if is_ht and not prev.get("ht_pushed"):
            if _send("⏸ Halbzeit", f"{home} {hg}-{ag} {away}", "ht"):
                pushes_sent += 1
                prev["ht_pushed"] = True
                print(f"  ⏸ HZ: {home} {hg}-{ag} {away}")

        # Abpfiff
        if completed and not prev.get("ft_pushed"):
            if _send("✅ Abpfiff", f"{home} {hg}-{ag} {away} — jetzt Settlement prüfen!", "ft"):
                pushes_sent += 1
                prev["ft_pushed"] = True
                print(f"  ✅ Abpfiff: {home} {hg}-{ag} {away}")

        # State updaten
        cache[ck] = {
            **prev,
            "home":         home,
            "away":         away,
            "home_score":   hg,
            "away_score":   ag,
            "status":       status,
            "period":       period,
            "clock":        sc.get("clock_display", ""),
            "completed":    completed,
            "updated":      now_iso,
        }

    cache["_meta"] = {"heartbeat_at": now_iso, "n_matches_watched": len(match_pairs),
                       "pushes_sent": pushes_sent}
    _save_cache(cache)
    print(f"[bl2_live_push] Fertig — {pushes_sent} Push(es) gesendet.")
    return 0


if __name__ == "__main__":
    from time import monotonic
    _t0 = monotonic()
    try:
        code = main()
        try:
            from src.monitoring.health_writer import write_health
            write_health("bundesliga2_live_push", "ok",
                         duration_s=monotonic() - _t0, exit_code=code)
        except Exception:
            pass
        sys.exit(code)
    except Exception as _exc:
        try:
            from src.monitoring.health_writer import write_health
            write_health("bundesliga2_live_push", "error",
                         duration_s=monotonic() - _t0,
                         error=str(_exc)[:200], exit_code=1)
        except Exception:
            pass
        raise

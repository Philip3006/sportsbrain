"""Live-Score-Push: Tor + Halbzeit Notifications für offene Wetten.

Logik:
  1. Lies offene Wetten aus ledger.csv
  2. Skip wenn KEIN Match aktuell live (now ∈ [kickoff, kickoff+115min])
  3. Sonst: TheOddsAPI /scores einmal pollen
  4. Vergleiche jeden Live-Match-Score gegen Cache in data/cache/live_scores.json
  5. Score gestiegen → ⚽ Tor-Push mit neuem Stand
  6. Elapsed > 50 Min und ht_sent != true → ⏸️ Halbzeit-Push
  7. Cache schreiben + via Workflow committen

Wird via .github/workflows/live_score_push.yml alle 5-10 Min ausgelöst.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "results" / "ledger.csv"
SIGNALS = ROOT / "docs" / "data" / "signals.json"
CACHE_PATH = ROOT / "data" / "live_scores.json"


def _norm(s: str) -> str:
    return (s or "").lower().strip()


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def main() -> int:
    if not LEDGER.exists() or not SIGNALS.exists():
        return 0

    # 1. Open bets
    open_bets: list[dict] = []
    with open(LEDGER, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") == "open":
                open_bets.append(r)
    if not open_bets:
        print("Keine offenen Wetten — Skip.")
        return 0

    # 2. Schedule (Kickoff-Map)
    try:
        data = json.loads(SIGNALS.read_text())
    except Exception:
        return 0
    schedule = data.get("schedule", [])
    ko_map: dict[tuple[str, str], str] = {}
    for g in schedule:
        h = _norm(g.get("home", ""))
        a = _norm(g.get("away", ""))
        ko = g.get("kickoff", "")
        if h and a and ko:
            ko_map[(h, a)] = ko

    # 3. Welche Matches sind aktuell live?
    now = datetime.now(timezone.utc)
    live_match_keys: set[tuple[str, str]] = set()
    for b in open_bets:
        h = _norm(b.get("home", ""))
        a = _norm(b.get("away", ""))
        ko_iso = ko_map.get((h, a))
        if not ko_iso:
            continue
        try:
            ko_dt = datetime.fromisoformat(ko_iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        elapsed_min = (now - ko_dt).total_seconds() / 60.0
        if -5 <= elapsed_min <= 115:  # 5 Min Slack vor KO, bis 115 nach KO
            live_match_keys.add((h, a))

    if not live_match_keys:
        print("Keine offenen Wetten haben aktuell ein Live-Match — Skip (kein API-Call).")
        return 0

    print(f"Live-Polling für {len(live_match_keys)} Match(es)...")

    # 4. Live-Scores fetchen
    from src.data.odds_api import fetch_wm_live_scores
    try:
        scores = fetch_wm_live_scores(days_from=2, api_key=os.getenv("ODDS_API_KEY"))
    except Exception as e:
        print(f"Score-Fetch fehlgeschlagen: {e}")
        return 1

    cache = _load_cache()
    pushes_sent = 0

    from src.notifications.web_push import _send_notification

    for m in scores:
        h, a = m.get("home", ""), m.get("away", "")
        key_tuple = (_norm(h), _norm(a))
        if key_tuple not in live_match_keys:
            continue
        match_id = m.get("match_id", f"{h}_vs_{a}")
        ko_iso = ko_map.get(key_tuple, m.get("commence_time", ""))
        try:
            ko_dt = datetime.fromisoformat(ko_iso.replace("Z", "+00:00"))
            elapsed_min = (now - ko_dt).total_seconds() / 60.0
        except ValueError:
            elapsed_min = 0.0

        hs, as_ = m.get("home_score"), m.get("away_score")
        if hs is None or as_ is None:
            continue

        prev = cache.get(match_id, {})
        prev_hs = prev.get("home_score")
        prev_as = prev.get("away_score")
        ht_sent = bool(prev.get("ht_sent", False))

        # Score-Änderung → Tor-Push
        if (prev_hs is not None and prev_as is not None
                and (hs > prev_hs or as_ > prev_as)):
            scorer = h if hs > prev_hs else a
            other = a if scorer == h else h
            new_s_score = hs if scorer == h else as_
            other_score = as_ if scorer == h else hs
            elapsed_str = f"{int(max(0, elapsed_min))}'" if elapsed_min > 0 else "Live"
            if _send_notification(
                title=f"⚽ TOR — {scorer}",
                body=f"{elapsed_str}   {h} {hs} : {as_} {a}",
                url="/sportsbrain/#bets",
                kind="goal",
                tag=f"goal-{match_id}-{hs}-{as_}",
                require=False,
            ):
                pushes_sent += 1
                print(f"  ⚽ Goal pushed: {h} {hs}-{as_} {a}")

        # Halbzeit-Push (50 Min nach KO, einmal pro Match)
        if not ht_sent and 50 <= elapsed_min <= 65:
            if _send_notification(
                title=f"⏸️ HALBZEIT — {h} {hs} : {as_} {a}",
                body="Erste Hälfte vorbei. Tap für Match-Details.",
                url="/sportsbrain/#bets",
                kind="halftime",
                tag=f"ht-{match_id}",
                require=False,
            ):
                pushes_sent += 1
                ht_sent = True
                print(f"  ⏸️ HT pushed: {h} {hs}-{as_} {a}")

        cache[match_id] = {
            "home":       h,
            "away":       a,
            "home_score": hs,
            "away_score": as_,
            "completed":  m.get("completed", False),
            "ht_sent":    ht_sent,
            "updated":    now.isoformat(),
        }

    _save_cache(cache)
    print(f"Total {pushes_sent} push(es) gesendet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tennis Live-Score-Push: Satz-Abschluss + Suspension Notifications.

Logik:
  1. Lies offene Tennis-Wetten aus allen Ledgern
  2. Fetch ESPN live tennis scores (ATP + WTA)
  3. Vergleiche gegen Cache in data/cache/tennis_live_scores.json
  4. Neuer abgeschlossener Satz → Push "🎾 Satz X: 6-Y | Stand"
  5. Spiel unterbrochen (suspended/postponed) → Push "⏸ Unterbrochen"
  6. Cache + docs/data/tennis_live_scores.json schreiben
  7. data/cache/tennis_suspended.json als Nebeneffekt aktualisieren

Wird via tennis_live_scan.yml alle 15 Min ausgeloest (10-22 UTC).
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.tennis_settlement import is_tennis_market
from src.config import DEFAULT_USER, ledger_path_for
from src.data.tennis_scores import canonical_match_key, fetch_tennis_scores_espn

CACHE_PATH = ROOT / "data" / "cache" / "tennis_live_scores.json"
DOCS_PATH  = ROOT / "docs" / "data" / "tennis_live_scores.json"
SUSP_PATH  = ROOT / "data" / "cache" / "tennis_suspended.json"


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


def _completed_sets(sets: list) -> list:
    return [(a, b) for a, b in sets if a != b and max(a, b) >= 6]


def _sets_str(sets: list, mark_last: bool = False) -> str:
    if not sets:
        return ""
    parts = []
    for i, (a, b) in enumerate(sets):
        s = f"{a}-{b}"
        if mark_last and i == len(sets) - 1:
            s += "*"
        parts.append(s)
    return ", ".join(parts)


def _sets_won(sets: list) -> tuple[int, int]:
    a = sum(1 for a, b in sets if a > b)
    b = sum(1 for a, b in sets if b > a)
    return a, b


def _open_tennis_bets() -> list[dict]:
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
                    market = r.get("market", "")
                    src = (r.get("source") or "").lower()
                    reason = (r.get("stake_reason") or "").lower()
                    if is_tennis_market(market) or "tennis" in src or "tennis" in reason:
                        bets.append(r)
        except Exception:
            continue
    return bets


def main() -> int:
    bets = _open_tennis_bets()
    if not bets:
        print("[tennis_live_push] Keine offenen Tennis-Wetten — Skip.")
        _touch_heartbeat("no open tennis bets")
        return 0

    # Dedupliziere Matches (ein Match kann mehrere offene Wetten haben)
    match_pairs: dict[str, tuple[str, str]] = {}
    for b in bets:
        home, away = b.get("home", ""), b.get("away", "")
        if not home or not away:
            continue
        ck = canonical_match_key(home, away)
        if ck not in match_pairs:
            match_pairs[ck] = (home, away)

    if not match_pairs:
        _touch_heartbeat("no valid match pairs")
        return 0

    print(f"[tennis_live_push] {len(match_pairs)} Tennis-Matches mit offenen Wetten")

    espn_all = {**fetch_tennis_scores_espn("atp"), **fetch_tennis_scores_espn("wta")}

    cache = _load_cache()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pushes_sent = 0
    live_count = 0
    suspended_matches: list[dict] = []

    try:
        from src.notifications.web_push import _send_notification
    except Exception:
        _send_notification = None  # type: ignore[assignment]

    for ck, (home, away) in match_pairs.items():
        sc = (espn_all.get(ck)
              or espn_all.get(f"{home} vs {away}")
              or espn_all.get(f"{away} vs {home}"))
        if not sc:
            continue

        status = sc.get("status", "")
        sets = sc.get("sets") or []

        if status not in ("in_progress", "suspended", "postponed"):
            continue  # completed/walkover → tennis_settle handelt das

        live_count += 1
        prev = cache.get(ck, {})
        curr_done = _completed_sets(sets)
        prev_done = _completed_sets(prev.get("sets", []))

        cache[ck] = {
            "home":        sc.get("player_a", home),
            "away":        sc.get("player_b", away),
            "sets":        sets,
            "sets_won":    list(_sets_won(curr_done)),
            "status":      status,
            "resume_time": sc.get("resume_time"),
            "updated":     now_iso,
            "suspended_push_sent": prev.get("suspended_push_sent", False),
        }

        # Suspension Push (einmal pro Unterbrechung)
        if status in ("suspended", "postponed"):
            suspended_matches.append({
                "match_key": ck,
                "home": sc.get("player_a", home),
                "away": sc.get("player_b", away),
                "status": status,
                "sets": sets,
                "resume_time": sc.get("resume_time"),
            })
            if not prev.get("suspended_push_sent") and _send_notification:
                label = "📅 Verschoben" if status == "postponed" else "⏸ Unterbrochen"
                score_str = _sets_str(sets)
                body = f"{home} vs {away}"
                if score_str:
                    body += f" · Stand: {score_str}"
                if _send_notification(
                    title=f"{label} — Tennis",
                    body=body,
                    url="/sportsbrain/#bets",
                    kind="suspension",
                    tag=f"susp-{ck}",
                    require=False,
                ):
                    pushes_sent += 1
                    cache[ck]["suspended_push_sent"] = True
                    print(f"  ⏸ Suspension pushed: {home} vs {away}")
            continue

        # Neu abgeschlossene Sätze → Push
        new_sets = curr_done[len(prev_done):]
        for i, (sa, sb) in enumerate(new_sets):
            set_num = len(prev_done) + i + 1
            aw, bw = _sets_won(curr_done)
            sets_str = _sets_str(curr_done)
            winner_name = sc.get("player_a", home) if sa > sb else sc.get("player_b", away)
            if _send_notification and _send_notification(
                title=f"🎾 Satz {set_num}: {sa}-{sb} — {winner_name}",
                body=f"{home} vs {away} · {sets_str} ({aw}:{bw} Sätze)",
                url="/sportsbrain/#bets",
                kind="tennis_set",
                tag=f"tset-{ck}-s{set_num}",
                require=False,
            ):
                pushes_sent += 1
                print(f"  🎾 Satz {set_num} pushed: {home} {sa}-{sb} {away}")

    cache["_meta"] = {
        "heartbeat_at": now_iso,
        "reason": f"{live_count} live matches, {pushes_sent} pushes",
    }
    _save_cache(cache)

    # tennis_suspended.json als Nebeneffekt aktualisieren
    SUSP_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUSP_PATH.write_text(json.dumps({"updated": now_iso, "matches": suspended_matches}, indent=2))

    print(f"[tennis_live_push] {live_count} live, {pushes_sent} push(es) gesendet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
